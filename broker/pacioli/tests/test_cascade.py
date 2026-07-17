import unittest
from pacioli.cascade import build_cascade, run_cascade
from pacioli.consent import new_marker
from pacioli.plan import new_plan

def _meta(doctype, docname):
    return {"doc_version": f"v-{docname}", "posting_date": "2026-07-03",
            "company": "X", "projected_gl": [["2026-07-03", "Acc", 1.0, ""]]}

def _fetcher(edges):
    # edges: {docname: [dependent docnames]} ; all docs are ("SI"/"PE") by a name->doctype map
    def fetch(doctype, docname):
        return [{"doctype": DT[d], "docname": d} for d in edges.get(docname, [])]
    return fetch

DT = {"T": "Sales Invoice", "P": "Payment Entry", "R": "Journal Entry"}

class BuildCascadeTest(unittest.TestCase):
    def test_leaf_target_is_only_node(self):
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher({}), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=25)
        self.assertTrue(r["ok"])
        self.assertEqual([n["docname"] for n in r["graph"]], ["T"])
        self.assertEqual(r["graph"][0]["coverage"], "modeled")

    def test_dependent_cancelled_before_target(self):
        # P links to T ; T is cancelled LAST
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher({"T": ["P"]}), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=25)
        self.assertTrue(r["ok"])
        self.assertEqual([n["docname"] for n in r["graph"]], ["P", "T"])
        # P is not in supported set -> generic coverage
        self.assertEqual(r["graph"][0]["coverage"], "generic")

    def test_transitive_chain_deepest_first(self):
        # R -> P -> T (R links to P, P links to T) ; order R, P, T
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher({"T": ["P"], "P": ["R"]}), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=25)
        self.assertEqual([n["docname"] for n in r["graph"]], ["R", "P", "T"])

    def test_cycle_refuses(self):
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher({"T": ["P"], "P": ["T"]}), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=25)
        self.assertFalse(r["ok"])
        self.assertIn("cycle", r["reason"].lower())

    def test_cap_exceeded_refuses_naming_count(self):
        edges = {"T": ["P", "R"]}
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher(edges), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=2)
        self.assertFalse(r["ok"])
        self.assertIn("3", r["reason"])  # T + P + R = 3 > cap 2

    def test_cap_exactly_met_succeeds(self):
        # F-V1 boundary: a graph of EXACTLY max_nodes must succeed — the cap refuses only when
        # the count exceeds it, never when it merely meets it (equal-or-stricter than ERPNext's
        # own row caps, not an off-by-one over-refusal at the boundary).
        edges = {"T": ["P", "R"]}
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=_fetcher(edges), node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=3)
        self.assertTrue(r["ok"])
        self.assertEqual({n["docname"] for n in r["graph"]}, {"T", "P", "R"})
        self.assertEqual(len(r["graph"]), 3)
        self.assertEqual(r["graph"][-1]["docname"], "T")  # target still cancelled last

    def test_unreadable_graph_refuses(self):
        def boom(doctype, docname):
            raise RuntimeError("linked-docs unreadable")
        r = build_cascade({"doctype": "Sales Invoice", "docname": "T"},
                          fetch_linked=boom, node_meta=_meta,
                          supported_doctypes={"Sales Invoice"}, max_nodes=25)
        self.assertFalse(r["ok"])
        self.assertIn("could not read", r["reason"].lower())


class AnsweredError(Exception):
    """A stand-in for an answered ``ErpnextError`` — carries ``answered=True`` exactly like the
    real transport-taxonomy exception. ``cascade.py`` only ever duck-types ``getattr(exc,
    "answered", False)``, so a plain exception with the attribute pins the branch without this
    bench-free pure-core test module importing ``pacioli.erpnext``."""

    def __init__(self, message):
        super().__init__(message)
        self.answered = True


