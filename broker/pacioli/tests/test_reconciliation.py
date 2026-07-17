# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The Close — Half 2, the Reconciliation (docs/plans/2026-07-09-the-close.md).

Pure-core tests, bench-free: build_reconciliation joins governed acts (receipts, constructed
directly) against a movement snapshot (the read layer's frozen contract) and sorts every GL voucher
into governed / ungoverned / second-generation. Adversarial by design: no row may be silently
dropped, the ungoverned bucket carries no verdict language, unparseable/off-tolerance times are
never quietly treated as governed, and an unreadable posture refuses to call the close complete."""
import unittest

from pacioli.prove import GENESIS, Receipt
from pacioli.reconciliation import build_reconciliation, render_reconciliation


# --- receipt builders ------------------------------------------------------------------------

def _intent(seq, body, ts="2026-07-01T10:00:00Z"):
    return Receipt(seq=seq, prev_hash=GENESIS, kind="intent", body=body, ts=ts, hmac=f"h{seq}")


def _outcome(seq, finalizes, *, status="committed", result=None, ts="2026-07-01T10:00:01Z"):
    body = {"finalizes": finalizes, "status": status}
    if result is not None:
        body["result"] = result
    return Receipt(seq=seq, prev_hash=GENESIS, kind="outcome", body=body, ts=ts, hmac=f"h{seq}")


def _submit_intent(seq, docname, *, doctype="Sales Invoice", tool="submit", transition="0->1",
                   target="bench"):
    return _intent(seq, {"tool": tool, "target": target, "plan_id": "p1", "docname": docname,
                         "doctype": doctype, "doc_version": "v1", "transition": transition})


def _committed_result(name, *, doctype="Sales Invoice", docstatus=1,
                      modified="2026-07-01 10:00:00.500000"):
    return {"name": name, "docstatus": docstatus, "modified": modified, "doctype": doctype}


def _gl_row(voucher_type, voucher_no, *, account="Debtors - X", debit=0.0, credit=0.0,
            posting_date="2026-07-01", creation="2026-07-01 10:00:00.500000",
            owner="seat@x", modified="2026-07-01 10:00:00.500000", modified_by="seat@x",
            is_cancelled=0, party_type=None, party=None):
    return {"voucher_type": voucher_type, "voucher_no": voucher_no, "account": account,
            "debit": debit, "credit": credit, "posting_date": posting_date, "creation": creation,
            "owner": owner, "modified": modified, "modified_by": modified_by,
            "is_cancelled": is_cancelled, "party_type": party_type, "party": party}


def _snapshot(gl_rows=None, reposts=None, immutable=True, delete_linked=False):
    return {"gl_rows": gl_rows or [], "reposts": reposts or [],
            "posture": {"enable_immutable_ledger": immutable,
                        "delete_linked_ledger_entries": delete_linked}}


# --- the fully-governed period ---------------------------------------------------------------

class TestFullyGoverned(unittest.TestCase):
    def _one_committed_submit(self, **row_kw):
        receipts = [
            _submit_intent(0, "SI-1"),
            _outcome(1, 0, result=_committed_result("SI-1")),
        ]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", debit=100.0, **row_kw),
                          _gl_row("Sales Invoice", "SI-1", credit=100.0, **row_kw)])
        return receipts, snap

    def test_all_rows_governed_ungoverned_empty(self):
        receipts, snap = self._one_committed_submit()
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["summary"]["ungoverned"], 0)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 0)
        self.assertEqual(r["ungoverned"], [])
        g = r["governed"][0]
        self.assertEqual((g["voucher_type"], g["voucher_no"]), ("Sales Invoice", "SI-1"))
        self.assertEqual(g["doctype"], "Sales Invoice")
        self.assertEqual(g["docname"], "SI-1")
        self.assertEqual(g["receipt_seq"], 0)
        self.assertEqual(g["transition"], "0->1")
        self.assertEqual(g["gl_row_count"], 2)

    def test_complete_true_when_posture_readable_and_consistent(self):
        receipts, snap = self._one_committed_submit()
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertTrue(r["complete"])

    def test_only_committed_intent_governs(self):
        # a FAILED outcome does not finalize — the voucher is ungoverned, no verdict.
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, status="failed")]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["ungoverned"], 1)

    def test_orphan_intent_does_not_govern(self):
        # an intent with no outcome at all cannot corroborate movement.
        receipts = [_submit_intent(0, "SI-1")]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["ungoverned"], 1)


# --- a desk posting: ungoverned, NO verdict --------------------------------------------------

class TestUngovernedDeskPosting(unittest.TestCase):
    def test_desk_voucher_is_ungoverned_with_no_verdict(self):
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([
            _gl_row("Sales Invoice", "SI-1", debit=100.0),  # governed
            _gl_row("Journal Entry", "JE-DESK", debit=50.0, owner="clerk@x"),  # desk, ungoverned
        ])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["summary"]["ungoverned"], 1)
        u = r["ungoverned"][0]
        self.assertEqual((u["voucher_type"], u["voucher_no"]), ("Journal Entry", "JE-DESK"))
        self.assertEqual(u["owner"], "clerk@x")
        self.assertIn("did not pass through Pacioli", u["note"])

    def test_render_never_accuses_ungoverned(self):
        snap = _snapshot([_gl_row("Journal Entry", "JE-DESK", owner="clerk@x")])
        text = render_reconciliation(build_reconciliation([], snap, target="bench")).lower()
        for word in ("unauthorized", "breach", "intrusion", "attack", "malicious", "illegal",
                     "violation", "suspicious", "illegitimate", "rogue"):
            self.assertNotIn(word, text)
        self.assertIn("did not pass through pacioli", text)


# --- Fork IV: a repost = second generation under a governed voucher --------------------------

class TestSecondGeneration(unittest.TestCase):
    def _governed_with_repost_row(self, reposts):
        # SI-1 governed at 10:00:00.5; a second row created much later (a repost), off tolerance.
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([
            _gl_row("Sales Invoice", "SI-1", debit=100.0, creation="2026-07-01 10:00:00.500000"),
            _gl_row("Sales Invoice", "SI-1", credit=100.0, creation="2026-07-05 08:00:00.000000"),
        ], reposts=reposts)
        return receipts, snap

    def test_repost_attributed_when_named(self):
        reposts = [{"name": "REPOST-1", "creation": "2026-07-05 08:00:00.000000", "owner": "admin@x",
                    "docstatus": 1, "vouchers": [{"voucher_type": "Sales Invoice",
                                                  "voucher_no": "SI-1"}]}]
        receipts, snap = self._governed_with_repost_row(reposts)
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 1)
        self.assertEqual(r["summary"]["governed"], 0)
        gen = r["governed_ungoverned_generation"][0]
        self.assertEqual(gen["repost_ref"], "REPOST-1")
        self.assertEqual(gen["doctype"], "Sales Invoice")
        self.assertEqual(gen["gl_row_count"], 2)
        self.assertEqual(gen["ungoverned_creation"], "2026-07-05 08:00:00.000000")
        self.assertTrue(gen["governed_transitions"])  # the act that DID touch it is carried
        self.assertEqual(r["reposts_in_window"][0]["name"], "REPOST-1")
        self.assertEqual(r["reposts_in_window"][0]["voucher_count"], 1)

    def test_repost_ref_none_when_no_reposts(self):
        receipts, snap = self._governed_with_repost_row(reposts=[])
        r = build_reconciliation(receipts, snap, target="bench")
        gen = r["governed_ungoverned_generation"][0]
        self.assertIsNone(gen["repost_ref"])
        self.assertEqual(r["reposts_in_window"], [])


# --- the 5 governed body shapes each produce a correct match ---------------------------------

class TestFiveBodyShapes(unittest.TestCase):
    def test_submit(self):
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)

    def test_cancel(self):
        receipts = [_submit_intent(0, "SI-1", tool="cancel", transition="1->2"),
                    _outcome(1, 0, result=_committed_result("SI-1", docstatus=2))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", is_cancelled=1, credit=100.0)])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["governed"][0]["transition"], "1->2")

    def test_amend(self):
        body = {"tool": "amend", "target": "bench", "docname": "SI-1", "doctype": "Sales Invoice",
                "doc_version": "v1", "transition": "2->0(draft)"}
        receipts = [_intent(0, body), _outcome(1, 0, result=_committed_result("SI-1", docstatus=0))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["governed"][0]["transition"], "2->0(draft)")

    def test_workflow_transition(self):
        body = {"tool": "workflow_transition", "target": "bench", "docname": "SI-1",
                "doctype": "Sales Invoice", "doc_version": "v1", "transition": "state:Draft->Approved"}
        receipts = [_intent(0, body), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["governed"][0]["transition"], "state:Draft->Approved")

    def test_cascade_cancel_has_no_target_key(self):
        # cascade_cancel body carries NO `target` — identity is still doctype+docname.
        body = {"tool": "cascade_cancel", "plan_id": "p1", "cascade_id": "c1", "seq": 0,
                "doctype": "Sales Invoice", "docname": "SI-1", "coverage": "modeled",
                "transition": "1->2"}
        receipts = [_intent(0, body), _outcome(1, 0, result=_committed_result("SI-1", docstatus=2))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", is_cancelled=1)])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["governed"][0]["transition"], "1->2")

    def test_reconcile_does_not_govern_gl_rows(self):
        # A reconcile writes PLE, not GL (scout-b, erpnext version-16). The invoice's/payment's GL
        # rows came from their SUBMIT act, not the reconcile. A reconcile-ONLY receipt (the invoice
        # was submitted at the desk, then Pacioli settled it) must NOT mark those GL rows governed —
        # that is a false-clean (ungoverned desk movement reading as governed). They stay UNGOVERNED.
        body = {"tool": "reconcile", "target": "bench", "plan_id": "p1",
                "allocations": [{"payment_type": "Payment Entry", "payment_no": "PE-1",
                                 "invoice_type": "Sales Invoice", "invoice_no": "SI-1",
                                 "allocated_amount": 100.0}]}
        receipts = [_intent(0, body), _outcome(1, 0, result={"readback": "ok"})]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1"), _gl_row("Payment Entry", "PE-1")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["ungoverned"], 2)

    def test_reconcile_does_not_mask_a_repost_second_generation(self):
        # Invoice submitted AND reconciled via Pacioli, then reposted by someone. The submit governs
        # the original rows; the repost rows are off-tolerance from the submit and MUST surface as a
        # second generation — the reconcile's (former) structural match must not mask them.
        rec_body = {"tool": "reconcile", "target": "bench", "plan_id": "p2",
                    "allocations": [{"payment_type": "Payment Entry", "payment_no": "PE-1",
                                     "invoice_type": "Sales Invoice", "invoice_no": "SI-1",
                                     "allocated_amount": 100.0}]}
        receipts = [
            _submit_intent(0, "SI-1"),
            _outcome(1, 0, result=_committed_result("SI-1", modified="2026-07-01 10:00:00.500000")),
            _intent(2, rec_body), _outcome(3, 2, result={"readback": "ok"}),
        ]
        snap = _snapshot([
            _gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:00:00.500000"),   # submit-time
            _gl_row("Sales Invoice", "SI-1", creation="2026-07-20 09:00:00.000000",     # repost-time
                    owner="admin@x"),
        ])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 1)


# --- the time gate: structural match, tolerance boundary, unparseable ------------------------

class TestTimeGate(unittest.TestCase):
    def _receipts(self, modified):
        return [_submit_intent(0, "SI-1"),
                _outcome(1, 0, result=_committed_result("SI-1", modified=modified))]

    def test_expected_time_none_is_structural_match(self):
        # committed outcome with NO result -> expected_time None -> matches regardless of row time.
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=None)]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="1999-01-01 00:00:00.000000")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)

    def test_within_tolerance_exactly_at_boundary(self):
        receipts = self._receipts("2026-07-01 10:00:00.000000")
        # row created exactly 120s later -> at the boundary -> governed (<=).
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:02:00.000000")])
        r = build_reconciliation(receipts, snap, target="bench", tolerance_seconds=120)
        self.assertEqual(r["summary"]["governed"], 1)

    def test_just_over_tolerance_is_not_governed(self):
        receipts = self._receipts("2026-07-01 10:00:00.000000")
        # 121s later -> over the 120s tolerance -> NOT governed (second generation, has matching act).
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:02:01.000000")])
        r = build_reconciliation(receipts, snap, target="bench", tolerance_seconds=120)
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 1)

    def test_unparseable_creation_not_silently_governed(self):
        receipts = self._receipts("2026-07-01 10:00:00.000000")
        # a garbage creation stamp cannot corroborate -> deny-biased -> NOT governed, surfaced.
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="not-a-timestamp")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 1)

    def test_creation_without_microseconds_parses(self):
        receipts = self._receipts("2026-07-01 10:00:00")
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:00:30")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)

    def test_row_creation_never_compared_against_pacioli_ts(self):
        # The Pacioli .ts is a DIFFERENT clock. Here the outcome result has NO modified stamp, so
        # expected_time is None and the join is structural — the row's frappe creation is never
        # matched against any receipt .ts. A wildly-different row creation still governs.
        receipts = [_submit_intent(0, "SI-1", target="bench"),
                    _outcome(1, 0, result={"name": "SI-1", "docstatus": 1})]  # no `modified`
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2050-01-01 00:00:00.000000")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)


# --- seat_owner corroboration (Fork III) -----------------------------------------------------

class TestSeatOwner(unittest.TestCase):
    def test_owner_match_stays_governed(self):
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", owner="seat@x")])
        r = build_reconciliation(receipts, snap, target="bench", seat_owner="seat@x")
        self.assertEqual(r["summary"]["governed"], 1)

    def test_owner_mismatch_downgrades_to_generation(self):
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", owner="someone-else@x")])
        r = build_reconciliation(receipts, snap, target="bench", seat_owner="seat@x")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 1)
        gen = r["governed_ungoverned_generation"][0]
        self.assertEqual(gen["ungoverned_owner"], "someone-else@x")
        self.assertIn("owner", gen["note"])

    def test_no_seat_owner_ignores_owner(self):
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", owner="anyone@x")])
        r = build_reconciliation(receipts, snap, target="bench", seat_owner=None)
        self.assertEqual(r["summary"]["governed"], 1)


# --- is_cancelled reversing rows govern against the cancel transition ------------------------

class TestIsCancelled(unittest.TestCase):
    def test_cancel_reversing_rows_govern_against_cancel(self):
        # submit then cancel of SI-1; the snapshot holds the original rows AND the is_cancelled
        # reversing rows. Both submit and cancel are governed acts; every row lines up.
        receipts = [
            _submit_intent(0, "SI-1"),
            _outcome(1, 0, result=_committed_result("SI-1", modified="2026-07-01 10:00:00.000000")),
            _submit_intent(2, "SI-1", tool="cancel", transition="1->2"),
            _outcome(3, 2, result=_committed_result("SI-1", docstatus=2,
                                                    modified="2026-07-02 09:00:00.000000")),
        ]
        snap = _snapshot([
            _gl_row("Sales Invoice", "SI-1", debit=100.0, is_cancelled=0,
                    creation="2026-07-01 10:00:00.000000"),
            _gl_row("Sales Invoice", "SI-1", credit=100.0, is_cancelled=1,
                    creation="2026-07-02 09:00:30.000000"),  # reversal, near the cancel stamp
        ])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertEqual(r["summary"]["governed_ungoverned_generation"], 0)


# --- posture: an unreadable flag refuses the close -------------------------------------------

class TestPosture(unittest.TestCase):
    def test_immutable_none_makes_incomplete_and_flags(self):
        snap = _snapshot([], immutable=None)
        r = build_reconciliation([], snap, target="bench")
        self.assertFalse(r["complete"])
        self.assertTrue(any("enable_immutable_ledger is None" in f for f in r["flags"]))

    def test_delete_linked_none_makes_incomplete_and_flags(self):
        snap = _snapshot([], delete_linked=None)
        r = build_reconciliation([], snap, target="bench")
        self.assertFalse(r["complete"])
        self.assertTrue(any("delete_linked_ledger_entries is None" in f for f in r["flags"]))

    def test_posture_echoed(self):
        snap = _snapshot([], immutable=True, delete_linked=False)
        r = build_reconciliation([], snap, target="bench")
        self.assertEqual(r["posture"], {"enable_immutable_ledger": True,
                                        "delete_linked_ledger_entries": False})

    def test_ungoverned_movement_does_not_make_incomplete(self):
        # a desk posting is normal; the close is still COMPLETE (posture readable, consistent).
        snap = _snapshot([_gl_row("Journal Entry", "JE-DESK", owner="clerk@x")])
        r = build_reconciliation([], snap, target="bench")
        self.assertEqual(r["summary"]["ungoverned"], 1)
        self.assertTrue(r["complete"])

    def test_int_posture_normalizes_and_still_flags_danger(self):
        # ERPNext checkbox fields read back as int 0/1, not bool. The core must normalize so its
        # `is False`/`is True` identity checks still fire — a mutable ledger (0) + delete-linked (1)
        # is the DANGEROUS posture and MUST raise both tamper flags, not silently suppress them.
        snap = {"gl_rows": [], "reposts": [],
                "posture": {"enable_immutable_ledger": 0, "delete_linked_ledger_entries": 1}}
        r = build_reconciliation([], snap, target="bench")
        self.assertTrue(r["complete"])  # both readable (0/1 are not None)
        self.assertTrue(any("enable_immutable_ledger is off" in f for f in r["flags"]))
        self.assertTrue(any("delete_linked_ledger_entries is on" in f for f in r["flags"]))
        # the echoed posture is normalized to real bools, never leaks a raw int
        self.assertEqual(r["posture"], {"enable_immutable_ledger": False,
                                        "delete_linked_ledger_entries": True})

    def test_surprise_posture_type_is_unreadable_not_truthy(self):
        # A non-bool/non-int/non-None posture value (e.g. a stray string from a proxy) must be
        # treated as UNREADABLE (refuse), never truthy-coerced to a falsely-safe "on".
        snap = {"gl_rows": [], "reposts": [],
                "posture": {"enable_immutable_ledger": "0", "delete_linked_ledger_entries": False}}
        r = build_reconciliation([], snap, target="bench")
        self.assertFalse(r["complete"])
        self.assertIsNone(r["posture"]["enable_immutable_ledger"])


class TestStructuralOnlyGovernance(unittest.TestCase):
    def test_structural_only_governance_is_flagged(self):
        # A committed outcome with NO server `modified` stamp (the cascade readback-confirmed path,
        # cascade.py) governs STRUCTURALLY — no time gate can apply, so a second generation of the
        # voucher's rows cannot be distinguished. Stays governed (never over-accuses) but MUST flag
        # the blind spot so the operator knows the time gate was not applied.
        receipts = [_submit_intent(0, "SI-1"),
                    _outcome(1, 0, result={"name": "SI-1", "docstatus": 1})]  # no `modified`
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:00:00.000000")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertTrue(any("structural match only" in f for f in r["flags"]))

    def test_time_corroborated_governance_is_not_flagged(self):
        # When the outcome carries a real `modified` stamp and the row matches it in tolerance, the
        # time gate WAS applied — no structural-only flag.
        receipts = [_submit_intent(0, "SI-1"),
                    _outcome(1, 0, result=_committed_result("SI-1", modified="2026-07-01 10:00:00.000000"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1", creation="2026-07-01 10:00:30.000000")])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 1)
        self.assertFalse(any("structural match only" in f for f in r["flags"]))

    def test_none_identity_governing_transition_never_governs_a_row(self):
        # A committed governing intent whose body has neither doctype nor docname yields a
        # (None, None) transition; a coerced non-dict / None-identity GL row also collapses to the
        # (None, None) voucher key. The two must NOT structural-match into "governed" — an
        # identity-less row read as governed is the unsafe-direction lie (sec-C / F6).
        receipts = [_intent(0, {"tool": "submit"}),  # no doctype/docname
                    _outcome(1, 0, result={"name": None, "docstatus": 1})]
        snap = _snapshot([_gl_row(None, None)])
        r = build_reconciliation(receipts, snap, target="bench")
        self.assertEqual(r["summary"]["governed"], 0)
        self.assertEqual(r["summary"]["ungoverned"], 1)


# --- empty snapshot, determinism, and the no-dropped-row invariant ---------------------------

class TestEmptyAndInvariants(unittest.TestCase):
    def test_empty_snapshot_is_honest_empty(self):
        r = build_reconciliation([], _snapshot([]), target="bench")
        self.assertEqual(r["summary"]["gl_rows_total"], 0)
        self.assertEqual(r["summary"]["vouchers_total"], 0)
        self.assertEqual(r["governed"], [])
        self.assertEqual(r["ungoverned"], [])
        self.assertEqual(r["governed_ungoverned_generation"], [])
        self.assertTrue(r["complete"])

    def test_missing_snapshot_keys_do_not_crash(self):
        r = build_reconciliation([], {}, target="bench")  # no gl_rows/reposts/posture
        self.assertEqual(r["summary"]["gl_rows_total"], 0)
        self.assertFalse(r["complete"])  # posture unreadable (None)

    def test_no_row_is_ever_silently_dropped(self):
        # A mix of every bucket + a malformed (None-identity) row. Every row must land in exactly
        # one voucher bucket; the bucketed count must equal gl_rows_total.
        receipts = [
            _submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1")),
            _submit_intent(2, "SI-2"),
            _outcome(3, 2, result=_committed_result("SI-2", modified="2026-07-01 10:00:00.000000")),
        ]
        snap = _snapshot([
            _gl_row("Sales Invoice", "SI-1", debit=100.0),                 # governed
            _gl_row("Sales Invoice", "SI-1", credit=100.0),                # governed (same voucher)
            _gl_row("Journal Entry", "JE-DESK", owner="clerk@x"),          # ungoverned
            _gl_row("Sales Invoice", "SI-2", creation="2026-07-09 00:00:00.000000"),  # 2nd-gen
            _gl_row(None, None, owner="ghost@x"),                          # malformed -> ungoverned
        ])
        r = build_reconciliation(receipts, snap, target="bench")
        s = r["summary"]
        covered = (sum(e["gl_row_count"] for e in r["governed"])
                   + sum(e["gl_row_count"] for e in r["ungoverned"])
                   + sum(e["gl_row_count"] for e in r["governed_ungoverned_generation"]))
        self.assertEqual(covered, s["gl_rows_total"])
        self.assertEqual(s["gl_rows_total"], 5)
        self.assertEqual(s["governed"] + s["ungoverned"] + s["governed_ungoverned_generation"],
                         s["vouchers_total"])
        self.assertTrue(r["complete"])

    def test_malformed_none_identity_row_is_bucketed_not_dropped(self):
        r = build_reconciliation([], _snapshot([_gl_row(None, None)]), target="bench")
        self.assertEqual(r["summary"]["gl_rows_total"], 1)
        self.assertEqual(r["summary"]["ungoverned"], 1)


# --- the scope_note and render discipline ----------------------------------------------------

class TestScopeAndRender(unittest.TestCase):
    def test_scope_note_always_present_and_carries_both_ceilings(self):
        for snap in (_snapshot([]), _snapshot([_gl_row("Sales Invoice", "SI-1")])):
            r = build_reconciliation([], snap, target="bench")
            note = r["scope_note"].lower()
            self.assertTrue(note)
            # (a) governs-vs-detects ceiling
            self.assertIn("did not pass through it", note)
            self.assertIn("no verdict", note)
            # (b) tamper ceiling
            self.assertIn("server-side code execution", note)
            self.assertIn("erase", note)

    def test_render_carries_scope_note(self):
        r = build_reconciliation([], _snapshot([]), target="bench")
        self.assertIn(r["scope_note"], render_reconciliation(r))

    def test_render_shows_period_and_company(self):
        r = build_reconciliation([], _snapshot([]), target="bench", company="Acme Inc",
                                 since="2026-07-01T00:00:00Z", until="2026-07-31T23:59:59Z")
        text = render_reconciliation(r)
        self.assertIn("Acme Inc", text)
        self.assertIn("2026-07-01T00:00:00Z", text)

    def test_period_echoed_not_used_to_window_receipts(self):
        # since/until are recorded for display; they do not drop a governed match whose row sits in
        # the snapshot. (Receipts are corroboration, never re-windowed on a different clock.)
        receipts = [_submit_intent(0, "SI-1"), _outcome(1, 0, result=_committed_result("SI-1"))]
        snap = _snapshot([_gl_row("Sales Invoice", "SI-1")])
        r = build_reconciliation(receipts, snap, target="bench",
                                 since="2020-01-01T00:00:00Z", until="2020-12-31T23:59:59Z")
        self.assertEqual(r["period"], {"since": "2020-01-01T00:00:00Z",
                                       "until": "2020-12-31T23:59:59Z"})
        self.assertEqual(r["summary"]["governed"], 1)


if __name__ == "__main__":
    unittest.main()
