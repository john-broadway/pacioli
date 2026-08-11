# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the SPINE pure core (pacioli.spine) — the governed-submit orchestration.

Gates (TOCTOU freshness → closed-books check → CONSENT/reserve) → atomic CLAIM (CAS, closes the
concurrency race) → durable intent BEFORE execute → execute → commit+outcome (or release+failed).
Side effects are injected so ordering, crash-safety, and the concurrency guard are testable bench-free.

Run: `python3 -m unittest pacioli.tests.test_spine` from the broker app root. No frappe required.
"""
import sqlite3
import unittest

from pacioli.consent import CONSUMED, LIVE, RESERVED, new_marker
from pacioli.plan import new_plan
from pacioli.prove import orphans
from pacioli.spine import _transition_end, governed_submit
from pacioli.store import BrokerStore

TOKEN = "marker-token"
PLAN = new_plan(
    plan_id="p1", target="acme/Acme Corp", doc_version="v1",
    posting_date="2026-06-30", projected_gl=[], risk_flags=[], ts="t0",
)
MARKER = new_marker(TOKEN, "p1", expires_at=1000.0)
NOW_EPOCH = 500.0
NOW_DATE = "2026-07-01"
DOC_VERSION = "v1"


class AnsweredError(Exception):
    """A stand-in for an answered ``ErpnextError`` — carries ``answered=True`` exactly like the
    real transport-taxonomy exception (spine.py only ever duck-types ``getattr(exc, "answered",
    False)``, so a plain exception with the attribute is enough to pin the branch without
    importing pacioli.erpnext into this bench-free pure-core test module)."""

    def __init__(self, message):
        super().__init__(message)
        self.answered = True


class FakeEffects:
    """Records call order; can simulate a lost claim (concurrent race), an execute failure, or a
    post-failure readback (the no-answer/ambiguous branch). ``readback`` is a thin seam — exactly
    like the real one wired to ``client.get_document(doctype, name).get("docstatus")`` — so it can
    raise; the pure core is responsible for never letting that raise escape (mirrors how it already
    owns ``execute``'s raise).

    ``intent_raises``/``outcome_raise_times`` simulate the WG-2b residual: an unexpected exception
    from the store's own persistence layer (e.g. ``prove.append``'s JSON-native guard rejecting a
    non-finite float that slipped past every upstream check) rather than from the bench transport.
    ``outcome_raise_times`` counts down across ALL ``record_outcome`` calls this effects instance
    sees (a fresh claim's whole flow calls it at most twice: once for real, once for the pure
    core's own sanitized retry) — set to 1 to fail only the first attempt (retry recovers), or
    higher to prove even a double failure never crashes the flow."""

    def __init__(self, execute_raises=None, claim=True, execute_result=None,
                readback_result="unset", readback_raises=None,
                intent_raises=None, outcome_raise_times=0, outcome_raises=None):
        self.calls = []
        self._raises = execute_raises
        self._claim = claim
        self._result = execute_result
        self._readback_result = readback_result
        self._readback_raises = readback_raises
        self._intent_raises = intent_raises
        self._outcome_raise_times = outcome_raise_times
        self._outcome_raises = outcome_raises or RuntimeError("store write failed")
        self._outcome_call_count = 0
        self.final_marker = None
        self.outcome_calls = []  # every ATTEMPTED (status, result) pair, including failed retries

    def claim_marker(self, reserved):
        self.calls.append("claim")
        return self._claim

    def record_intent(self, body):
        self.calls.append("intent")
        if self._intent_raises:
            raise self._intent_raises
        self.intent_body = body
        return ("intent-receipt", body)

    def execute(self):
        self.calls.append("execute")
        if self._raises:
            raise self._raises
        return self._result if self._result is not None else {"docstatus": 1, "name": "SINV-001"}

    def readback(self):
        self.calls.append("readback")
        if self._readback_raises:
            raise self._readback_raises
        return None if self._readback_result == "unset" else self._readback_result

    def record_outcome(self, intent, status, result, final_marker):
        self._outcome_call_count += 1
        self.outcome_calls.append((status, result))
        if self._outcome_call_count <= self._outcome_raise_times:
            raise self._outcome_raises
        self.calls.append(("outcome", status))
        self.final_marker = final_marker


def _submit(effects, plan=PLAN, marker=MARKER, token=TOKEN, doc_version=DOC_VERSION, locks=None):
    return governed_submit(
        plan=plan, marker=marker, token=token, current_doc_version=doc_version,
        now_epoch=NOW_EPOCH, now_date=NOW_DATE, locks=locks or {}, effects=effects,
    )


class TestHappyPath(unittest.TestCase):
    def test_success(self):
        res = _submit(FakeEffects())
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.stage, "done")
        self.assertEqual(res.result["name"], "SINV-001")

    def test_order_claim_intent_execute_then_outcome(self):
        # concurrency guard AND crash guard: claim + durable intent BOTH precede the irreversible submit
        fx = FakeEffects()
        _submit(fx)
        self.assertEqual(fx.calls, ["claim", "intent", "execute", ("outcome", "committed")])

    def test_marker_consumed_on_success(self):
        fx = FakeEffects()
        _submit(fx)
        self.assertEqual(fx.final_marker.state, CONSUMED)


class TestConcurrencyClaim(unittest.TestCase):
    def test_lost_claim_denies_before_execute(self):
        # a racing submit already reserved the marker → CAS loses → refuse, nothing executed
        fx = FakeEffects(claim=False)
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "consent")
        self.assertEqual(fx.calls, ["claim"])  # claimed-and-lost; never recorded intent, never executed


class TestGatesBlockBeforeClaim(unittest.TestCase):
    def test_stale_plan_blocks(self):
        fx = FakeEffects()
        res = _submit(fx, doc_version="v2")
        self.assertEqual(res.stage, "fresh")
        self.assertEqual(fx.calls, [])

    def test_red_line_blocks(self):
        # uses now_date (ISO), not now_epoch — the split-clock fix
        fx = FakeEffects()
        res = _submit(fx, locks={"frozen_until": "2026-12-31"})
        self.assertEqual(res.stage, "red_line")
        self.assertEqual(fx.calls, [])

    def test_bad_marker_blocks(self):
        fx = FakeEffects()
        res = _submit(fx, token="wrong-token")
        self.assertEqual(res.stage, "consent")
        self.assertEqual(fx.calls, [])  # reserve fails before the claim is even attempted


class TestExecuteFailure(unittest.TestCase):
    """Transport taxonomy (docs/plans/2026-07-07-transport-taxonomy.md): an ANSWERED refusal
    (``ErpnextError``-shaped, ``answered=True``) keeps today's byte-identical release+"failed"
    behavior. Everything else — including a raw, unconverted exception with no ``answered``
    attribute at all — is now "no answer": the marker is SPENT (never released) and resolved via a
    governed readback, because the mutating call may already be in motion server-side. THE FLIP
    (deliberate, deny-biased, CHANGELOG-worthy): a bare ``RuntimeError`` used to release the marker
    (the old, never-verified "no progress" assumption); it now spends+readbacks instead — see
    ``test_unanswered_exception_spends_marker_and_resolves_via_readback`` below, the direct sibling
    of the answered-error pin this test used to be."""

    def test_answered_error_records_failed_and_releases_marker(self):
        # Byte-identical to the pre-taxonomy behavior — pinned against ANY future regression that
        # would loosen this branch (the redteam property: never add a release path here).
        fx = FakeEffects(execute_raises=AnsweredError("HTTP 500: ValidationError"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "execute")
        self.assertEqual(fx.calls, ["claim", "intent", "execute", ("outcome", "failed")])
        self.assertEqual(fx.final_marker.state, LIVE)  # a failed submit must not burn the grant

    def test_unanswered_exception_spends_marker_and_resolves_via_readback(self):
        # THE FLIP: a raw, unconverted exception (no `answered` attribute at all — the exact shape
        # of the pre-fix residual, e.g. a raw OSError that escaped default_transport, or any other
        # unclassified raise) is "no answer", not an answered refusal. The old law released the
        # marker on the unverified assumption that nothing happened; the new law never releases a
        # grant for an act that may already be server-side, and instead confirms via readback.
        fx = FakeEffects(execute_raises=RuntimeError("bench 500"), readback_result=1)
        res = _submit(fx)
        self.assertTrue(res.ok, res.reason)  # readback confirms docstatus 1 == the 0->1 transition
        self.assertEqual(res.stage, "done")
        self.assertEqual(res.result["confirmed_via"], "post_failure_readback")
        # Receipt honesty (redteam catch): the durable receipt carries WHAT failed, not just that
        # a readback later confirmed it — an auditor reads this months on, without the code.
        self.assertEqual(res.result["error"], "bench 500")
        self.assertEqual(fx.calls,
                         ["claim", "intent", "execute", "readback", ("outcome", "committed")])
        self.assertEqual(fx.final_marker.state, CONSUMED)  # spent, never released

    def test_unanswered_exception_readback_mismatch_is_unconfirmed_marker_still_spent(self):
        fx = FakeEffects(execute_raises=RuntimeError("timeout"), readback_result=0)  # unchanged
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "unconfirmed")
        self.assertEqual(fx.calls,
                         ["claim", "intent", "execute", "readback", ("outcome", "unconfirmed")])
        self.assertEqual(fx.final_marker.state, CONSUMED)  # spent, NOT released-in-flight

    def test_unanswered_exception_readback_itself_raises_unconfirmed_with_readback_error(self):
        fx = FakeEffects(execute_raises=RuntimeError("timeout"),
                         readback_raises=RuntimeError("readback also timed out"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "unconfirmed")
        self.assertIn("readback also timed out", res.reason)
        self.assertEqual(fx.calls,
                         ["claim", "intent", "execute", "readback", ("outcome", "unconfirmed")])
        self.assertEqual(fx.final_marker.state, CONSUMED)  # never release-in-flight

    def test_answered_error_never_attempts_a_readback(self):
        # The answered branch is byte-identical to before: no readback call at all.
        fx = FakeEffects(execute_raises=AnsweredError("HTTP 417: unbalanced"))
        _submit(fx)
        self.assertNotIn("readback", fx.calls)

    def test_status_429_is_answered_releases_marker(self):
        # T3 (pin sheet): 429 is always pre-handler — guaranteed no progress, safe to release
        # wherever emitted. Simulated here the same way as any other answered exception (the real
        # classification lives in erpnext.py/_answered; this pins the spine's consumption of it).
        err = AnsweredError("HTTP 429: rate limited")
        fx = FakeEffects(execute_raises=err)
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(fx.final_marker.state, LIVE)


class TestUnconfirmedOutcome(unittest.TestCase):
    """The execute response must CONFIRM the claimed transition — the book never records
    'committed' for a write the response did not show. Found live (envelope E1, 2026-07-07):
    ERPNext queues a >100-row Journal Entry submit/cancel to a background worker, frappe returns
    200 with the doc still at its pre-transition docstatus, and the broker recorded a committed
    0->1 that had not happened (the worker made it true ~28s later — but a failed worker would
    have left the book claiming more than reality)."""

    def test_docstatus_mismatch_records_unconfirmed_not_committed(self):
        fx = FakeEffects(execute_result={"docstatus": 0, "name": "JV-BIG"})
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "unconfirmed")
        self.assertIn("queue", (res.reason or "").lower())  # names the known ERPNext cause
        self.assertEqual(fx.calls, ["claim", "intent", "execute", ("outcome", "unconfirmed")])

    def test_marker_spent_on_unconfirmed(self):
        # Consent initiated an irreversible act now in motion server-side — the grant is spent by
        # that commitment; releasing it would let one marker initiate a second act.
        fx = FakeEffects(execute_result={"docstatus": 0, "name": "JV-BIG"})
        _submit(fx)
        self.assertEqual(fx.final_marker.state, CONSUMED)

    def test_missing_docstatus_is_unconfirmed_too(self):
        # Deny-biased: a response that doesn't show the transition at all cannot confirm it.
        fx = FakeEffects(execute_result={"name": "JV-BIG"})
        res = _submit(fx)
        self.assertEqual(res.stage, "unconfirmed")

    def test_cancel_checks_its_own_end_state(self):
        fx = FakeEffects(execute_result={"docstatus": 1, "name": "JV-BIG"})  # still 1, expected 2
        res = governed_submit(
            plan=PLAN, marker=MARKER, token=TOKEN, current_doc_version=DOC_VERSION,
            now_epoch=NOW_EPOCH, now_date=NOW_DATE, locks={}, effects=fx,
            op="cancel", transition="1->2",
        )
        self.assertEqual(res.stage, "unconfirmed")

    def test_cancel_confirmed_when_docstatus_2(self):
        fx = FakeEffects(execute_result={"docstatus": 2, "name": "JV-BIG"})
        res = governed_submit(
            plan=PLAN, marker=MARKER, token=TOKEN, current_doc_version=DOC_VERSION,
            now_epoch=NOW_EPOCH, now_date=NOW_DATE, locks={}, effects=fx,
            op="cancel", transition="1->2",
        )
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.stage, "done")


class TestIntentRecordsDoctype(unittest.TestCase):
    """The intent receipt is self-describing: it carries the plan's doctype so an auditor reads it
    off the receipt, not only via a join to the (prunable) plan store."""

    def test_intent_body_carries_plan_doctype(self):
        pi_plan = new_plan(
            plan_id="p1", target="acme/Acme Corp", doc_version="v1", posting_date="2026-06-30",
            projected_gl=[], risk_flags=[], ts="t0", op="submit", doctype="Purchase Invoice",
        )
        fx = FakeEffects()
        _submit(fx, plan=pi_plan, marker=new_marker(TOKEN, "p1", expires_at=1000.0))
        self.assertEqual(fx.intent_body["doctype"], "Purchase Invoice")

    def test_intent_doctype_defaults_to_sales_invoice(self):
        fx = FakeEffects()
        _submit(fx)  # the default PLAN has no explicit doctype
        self.assertEqual(fx.intent_body["doctype"], "Sales Invoice")


class TestPostClaimExceptionRobustness(unittest.TestCase):
    """WG-2b: the residual named in the 2026-07-10 readiness audit — "a post-claim exception still
    strands the marker in `reserved` (dispatch's catch is narrow)". The concrete NaN trigger
    (WG-2a) is closed at ``reconcile.check_allocation``; this is the GENERAL robustness: any
    unexpected exception from ``effects.record_intent``/``effects.record_outcome`` themselves
    (e.g. ``prove.append``'s JSON-native guard rejecting a non-finite float that slipped past
    every upstream check) must never crash past a structured result, and must never let the
    marker become spendable again on an UNKNOWN failure."""

    def test_intent_recording_failure_is_structured_not_a_crash(self):
        # Post-claim, pre-wire: claim_marker already won (the marker is claimed/dead), but
        # record_intent itself raises before execute() is ever reached — nothing was sent to the
        # bench. governed_submit must return a structured SubmitResult, never let the raw
        # exception propagate (that propagation is exactly what let it past dispatch()'s narrow
        # `except (ErpnextError, RegistryError)` catch as an unstructured crash).
        fx = FakeEffects(intent_raises=ValueError(
            "body: non-finite float (nan) cannot be sealed into a receipt"))
        res = _submit(fx)  # must not raise
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.reason)
        self.assertNotEqual(res.stage, "done")
        # execute() was never reached — this is genuinely pre-wire, not a transport ambiguity.
        self.assertEqual(fx.calls, ["claim", "intent"])

    def test_intent_recording_failure_leaves_marker_unspendable(self):
        # No intent receipt exists to finalize (store.record_outcome requires a real intent), so
        # no outcome can be linked to one — the honest residual this module's docstring already
        # names for a claim -> intent crash ("dead -> fail-closed; a human re-mints"). The fix is
        # that this is now a clean structured deny instead of an unstructured crash; the marker
        # must NEVER be released back to live from this branch (deny-bias: an unknown failure must
        # never make a claimed grant spendable again).
        fx = FakeEffects(intent_raises=RuntimeError("store busy"))
        _submit(fx)
        # record_outcome was never called at all — nothing settled the marker, so store-side it
        # remains exactly what claim_marker left it: reserved, dead, not spendable.
        self.assertIsNone(fx.final_marker)
        self.assertEqual(fx.outcome_calls, [])

    def test_outcome_recording_failure_after_confirmed_success_degrades_to_unconfirmed(self):
        # Post-wire: execute() succeeds and the response CONFIRMS the transition, but the first
        # attempt to durably record the "committed" outcome raises (e.g. a non-finite float in the
        # result body). The pure core must never silently claim "committed" through a write that
        # itself just failed — it retries with a sanitized, deny-biased "unconfirmed" record
        # instead (mirrors the existing readback-failure degrade pattern in _resolve_no_answer).
        fx = FakeEffects(outcome_raise_times=1,
                         outcome_raises=ValueError("result.docstatus: non-finite float (nan)"))
        res = _submit(fx)
        self.assertFalse(res.ok)  # the ledger degraded — never report a clean "done"
        self.assertEqual(res.stage, "unconfirmed")
        self.assertEqual(len(fx.outcome_calls), 2)  # the poisoned attempt + the sanitized retry
        self.assertEqual(fx.outcome_calls[0][0], "committed")  # what was originally attempted
        self.assertEqual(fx.outcome_calls[1][0], "unconfirmed")  # never silently "committed"
        self.assertEqual(fx.calls, ["claim", "intent", "execute", ("outcome", "unconfirmed")])

    def test_outcome_recording_failure_after_success_marker_stays_spent_not_spendable(self):
        fx = FakeEffects(outcome_raise_times=1)
        _submit(fx)
        # Real-world progress may have happened (execute() confirmed it) — the marker must be
        # spent (CONSUMED), never released back to LIVE, regardless of the recording hiccup.
        self.assertEqual(fx.final_marker.state, CONSUMED)

    def test_outcome_recording_double_failure_never_crashes(self):
        # Even the sanitized retry fails (a genuinely unrecoverable store) — this must still
        # return a structured result, never raise past governed_submit.
        fx = FakeEffects(outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)  # must not raise
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.reason)
        self.assertNotIn(("outcome", "committed"), fx.calls)  # never recorded as landed

    def test_answered_failure_outcome_recording_failure_preserves_failed_status_on_retry(self):
        # Pre-wire truth (an ANSWERED refusal — the bench definitely refused, nothing landed) must
        # be preserved through the degrade retry: "failed" never gets silently promoted to
        # "unconfirmed" here, and the marker's RELEASE (this branch's existing, correct behavior
        # for a known-clean refusal) is unaffected by the recording hiccup.
        fx = FakeEffects(execute_raises=AnsweredError("HTTP 500: ValidationError"),
                         outcome_raise_times=1, outcome_raises=ValueError("boom"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(len(fx.outcome_calls), 2)
        self.assertEqual(fx.outcome_calls[0][0], "failed")
        self.assertEqual(fx.outcome_calls[1][0], "failed")  # preserved, not upgraded/downgraded
        self.assertEqual(fx.final_marker.state, LIVE)  # a failed submit must not burn the grant

    def test_unconfirmed_docstatus_mismatch_outcome_recording_failure_recovers(self):
        # The OTHER post-wire branch (docstatus mismatch, already "unconfirmed") must also survive
        # a first-attempt recording failure without crashing, and stays "unconfirmed" on retry.
        fx = FakeEffects(execute_result={"docstatus": 0, "name": "JV-BIG"},
                         outcome_raise_times=1, outcome_raises=ValueError("boom"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertEqual(res.stage, "unconfirmed")
        self.assertEqual(fx.outcome_calls[1][0], "unconfirmed")
        self.assertEqual(fx.final_marker.state, CONSUMED)


UNSETTLED = "the marker remains CLAIMED"


class TestDoubleFailureMessagesTellTheTruth(unittest.TestCase):
    """Every ``not recorded`` return — the sentence an operator reads when BOTH the outcome write
    and ``_settle``'s sanitized retry have failed. These are the worst-state messages, and the
    party-baselines lesson is that a false sentence hides exactly here.

    **The one fact none of them may misstate.** When ``_settle`` returns ``recorded=False``,
    ``record_outcome`` never completed, so *no marker write persisted*. ``commit()``/``release()``
    are pure — they return a new dataclass; only ``record_outcome`` writes one. So the marker is
    left exactly as ``claim_marker`` left it: ``reserved``, by the CAS that committed before
    ``execute``. Not ``live``, and **not** ``consumed``.

    ⚠️ Two of these said **"the consent marker is spent"** until 2026-08-11 — a durable fact the
    immediately-preceding failed write means did not happen. Proven false against a real
    ``BrokerStore`` in :class:`TestUnsettledMarkerIsReallyReservedInTheStore` below, which is the
    point of that class existing: asserting on ``FakeEffects.final_marker`` only proves the *fake*
    never received a marker, never that the *row* says ``reserved``.

    Why it mattered: "spent" tells an operator the ceremony completed and the grant is burned —
    nothing to clean up. The truth is a marker stranded in ``reserved``, which no sweep collects
    (``prove.orphans`` sweeps open *intents*, not markers) and which reads as neither a live grant
    nor a spent one. Different remediation, and the honest phrasing was already three lines away
    in this same function.
    """

    def test_answered_refusal_double_failure_says_the_grant_was_not_returned(self):
        # An ANSWERED refusal is the one branch whose marker was meant to be RELEASED (a failed
        # submit must not burn the grant). The release never persisted, so the human's grant is
        # stranded rather than returned — the message must not imply it came back.
        fx = FakeEffects(execute_raises=AnsweredError("HTTP 500: ValidationError"),
                         outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn(UNSETTLED, res.reason)
        self.assertIn("the grant was never returned", res.reason)
        self.assertIn("HTTP 500: ValidationError", res.reason)  # the original failure
        self.assertIn("store unreachable", res.reason)          # AND the recording failure
        self.assertIsNone(fx.final_marker)  # nothing was ever settled

    def test_unconfirmed_docstatus_double_failure_does_not_claim_the_marker_is_spent(self):
        fx = FakeEffects(execute_result={"docstatus": 0, "name": "JV-BIG"},
                         outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn(UNSETTLED, res.reason)
        self.assertNotIn("marker is spent", res.reason)
        self.assertIn("docstatus 0", res.reason)   # what was seen
        self.assertIn("expected 1", res.reason)    # what was required
        self.assertIsNone(fx.final_marker)

    def test_confirmed_success_double_failure_does_not_claim_the_marker_is_spent(self):
        # The bench DID the irreversible thing and confirmed it; only the book failed. Every word
        # about the document must stay true, and the marker must not be reported as settled.
        fx = FakeEffects(outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn("succeeded at the bench", res.reason)  # still true, still said
        self.assertIn(UNSETTLED, res.reason)
        self.assertNotIn("marker is spent", res.reason)
        self.assertIn("prove_orphans", res.reason)  # the sweep that DOES collect the open intent
        self.assertIsNone(fx.final_marker)

    def test_no_answer_readback_raises_double_failure(self):
        # readback itself raises AND the settle double-fails: covers _settle_or_deny's own deny.
        fx = FakeEffects(execute_raises=RuntimeError("connection reset"),
                         readback_raises=RuntimeError("readback timed out"),
                         outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn(UNSETTLED, res.reason)
        self.assertIn("no answer from the bench", res.reason)
        self.assertIsNone(fx.final_marker)

    def test_no_answer_readback_mismatch_double_failure(self):
        # readback ANSWERS and says the transition did NOT happen, then the settle double-fails.
        fx = FakeEffects(execute_raises=RuntimeError("connection reset"), readback_result=0,
                         outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn(UNSETTLED, res.reason)
        self.assertIsNone(fx.final_marker)

    def test_no_answer_readback_confirms_completion_double_failure(self):
        # The worst case in the module: the act DID land (readback proves it) and the book cannot
        # say so at all. The message must carry both halves.
        fx = FakeEffects(execute_raises=RuntimeError("connection reset"), readback_result=1,
                         outcome_raise_times=99, outcome_raises=RuntimeError("store unreachable"))
        res = _submit(fx)
        self.assertFalse(res.ok)
        self.assertIn("DID complete", res.reason)
        self.assertIn(UNSETTLED, res.reason)
        self.assertIsNone(fx.final_marker)

    def test_no_answer_readback_confirms_completion_degraded_record_IS_spent(self):
        # The CONTRAST case that keeps the fix honest: here the retry SUCCEEDS, so the committed
        # marker really was persisted. This message SHOULD say spent — proving the correction
        # above is about truth, not about deleting the word everywhere.
        fx = FakeEffects(execute_raises=RuntimeError("connection reset"), readback_result=1,
                         outcome_raise_times=1, outcome_raises=ValueError("nonfinite float"))
        res = _submit(fx)
        self.assertFalse(res.ok)                      # degraded — never a clean "done"
        self.assertEqual(res.stage, "unconfirmed")
        self.assertIn("marker is spent", res.reason)  # TRUE here: the retry persisted it
        self.assertNotIn(UNSETTLED, res.reason)
        self.assertEqual(fx.final_marker.state, CONSUMED)


class TestEveryDoubleFailureCarriesBothClauses(unittest.TestCase):
    """All five ``recorded=False`` returns, driven in one place.

    ⚠️ Written because sampling one site was not enough. The 08-11 batch introduced the
    ``prove_orphans`` pointer and claimed the shared constant meant the five "cannot drift apart
    again" — but only the MARKER sentence was shared. The pointer was hand-written at four sites
    and pinned at ONE, so an independent review deleted it from the other three with zero reds.
    It was also missing outright from the fifth (the answered refusal), where it is not merely
    true but the most explainable case of all: the bench refused, nothing posted, and the orphan
    the operator will meet later has a clean explanation nobody gave them.

    Both clauses are now constants and this test drives every path, so deleting either from any
    site reds here. A shared constant is not the guarantee — a test that visits every site is."""

    CASES = {
        "answered refusal": dict(execute_raises=AnsweredError("HTTP 500: ValidationError")),
        "docstatus mismatch": dict(execute_result={"docstatus": 0, "name": "JV-BIG"}),
        "confirmed success": dict(),
        "no answer, readback raises": dict(execute_raises=RuntimeError("connection reset"),
                                           readback_raises=RuntimeError("readback timed out")),
        "no answer, readback says no": dict(execute_raises=RuntimeError("connection reset"),
                                            readback_result=0),
        "no answer, readback says yes": dict(execute_raises=RuntimeError("connection reset"),
                                             readback_result=1),
    }

    def _drive(self, kw):
        fx = FakeEffects(outcome_raise_times=99,
                         outcome_raises=RuntimeError("store unreachable"), **kw)
        return _submit(fx), fx

    def test_all_of_them_carry_the_same_two_shared_clauses(self):
        # DRIFT BETWEEN SITES. Comparing against the constants is the right instrument for this
        # one question — "do all five say the same thing" — and the wrong one for "is what they
        # say correct", which is the test below.
        from pacioli.spine import _OPEN_INTENT, _UNSETTLED
        for label, kw in self.CASES.items():
            with self.subTest(path=label):
                res, fx = self._drive(kw)
                self.assertFalse(res.ok)
                self.assertIn(_UNSETTLED, res.reason)
                self.assertIn(_OPEN_INTENT, res.reason)
                self.assertIsNone(fx.final_marker)

    def test_all_of_them_use_the_operator_facing_words_that_have_to_be_there(self):
        # ⚠️ The test above CANNOT catch a reworded constant — it compares the constant against
        # itself, so editing `_UNSETTLED` edits both sides and it stays green. Proven: a mutation
        # dropping the store's own state literal from the sentence left it passing. Self-
        # referential assertions are this house's recorded trap (an extractor that reads the very
        # prose it is checking), and I walked into it while fixing a finding about drift.
        #
        # So the load-bearing WORDS are asserted literally. `reserved` matters most: it is what
        # `SELECT state FROM markers` returns and what the sibling refusal an operator will meet
        # next already says ("marker not available (state: reserved)"). A message describing the
        # row in words that cannot be grepped against the row is a message that costs them a
        # search.
        for label, kw in self.CASES.items():
            with self.subTest(path=label):
                res, _fx = self._drive(kw)
                self.assertIn("reserved", res.reason)
                self.assertIn("NOT recorded as spent", res.reason)
                self.assertIn("prove_orphans", res.reason)
                self.assertNotIn("marker is spent", res.reason)

    def test_the_two_no_answer_subbranches_no_longer_render_identically(self):
        # They shared one return and produced BYTE-IDENTICAL text, both saying "no answer from the
        # bench" — while in one of them the readback DID answer and said the transition had not
        # happened. An operator sent looking for a posting that is not there. Both siblings that
        # are not double failures carry their detail; this one dropped it, and since the ledger
        # write also failed, the detail was not persisted anywhere either.
        common = dict(execute_raises=RuntimeError("connection reset"), outcome_raise_times=99,
                      outcome_raises=RuntimeError("store unreachable"))
        raised = _submit(FakeEffects(readback_raises=RuntimeError("readback timed out"), **common))
        answered = _submit(FakeEffects(readback_result=0, **common))
        self.assertNotEqual(raised.reason, answered.reason)
        self.assertIn("readback ALSO failed", raised.reason)
        self.assertIn("readback timed out", raised.reason)
        self.assertIn("readback DID answer", answered.reason)
        self.assertIn("docstatus 0", answered.reason)
        self.assertIn("not the expected 1", answered.reason)


class TestUnsettledMarkerIsReallyReservedInTheStore(unittest.TestCase):
    """The claim above — "no marker write persisted, the row still reads ``reserved``" — asserted
    against a REAL :class:`BrokerStore` rather than against ``FakeEffects``.

    This class exists because the fake cannot prove it. ``fx.final_marker is None`` proves only
    that the *fake* was never handed a marker; the sentence in the message is about the *persisted
    row*. Reading the row is the only assertion that is about the same subject as the claim."""

    def _real_store_submit(self, record_outcome_raises):
        store = BrokerStore(sqlite3.connect(":memory:"), b"k" * 32,
                            now_iso=lambda: "2026-07-14T00:00:00Z")
        plan = new_plan(plan_id="p1", target="acme/Acme Corp", doc_version="v1",
                        posting_date="2026-06-30", projected_gl=[], risk_flags=[], ts="t0")
        store.record_plan(plan)
        store.mint_marker("tok", "p1", 1000.0)

        class Effects:
            def claim_marker(self, reserved):  return store.claim_marker(reserved)
            def record_intent(self, body):     return store.record_intent(body)
            def execute(self):                 return {"docstatus": 1, "name": "SINV-001"}
            def readback(self):                raise AssertionError("not reached")
            def record_outcome(self, *a):      raise record_outcome_raises

        res = governed_submit(plan=plan, marker=store.get_marker("tok"), token="tok",
                              current_doc_version="v1", now_epoch=500.0, now_date="2026-07-01",
                              locks={}, effects=Effects())
        return res, store

    def test_the_row_reads_reserved_not_consumed_after_a_double_failure(self):
        res, store = self._real_store_submit(RuntimeError("store unreachable"))
        state = store._conn.execute(
            "SELECT state FROM markers WHERE plan_id='p1'").fetchone()[0]
        self.assertEqual(state, RESERVED)     # NOT consumed — the commit never persisted
        self.assertNotEqual(state, CONSUMED)
        self.assertNotEqual(state, LIVE)
        self.assertIn(UNSETTLED, res.reason)  # and the message says exactly that

    def test_the_intent_is_an_orphan_so_the_sweep_named_in_the_message_finds_it(self):
        # The message points the operator at prove_orphans. That pointer must be real: the intent
        # WAS recorded (record_intent succeeded), no committed outcome ever landed, so the sweep
        # this sentence names is the one that actually surfaces it.
        res, store = self._real_store_submit(RuntimeError("store unreachable"))
        self.assertIn("prove_orphans", res.reason)
        open_intents = orphans(store.receipts())
        self.assertEqual(len(open_intents), 1)


class TestTransitionEndFailsOpenSoTheShippedLabelsAreGuarded(unittest.TestCase):
    """``_transition_end`` returns ``None`` for any label it cannot parse, and ``None`` SKIPS the
    docstatus confirmation check entirely (``if expected is not None``) — so an unparseable label
    silently disables envelope E1, the protection against ERPNext answering 200 with the write
    still queued to a background worker.

    No shipped caller can reach that today: ``tools.py`` passes only the literals ``"0->1"``
    (submit) and ``"1->2"`` (cancel) — verified exhaustively, ``tools.py`` being the only module
    that imports from ``pacioli.spine``, with one call site and two callers. So this is not a live
    defect; it is a trapdoor under the next op anyone adds.

    ⚠️ **THE GUARD IS NOT IN THIS CLASS, and the 08-11 commit wrongly said it was.** It claimed
    "a third op whose label cannot be read now goes red instead of quietly switching E1 off". An
    independent review disproved that by mutation: flipping the SHIPPED submit label to
    ``"draft->submitted"``, which turns E1 off for every submit doctype, reddened **zero** tests.
    Everything below examines string literals typed into this file, never what a call site passes,
    so no change to a call site can red them.

    The real guard is ``test_tools.TestEnvelopeE1HoldsAtTheLayerThatChoosesTheLabel``, which never
    mentions a label at all: it drives ``dispatch`` and asserts the unconfirmed-docstatus refusal,
    so E1 can only pass while the shipped label remains readable. Both mutations above red it.
    These tests keep their own narrower job — the coercion contract itself."""

    def test_both_shipped_transition_labels_parse_to_their_real_end_state(self):
        self.assertEqual(_transition_end("0->1"), 1)  # submit
        self.assertEqual(_transition_end("1->2"), 2)  # cancel

    def test_an_unparseable_label_returns_none_rather_than_raising(self):
        for label in ("draft->submitted", "0->", "nonsense", "", None):
            with self.subTest(label=label):
                self.assertIsNone(_transition_end(label))

    def test_only_the_right_hand_side_is_read(self):
        # ``"->1"`` DOES parse, to 1: the parser reads only what follows the arrow and never
        # validates the start state. Recorded because it looks unparseable and is not — a fixture
        # asserting None here was wrong about the code, not the other way round.
        self.assertEqual(_transition_end("->1"), 1)

    def test_a_multi_digit_end_state_is_TRUNCATED_not_refused(self):
        # ``[:1]`` takes one character, so "0->10" reads as 1 — a WRONG expected docstatus rather
        # than a skipped check, which is the more dangerous of the two failure modes: the gate
        # runs and compares against a number nobody chose. Harmless today (docstatus is only ever
        # 0/1/2, so no shipped label has a multi-digit end state) and pinned so it stays a known
        # limitation rather than a surprise.
        self.assertEqual(_transition_end("0->10"), 1)

    def test_a_workflow_style_label_parses_to_none_which_is_why_workflow_does_not_use_this(self):
        # The near miss worth naming: `request_workflow_transition` builds labels shaped
        # `state:Draft->Pending Approval` onto the SAME intent-receipt `transition` field, and
        # those parse to None here. They never reach this function — that tool writes its
        # intent/outcome receipts directly and never calls the spine (verified: tools.py is the
        # only module importing pacioli.spine, with one call site). If it were ever routed through
        # the spine, E1 would be silently off for every workflow transition.
        self.assertIsNone(_transition_end("state:Draft->Pending Approval"))

    def test_none_means_the_confirmation_check_is_skipped_which_is_why_it_is_guarded(self):
        # The consequence, pinned rather than argued: with an unparseable transition the docstatus
        # is never checked, so a response that does NOT show the transition still records as a
        # clean committed success. This is the trapdoor the test above exists to keep shut.
        fx = FakeEffects(execute_result={"docstatus": 0, "name": "NOT-SUBMITTED"})
        res = governed_submit(
            plan=PLAN, marker=MARKER, token=TOKEN, current_doc_version=DOC_VERSION,
            now_epoch=NOW_EPOCH, now_date=NOW_DATE, locks={}, effects=fx,
            op="submit", transition="draft->submitted",
        )
        self.assertTrue(res.ok)  # docstatus 0 accepted as success — because E1 was switched off
        self.assertEqual(fx.outcome_calls[0][0], "committed")


if __name__ == "__main__":
    unittest.main()