class FakeEffects:
    def __init__(self, versions, locks=None, fail_on=None, unconfirmed_on=None,
                answered_fail_on=None, readback_by=None, readback_fail_on=None, claim_ok=True,
                intent_raise_on=None, outcome_raise_times=0, outcome_raises=None):
        self.versions = versions        # {docname: modified}
        self.locks = locks or {}
        self.claim_ok = claim_ok        # False -> lost the CAS race (a concurrent cascade claimed it)
        self.fail_on = fail_on          # docname whose cancel raises a RAW (no-answer) exception
        self.answered_fail_on = answered_fail_on  # docname whose cancel raises an ANSWERED exception
        self.unconfirmed_on = unconfirmed_on  # docname whose cancel "succeeds" but reports a
                                               # stale (queued-shape) docstatus, never 2
        self.readback_by = readback_by or {}  # docname -> docstatus the no-answer readback shows
        self.readback_fail_on = readback_fail_on  # docname whose readback itself raises
        self.readback_calls = []        # docnames the no-answer readback was attempted for
        # WG-2b: the residual named in the 2026-07-10 readiness audit — an unexpected exception
        # from the STORE's own persistence layer (record_intent/record_outcome), not the bench.
        self.intent_raise_on = intent_raise_on          # docname whose record_intent call raises
        self.outcome_raise_times = outcome_raise_times  # first N record_outcome calls raise
        self.outcome_raises = outcome_raises or RuntimeError("store write failed")
        self._outcome_call_count = 0
        self.claimed = False
        self.cancelled = []
        self.lock_calls = []            # (company, doctype, posting_date) per locks_for call
        self.receipts = []              # (kind, body/status)
        self.outcome_results = []       # result dicts as durably recorded (receipt-honesty pins)
        self.final_marker = "unset"     # the settle passed on the terminal outcome
    def claim_marker(self, reserved):
        self.claimed = True; return self.claim_ok
    def current_version(self, dt, name):
        return self.versions[name]
    def locks_for(self, company, doctype, posting_date):
        # F-S1: locks_for grew doctype/posting_date params (cascade.run_cascade now passes the
        # SAME node["doctype"]/node["posting_date"] it already used for that node's check_fresh/
        # check_red_line call) — the fake stays doctype-blind (self.locks is fixed per test) but
        # RECORDS the call triple: all three params are plain strings, so an argument-order swap
        # at the call site would be a silent no-match -> allow; the recording is what catches it.
        self.lock_calls.append((company, doctype, posting_date))
        return self.locks
    def record_intent(self, body):
        if body.get("docname") == self.intent_raise_on:
            raise RuntimeError(f"could not durably record intent for {body.get('docname')}")
        self.receipts.append(("intent", body)); return {"seq": len(self.receipts)}
    def cancel(self, dt, name):
        if name == self.answered_fail_on:
            raise AnsweredError("HTTP 417: LinkExistsError")
        if name == self.fail_on:
            raise RuntimeError("ERPNext blocked the cancel")
        self.cancelled.append(name)
        if name == self.unconfirmed_on:
            return {"name": name, "docstatus": 1}  # queued — unchanged from submitted
        return {"name": name, "docstatus": 2}
    def readback(self, dt, name):
        # The no-answer/ambiguous branch's governed readback — a thin seam (like the real one
        # wired to client.get_document), so it can raise; the pure core owns never letting that
        # raise escape (mirrors how it already owns `cancel`'s raise).
        self.readback_calls.append(name)
        if name == self.readback_fail_on:
            raise RuntimeError("readback also failed")
        return self.readback_by.get(name)
    def record_outcome(self, intent, status, result, final_marker):
        self._outcome_call_count += 1
        if self._outcome_call_count <= self.outcome_raise_times:
            raise self.outcome_raises
        self.receipts.append(("outcome", status))
        self.outcome_results.append(result)  # the durable receipt body — tests assert honesty here
        if final_marker != "unset" and final_marker is not None:
            self.final_marker = getattr(final_marker, "state", final_marker)

def _cplan(graph):
    return new_plan(plan_id="c1", target="t", doc_version=graph[-1]["doc_version"],
                    posting_date="2026-07-03", docname=graph[-1]["docname"],
                    op="cascade_cancel", doctype=graph[-1]["doctype"], graph=graph)

def _graph(names_dt_ver):
    return [{"doctype": dt, "docname": n, "doc_version": v, "posting_date": "2026-07-03",
             "company": "X", "coverage": "generic", "projected_gl": []}
            for (n, dt, v) in names_dt_ver]

class RunCascadeTest(unittest.TestCase):
    def _marker(self):
        tok = "m" * 32
        return new_marker(token=tok, plan_id="c1", expires_at=10_000.0), tok

    def test_full_cascade_all_cancelled_marker_committed(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertTrue(r["ok"])
        self.assertEqual(eff.cancelled, ["P", "T"])
        # F-S1: the per-node lock read got the node's own (company, doctype, posting_date), in
        # that order — distinct strings per position, so a swapped-argument regression fails here.
        self.assertEqual(eff.lock_calls, [("X", "Payment Entry", "2026-07-03"),
                                          ("X", "Sales Invoice", "2026-07-03")])
        self.assertEqual([n["docname"] for n in r["cancelled"]], ["P", "T"])
        self.assertIsNone(r["stopped_at"])
        self.assertEqual(r["total"], 2)
        self.assertEqual(eff.final_marker, "consumed")  # committed

    def test_closed_books_lock_refuses_before_any_cancel(self):
        # README: "the same closed-books check blocks unwinding into a locked period." The per-node
        # red_line belt fires at preflight — previously UNTESTED through cascade's wiring (every
        # run_cascade test used locks={} and posting_date == now_date). Node posting 2026-07-03 sits
        # inside a period closed through 2026-07-31.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"},
                          locks={"closed_period_until": "2026-07-31"})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "red_line")
        self.assertIn("locked period", r["stopped_at"]["reason"].lower())
        self.assertEqual(eff.cancelled, [])       # refused BEFORE any cancel
        self.assertFalse(eff.claimed)             # marker never claimed
        self.assertEqual(eff.receipts, [])        # clean no-op — nothing recorded

    def test_future_dated_node_with_live_lock_refuses_catching_arg_swap(self):
        # A node dated in the FUTURE while a lock is live is the silent-escape vector check_red_line
        # guards. It also pins the (posting_date, now_date) ORDER at the cascade call site: swap them
        # and this future-dating refusal would not fire (07-03 is not > 08-15).
        g = [{"doctype": "Sales Invoice", "docname": "T", "doc_version": "vT",
              "posting_date": "2026-08-15", "company": "X", "coverage": "generic", "projected_gl": []}]
        eff = FakeEffects(versions={"T": "vT"}, locks={"closed_period_until": "2026-06-30"})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "red_line")
        self.assertIn("future", r["stopped_at"]["reason"].lower())
        self.assertEqual(eff.cancelled, [])

    def test_concurrent_marker_cas_loss_refuses_nothing_recorded(self):
        # The CAS-claim can lose to a concurrent cascade (claim_marker -> False). The single-op spine
        # pins this; the cascade side was unexercised. A lost claim refuses at consent and records
        # NOTHING irreversible — the marker was never actually claimed for this run.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, claim_ok=False)
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "consent")
        self.assertIn("concurrent", r["reason"].lower())
        self.assertTrue(eff.claimed)            # the claim WAS attempted
        self.assertEqual(eff.cancelled, [])     # nothing cancelled
        self.assertEqual(eff.receipts, [])      # nothing recorded
        self.assertEqual(eff.final_marker, "unset")

    def test_second_node_stale_refuses_before_any_cancel_preflight_is_all_or_nothing(self):
        # Preflight checks EVERY node before the execute loop starts. Node 0 (P) is fresh and clean;
        # only node 1 (T) drifted. The whole run must refuse at preflight with ZERO cancels — proving
        # preflight is all-before-execute, not interleaved (which would cancel P before checking T).
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "CHANGED"})  # only the SECOND node drifted
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "fresh")
        self.assertEqual(r["stopped_at"]["docname"], "T")   # node 1, not node 0
        self.assertEqual(r["stopped_at"]["seq"], 1)
        self.assertEqual(eff.cancelled, [])                 # P NOT cancelled — all-or-nothing
        self.assertFalse(eff.claimed)
        self.assertEqual(eff.receipts, [])

    def test_answered_error_midway_commits_due_to_progress(self):
        # ANSWERED refusal (byte-identical to pre-taxonomy behavior): the bench definitely saw and
        # refused the cancel, so the existing progress-based rule stands — >=1 prior cancel ->
        # commit. This is the sibling that keeps the answered branch pinned exactly as it was.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, answered_fail_on="T")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, ["P"])            # T never cancelled
        self.assertEqual(r["stopped_at"]["docname"], "T")
        self.assertEqual(eff.final_marker, "consumed")     # >=1 progress -> committed
        self.assertEqual(eff.readback_calls, [])           # answered branch never reads back
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed", "failed"])  # P committed normally, T failed

    def test_answered_error_first_node_releases(self):
        # ANSWERED refusal, byte-identical: 0 progress on a first-node answered error still
        # releases — the redteam property is "never ADD a release path", not "never have one".
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, answered_fail_on="P")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, [])
        self.assertEqual(r["stopped_at"]["docname"], "P")
        self.assertEqual(eff.final_marker, "live")         # 0 progress -> released
        self.assertEqual(eff.readback_calls, [])           # answered branch never reads back

    def test_no_answer_exception_midway_commits_and_attempts_readback(self):
        # THE FLIP's non-first-node sibling: a RAW, unconverted exception (no `answered` attribute
        # — the exact residual shape) is "no answer", not an answered refusal. It commits — as
        # before, but now for a DIFFERENT reason (always, never "because of progress") — and
        # attempts a governed readback. No readback_by entry for "T" -> None != 2 -> unconfirmed.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, fail_on="T")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, ["P"])            # T never confirmed cancelled
        self.assertEqual(r["stopped_at"]["docname"], "T")
        self.assertEqual(eff.final_marker, "consumed")     # ALWAYS committed on no-answer now
        self.assertEqual(eff.readback_calls, ["T"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed", "unconfirmed"])

    def test_no_answer_exception_first_node_now_commits_not_released(self):
        # THE FLIP (deliberate, deny-biased, CHANGELOG-worthy): this test used to be
        # `test_first_node_fail_marker_released`, pinning that a first-node RuntimeError released
        # the marker on the never-verified "no progress" assumption. Under the new taxonomy an
        # unconverted exception is "no answer" — the cancel may already be in motion server-side —
        # so it ALWAYS commits (never releases), regardless of node position, and resolves via
        # readback exactly like the midway case above.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, fail_on="P")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, [])
        self.assertEqual(r["stopped_at"]["docname"], "P")
        self.assertEqual(eff.final_marker, "consumed")     # NOT released — the flip
        self.assertEqual(eff.readback_calls, ["P"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])


class RunCascadeNoAnswerReadbackTest(unittest.TestCase):
    """The full no-answer/ambiguous resolution table (docs/plans/2026-07-07-transport-taxonomy.md),
    exercised at the cascade level: an unconverted exception from ``effects.cancel`` always spends
    the marker and resolves via ``effects.readback`` — confirmed/unconfirmed/readback-failed."""

    def _marker(self):
        tok = "m" * 32
        return new_marker(token=tok, plan_id="c1", expires_at=10_000.0), tok

    def test_readback_confirms_cancelled_node_recorded_but_run_still_fail_stops(self):
        # DESIGN CHOICE (see cascade.py's own comment at this branch): even though the readback
        # CONFIRMS the node actually cancelled, the run still fail-stops here rather than resuming
        # the loop — the raised exception interrupted normal control flow, and resuming past an
        # exceptional path is not the same guarantee as the ordinary per-node continue. The
        # confirmed node IS recorded honestly in `cancelled`, with `confirmed_via` in its outcome.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, fail_on="P", readback_by={"P": 2})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])                          # still fail-stops
        self.assertEqual([n["docname"] for n in r["cancelled"]], ["P"])  # confirmed, recorded
        self.assertEqual(r["stopped_at"]["docname"], "P")
        self.assertNotIn("T", eff.cancelled)                # T never attempted
        self.assertEqual(eff.final_marker, "consumed")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed"])
        # Receipt honesty (redteam catch): the durable receipt must carry WHAT failed, not just
        # that a readback later confirmed it — an auditor reads this months on, without the code.
        self.assertEqual(eff.outcome_results[0]["error"], "ERPNext blocked the cancel")
        self.assertEqual(eff.outcome_results[0]["confirmed_via"], "post_failure_readback")

    def test_readback_mismatch_is_unconfirmed_node_not_counted_cancelled(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, fail_on="P", readback_by={"P": 1})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["cancelled"], [])                # NOT counted — readback didn't confirm
        self.assertEqual(eff.final_marker, "consumed")
        self.assertIn("unconfirmed", r["stopped_at"]["reason"].lower())

    def test_readback_itself_raises_degrades_to_unconfirmed_with_readback_error(self):
        # The readback must NEVER be allowed to crash this flow (mirrors the existing E3 CRITICAL
        # fix for the docstatus-missing readback in _Effects.cancel) — a failed readback degrades
        # to `readback_error` on the result, marker still spent, never released-in-flight.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, fail_on="P", readback_fail_on="P")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["cancelled"], [])
        self.assertEqual(eff.final_marker, "consumed")
        self.assertIn("readback also failed", r["stopped_at"]["reason"])
        outcomes = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(outcomes, ["unconfirmed"])

    def test_stale_node_version_refuses_before_any_cancel(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "CHANGED", "T": "vT"})  # P drifted since plan
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, [])
        self.assertEqual(r["stage"], "fresh")

    def test_empty_graph_refuses_marker_never_claimed(self):
        plan = new_plan(plan_id="c1", target="t", doc_version="", posting_date="2026-07-03",
                        docname="T", op="cascade_cancel", doctype="Sales Invoice", graph=[])
        eff = FakeEffects(versions={})
        m, tok = self._marker()
        r = run_cascade(plan=plan, marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "plan")
        self.assertEqual(r["cancelled"], [])
        self.assertEqual(r["total"], 0)
        self.assertFalse(eff.claimed)   # marker never claimed — no consent to spend on nothing


class RunCascadeUnconfirmedTest(unittest.TestCase):
    """Gap A (envelope E3): a node's cancel returning without exception is NOT proof the
    transition happened — ERPNext can queue a cancel (the exact E1 shape, spine.py's
    `governed_submit`) and answer 200 with the doc still at its pre-transition docstatus. The
    cascade loop must confirm docstatus 2 per node before recording `committed`, exactly like the
    single-op spine does for its one transition."""

    def _marker(self):
        tok = "m" * 32
        return new_marker(token=tok, plan_id="c1", expires_at=10_000.0), tok

    def test_unconfirmed_first_node_stops_cascade_and_is_not_counted_cancelled(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, unconfirmed_on="P")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "execute")
        self.assertEqual(r["stopped_at"]["docname"], "P")
        self.assertIn("unconfirmed", r["stopped_at"]["reason"].lower())
        self.assertEqual(r["cancelled"], [])            # P attempted but never confirmed
        self.assertEqual(eff.cancelled, ["P"])           # the cancel WAS attempted at the bench

    def test_unconfirmed_node_records_outcome_unconfirmed_not_committed(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, unconfirmed_on="P")
        m, tok = self._marker()
        run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                    now_date="2026-07-03", effects=eff)
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])

    def test_unconfirmed_first_node_still_settles_marker_committed(self):
        # Deny-biased extension of the progress-based settle rule: an unconfirmed node means an
        # act MAY have been initiated server-side even though it is the very first node in the
        # graph (cancelled == [] so far) — releasing the marker here would let one grant initiate
        # a second act, the exact E1 rule (spine.py, CHANGELOG 0.9.1).
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, unconfirmed_on="P")
        m, tok = self._marker()
        run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                    now_date="2026-07-03", effects=eff)
        self.assertEqual(eff.final_marker, "consumed")

    def test_unconfirmed_node_leaves_later_nodes_untouched(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, unconfirmed_on="P")
        m, tok = self._marker()
        run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                    now_date="2026-07-03", effects=eff)
        self.assertNotIn("T", eff.cancelled)

    def test_unconfirmed_terminal_node_after_prior_confirmed_stays_committed(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, unconfirmed_on="T")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual([n["docname"] for n in r["cancelled"]], ["P"])
        self.assertEqual(eff.final_marker, "consumed")

    def test_happy_path_confirmed_every_node_still_all_committed(self):
        # Regression: the existing FakeEffects.cancel already returns docstatus 2 for every node,
        # so the new confirm-check must not change the happy path's outcome shape.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"})
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertTrue(r["ok"], r)
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed", "committed"])


class RunCascadePostClaimExceptionRobustnessTest(unittest.TestCase):
    """WG-2b: the residual named in the 2026-07-10 readiness audit — "a post-claim exception still
    strands the marker in `reserved` (dispatch's catch is narrow) ... applies to spine/cascade
    too". Mirrors ``TestPostClaimExceptionRobustness`` in ``test_spine.py``: an unexpected
    exception from the STORE's own persistence layer (``record_intent``/``record_outcome``), not
    the bench transport, must never crash past a structured result and must never let the marker
    become spendable again on an unknown failure."""

    def _marker(self):
        tok = "m" * 32
        return new_marker(token=tok, plan_id="c1", expires_at=10_000.0), tok

    def test_intent_recording_failure_stops_cascade_structured_no_crash(self):
        # Post-claim, pre-wire, for node P: claim_marker already won, but record_intent for P
        # raises before effects.cancel is ever reached — nothing sent to the bench for this node.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, intent_raise_on="P")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)  # must not raise
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "execute")
        self.assertEqual(r["stopped_at"]["docname"], "P")
        self.assertEqual(eff.cancelled, [])          # nothing ever sent to the bench
        self.assertEqual(eff.receipts, [])           # no intent, no outcome — nothing to link one to

    def test_intent_recording_failure_leaves_marker_unspendable(self):
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, intent_raise_on="P")
        m, tok = self._marker()
        run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                    now_date="2026-07-03", effects=eff)
        self.assertTrue(eff.claimed)          # the claim WAS attempted and won
        # No record_outcome call ever happened — store-side the marker remains exactly what
        # claim_marker left it: reserved, dead, not spendable.
        self.assertEqual(eff.final_marker, "unset")

    def test_intent_recording_failure_midway_after_prior_progress(self):
        # The intent-recording failure hits the SECOND node, after the first genuinely cancelled.
        # Real-world progress already happened for P — the run must still stop cleanly (never
        # crash), naming T as the stopping point, with P's own outcome already durably recorded.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, intent_raise_on="T")
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)  # must not raise
        self.assertFalse(r["ok"])
        self.assertEqual(eff.cancelled, ["P"])
        self.assertEqual(r["stopped_at"]["docname"], "T")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed"])  # P's own outcome, recorded before T's failure

    def test_outcome_recording_failure_after_confirmed_cancel_recovers_degraded(self):
        # Post-wire: P's cancel succeeds and confirms (docstatus 2), but the FIRST attempt to
        # durably record its "committed" outcome raises. The pure core must never silently drop
        # this — it retries with a sanitized, deny-biased "unconfirmed" record instead of crashing
        # or claiming a clean "committed" through a write that itself just failed.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, outcome_raise_times=1,
                          outcome_raises=ValueError("result.amount: non-finite float (nan)"))
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)  # must not raise
        # Both nodes genuinely cancelled at the bench...
        self.assertEqual(eff.cancelled, ["P", "T"])
        # ...but P's outcome-recording degraded, so the overall claim can no longer be a clean
        # "ok: True" — the caller must reconcile before treating this as fully closed.
        self.assertFalse(r["ok"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses[0], "unconfirmed")  # P's degraded record — never silently "committed"
        self.assertEqual(statuses[1], "committed")    # T's own recording was unaffected

    def test_outcome_recording_double_failure_never_crashes(self):
        # Even the sanitized retry fails (a genuinely unrecoverable store) — must still return a
        # structured result, never raise past run_cascade.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, outcome_raise_times=99,
                          outcome_raises=RuntimeError("store unreachable"))
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)  # must not raise
        self.assertFalse(r["ok"])
        # the intent WAS durably recorded (that call never fails here) — only the outcome, whose
        # every attempt (original + sanitized retry) raised, never lands.
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, [])

    def test_answered_failure_outcome_recording_failure_preserves_failed_status(self):
        # Pre-wire truth (an ANSWERED refusal on the FIRST node — nothing landed) must be
        # preserved through the degrade retry: "failed" never gets silently promoted, and the
        # marker's release (this branch's existing, correct behavior for a known-clean refusal on
        # a first node with zero progress) is unaffected by the recording hiccup.
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, answered_fail_on="P",
                          outcome_raise_times=1, outcome_raises=ValueError("boom"))
        m, tok = self._marker()
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)
        self.assertFalse(r["ok"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["failed"])       # preserved, not upgraded to unconfirmed
        self.assertEqual(eff.final_marker, "live")   # 0 progress, known-clean refusal -> released

    def test_terminal_committed_outcome_recording_failure_degrades_overall_result(self):
        # The LAST node's own settle is what flips the marker to committed AND is the only call
        # site whose success feeds the top-level `{"ok": True, ..., "stage": "done"}` return with
        # no other per-node check in between — this is the one place a silent degrade could slip
        # through as a false "ok: True".
        g = _graph([("P", "Payment Entry", "vP"), ("T", "Sales Invoice", "vT")])
        eff = FakeEffects(versions={"P": "vP", "T": "vT"}, outcome_raise_times=1,
                          outcome_raises=ValueError("boom"))
        eff.outcome_raise_times = 0  # let P's outcome record cleanly...
        m, tok = self._marker()
        # ...then force ONLY T's (the terminal) outcome-recording to fail, by raising on the
        # second record_outcome call rather than the first.
        real_record_outcome = eff.record_outcome
        calls = {"n": 0}
        def flaky_record_outcome(intent, status, result, final_marker):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("result.docstatus: non-finite float (nan)")
            return real_record_outcome(intent, status, result, final_marker)
        eff.record_outcome = flaky_record_outcome
        r = run_cascade(plan=_cplan(g), marker=m, token=tok, now_epoch=1.0,
                        now_date="2026-07-03", effects=eff)  # must not raise
        self.assertEqual(eff.cancelled, ["P", "T"])   # both genuinely cancelled
        self.assertFalse(r["ok"])                     # never a false "done" through a degraded ledger
        self.assertEqual(eff.final_marker, "consumed")  # still spent — real progress happened
