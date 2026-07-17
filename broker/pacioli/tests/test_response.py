# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The Close — Half 3, response-to-gap (docs/plans/2026-07-11-close-half3-response-to-gap.md).

Pure-core tests, bench-free. ``build_response`` is a pure consumer of the two dicts Halves 1 and 2
already return (``build_statement`` / ``build_reconciliation``) plus the operator's posture and
response envelope. It renders NO verdict on another party: ``ungoverned`` movement is recorded
(never accused) under the default posture and only elevated to an alert under a ``sole_door``
posture the operator explicitly declared. The envelope is a floor, not an override — an operator
can escalate a finding but can never silence one below its deny-biased floor. **Invariant 1,
revised 2026-07-15, narrowed same-day by the redteam fix wave (D1)** (see ``response.py``'s module
docstring and ``TestChainBroken``/``TestChainBrokenRender`` below): CONTAIN is never a default for
a working ledger honestly recording a known uncertainty; ``chain_broken`` — the attestation
apparatus provably broken, ``verified is False`` ONLY (the anchor_matches branch was dropped) — is
the sole exception."""
import unittest

from pacioli.response import build_response, render_response


def _stmt(acts, *, target="bench", chain=None):
    d = {"target": target, "acts": acts, "period": {"since": None, "until": None},
         "summary": {}, "balanced": True, "flags": [], "scope_note": "s"}
    if chain is not None:
        d["chain"] = chain
    return d


def _chain(*, verified=True, verify_reason=None, anchor_matches=None, head="h", count=1):
    return {"head": head, "count": count, "verified": verified, "verify_reason": verify_reason,
            "anchor_head": None if anchor_matches is None else "a", "anchor_matches": anchor_matches}


def _act(cls, *, outcome_status=None, seq=1, tool="submit", doctype="Sales Invoice", docname="SI-1"):
    return {"seq": seq, "class": cls, "outcome_status": outcome_status, "tool": tool,
            "doctype": doctype, "docname": docname, "ts": "2026-07-01T00:00:00Z"}


def _recon(*, target="bench", governed=None, ungoverned=None, generation=None,
           complete=True, immutable=True, delete_linked=False):
    return {
        "target": target,
        "company": None,
        "period": {"since": None, "until": None},
        "governed": governed or [],
        "ungoverned": ungoverned or [],
        "governed_ungoverned_generation": generation or [],
        "reposts_in_window": [],
        "summary": {},
        "posture": {"enable_immutable_ledger": immutable,
                    "delete_linked_ledger_entries": delete_linked},
        "complete": complete,
        "flags": [],
        "scope_note": "s",
    }


def _ungov(voucher_no="SI-9", owner="desk@x", vt="Sales Invoice"):
    return {"voucher_type": vt, "voucher_no": voucher_no, "owner": owner,
            "gl_row_count": 2, "posting_date": "2026-07-01"}


def _gen(voucher_no="SI-2"):
    return {"voucher_type": "Sales Invoice", "voucher_no": voucher_no, "gl_row_count": 2,
            "ungoverned_owner": "admin@x", "ungoverned_creation": "2026-07-02 03:00:00",
            "repost_ref": "RB-1", "note": "a governed act references this voucher..."}


class TestEmptyAndClean(unittest.TestCase):
    def test_no_inputs_is_clean_record(self):
        r = build_response(None, None)
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["response"], "record")
        self.assertEqual(r["response_level"], 0)
        self.assertFalse(r["seal_required"])
        self.assertFalse(r["gate_required"])

    def test_committed_act_is_not_a_finding(self):
        r = build_response(_stmt([_act("committed")]), None)
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["response"], "record")

    def test_failed_recorded_open_is_not_a_finding(self):
        # matches close.py: a `failed` recorded-open is a KNOWN non-event, not an orphan-like gap.
        r = build_response(_stmt([_act("recorded_open", outcome_status="failed")]), None)
        self.assertEqual(r["findings"], [])

    def test_scope_note_present_and_accounting_framed(self):
        r = build_response(None, None)
        self.assertIn("scope_note", r)
        low = r["scope_note"].lower()
        self.assertNotIn("unauthorized", low)
        self.assertNotIn("breach", low)


class TestStatementFindings(unittest.TestCase):
    def test_orphan_is_alert_floor(self):
        r = build_response(_stmt([_act("orphan")]), None)
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(r["findings"][0]["class"], "orphan")
        self.assertEqual(r["findings"][0]["response"], "alert")
        self.assertEqual(r["response"], "alert")

    def test_unconfirmed_recorded_open_is_a_finding(self):
        r = build_response(_stmt([_act("recorded_open", outcome_status="unconfirmed")]), None)
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(r["findings"][0]["class"], "unconfirmed")
        self.assertEqual(r["findings"][0]["response"], "alert")

    def test_malformed_act_is_flagged_not_crashed(self):
        r = build_response({"target": "bench", "acts": ["not-a-dict"]}, None)
        self.assertTrue(any("classify" in f or "malformed" in f for f in r["flags"]))
        self.assertEqual(r["findings"], [])


class TestReconciliationFindings(unittest.TestCase):
    def test_second_generation_is_alert(self):
        r = build_response(None, _recon(generation=[_gen()]))
        self.assertEqual([f["class"] for f in r["findings"]], ["second_generation"])
        self.assertEqual(r["response"], "alert")

    def test_blind_read_complete_false_is_alert(self):
        r = build_response(None, _recon(complete=False))
        self.assertTrue(any(f["class"] == "blind_read" for f in r["findings"]))
        self.assertEqual(r["response"], "alert")

    def test_blind_read_complete_none_is_alert(self):
        r = build_response(None, _recon(complete=None))
        self.assertTrue(any(f["class"] == "blind_read" for f in r["findings"]))

    def test_adverse_posture_immutable_off_is_record(self):
        r = build_response(None, _recon(immutable=False))
        adverse = [f for f in r["findings"] if f["class"] == "adverse_posture"]
        self.assertEqual(len(adverse), 1)
        self.assertEqual(adverse[0]["response"], "record")

    def test_adverse_posture_unreadable_is_flagged_adverse(self):
        r = build_response(None, _recon(immutable=None, delete_linked=None))
        self.assertTrue(any(f["class"] == "adverse_posture" for f in r["findings"]))

    def test_healthy_posture_is_not_a_finding(self):
        r = build_response(None, _recon(immutable=True, delete_linked=False))
        self.assertFalse(any(f["class"] == "adverse_posture" for f in r["findings"]))


class TestPostureGovernsUngoverned(unittest.TestCase):
    def test_ungoverned_default_posture_is_recorded_not_alerted(self):
        # accounting-not-police: absent posture => mixed_door; ungoverned is surfaced at RECORD.
        r = build_response(None, _recon(ungoverned=[_ungov()]))
        self.assertEqual(r["posture"], "mixed_door")
        ung = [f for f in r["findings"] if f["class"] == "ungoverned"]
        self.assertEqual(len(ung), 1)
        self.assertEqual(ung[0]["response"], "record")
        self.assertEqual(r["response"], "record")  # legitimate desk activity never raises the aggregate

    def test_ungoverned_sole_door_is_alerted(self):
        r = build_response(None, _recon(ungoverned=[_ungov()]), posture="sole_door")
        self.assertEqual(r["posture"], "sole_door")
        ung = [f for f in r["findings"] if f["class"] == "ungoverned"]
        self.assertEqual(ung[0]["response"], "alert")
        self.assertEqual(r["response"], "alert")

    def test_unparseable_posture_is_deny_biased_sole_door(self):
        r = build_response(None, _recon(ungoverned=[_ungov()]), posture="garbage")
        self.assertEqual(r["posture"], "sole_door")
        self.assertTrue(any("posture" in f for f in r["flags"]))
        ung = [f for f in r["findings"] if f["class"] == "ungoverned"]
        self.assertEqual(ung[0]["response"], "alert")


class TestEnvelopeIsAFloor(unittest.TestCase):
    def test_operator_can_escalate_orphan_to_contain(self):
        r = build_response(_stmt([_act("orphan")]), None, envelope={"orphan": "contain"})
        self.assertEqual(r["findings"][0]["response"], "contain")
        self.assertEqual(r["response"], "contain")
        self.assertTrue(r["seal_required"])

    def test_operator_cannot_silence_orphan_below_floor(self):
        r = build_response(_stmt([_act("orphan")]), None, envelope={"orphan": "record"})
        self.assertEqual(r["findings"][0]["response"], "alert")  # floor holds

    def test_contain_never_appears_by_default(self):
        r = build_response(
            _stmt([_act("orphan")]),
            _recon(complete=False, ungoverned=[_ungov()], generation=[_gen()], immutable=False),
            posture="sole_door",
        )
        self.assertNotIn("contain", [f["response"] for f in r["findings"]])
        self.assertNotEqual(r["response"], "contain")

    def test_gate_required_when_a_finding_is_attestation_gate(self):
        r = build_response(_stmt([_act("orphan")]), None, envelope={"orphan": "attestation_gate"})
        self.assertTrue(r["gate_required"])
        self.assertFalse(r["seal_required"])
        self.assertEqual(r["response"], "attestation_gate")

    def test_unparseable_envelope_level_falls_to_floor(self):
        r = build_response(_stmt([_act("orphan")]), None, envelope={"orphan": "nonsense"})
        self.assertEqual(r["findings"][0]["response"], "alert")


class TestChainBroken(unittest.TestCase):
    """D1 (John ruled 2026-07-15, redteam fix wave): the anchor branch is DROPPED entirely.
    ``chain_broken`` fires IFF ``chain.get("verified") is False`` — ``anchor_matches`` no longer
    drives it at all (it was a naive head==anchor equality that false-sealed a legitimately-grown
    chain; count-aware rollback detection lives in `anchor check`/`seal-status --anchor` instead)."""

    def test_verified_false_is_contain(self):
        r = build_response(_stmt([], chain=_chain(verified=False, verify_reason="bad-hash")), None)
        findings = [f for f in r["findings"] if f["class"] == "chain_broken"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["response"], "contain")
        self.assertEqual(r["response"], "contain")
        self.assertTrue(r["seal_required"])
        self.assertTrue(r["gate_required"])
        self.assertIn("bad-hash", str(findings[0]["detail"]))

    def test_anchor_mismatch_alone_does_NOT_emit_chain_broken(self):
        # THE D1 PIN: verified True (the chain itself verifies), anchor_matches False (a stale
        # --expected-head against a legitimately-grown chain). Through the pre-fix-wave build this
        # false-sealed; as of D1 it must emit NOTHING and must NOT reach contain/seal_required —
        # anchor_matches's naive head==anchor equality no longer drives this floor at all.
        r = build_response(_stmt([], chain=_chain(verified=True, anchor_matches=False)), None)
        self.assertFalse(any(f["class"] == "chain_broken" for f in r["findings"]))
        self.assertFalse(r["seal_required"])
        self.assertEqual(r["response"], "record")

    def test_verified_false_with_anchor_matches_false_still_one_finding_no_anchor_mention(self):
        # Both signals unhappy: still fires (on verified alone), still exactly one finding, and the
        # detail no longer mentions the anchor/rollback at all — verify failure only.
        r = build_response(
            _stmt([], chain=_chain(verified=False, verify_reason="tampered", anchor_matches=False)),
            None,
        )
        findings = [f for f in r["findings"] if f["class"] == "chain_broken"]
        self.assertEqual(len(findings), 1)
        detail_str = str(findings[0]["detail"]).lower()
        self.assertIn("tampered", detail_str)
        self.assertNotIn("anchor", detail_str)
        self.assertNotIn("rollback", detail_str)

    def test_guard_clean_pinless_close_does_not_emit(self):
        # verified True, anchor_matches None — the ordinary pinless clean close. MUST NOT seal.
        r = build_response(_stmt([], chain=_chain(verified=True, anchor_matches=None)), None)
        self.assertFalse(any(f["class"] == "chain_broken" for f in r["findings"]))
        self.assertFalse(r["seal_required"])

    def test_guard_verified_none_does_not_emit(self):
        # verified None (verify not run — synthetic/non-cmd_close statement). Absent signal, not failed.
        r = build_response(_stmt([], chain=_chain(verified=None, anchor_matches=None)), None)
        self.assertFalse(any(f["class"] == "chain_broken" for f in r["findings"]))
        self.assertFalse(r["seal_required"])

    def test_guard_chain_key_absent_does_not_emit_or_crash(self):
        r = build_response(_stmt([]), None)
        self.assertFalse(any(f["class"] == "chain_broken" for f in r["findings"]))
        self.assertFalse(r["seal_required"])

    def test_envelope_record_cannot_silence_chain_broken(self):
        r = build_response(
            _stmt([], chain=_chain(verified=False)), None, envelope={"chain_broken": "record"}
        )
        self.assertEqual(r["response"], "contain")
        self.assertTrue(r["seal_required"])

    def test_envelope_alert_cannot_silence_chain_broken(self):
        r = build_response(
            _stmt([], chain=_chain(verified=False)), None, envelope={"chain_broken": "alert"}
        )
        self.assertEqual(r["response"], "contain")
        self.assertTrue(r["seal_required"])

    def test_coexists_with_other_findings_aggregate_is_contain(self):
        r = build_response(
            _stmt([_act("orphan")], chain=_chain(verified=False)), None,
        )
        classes = {f["class"] for f in r["findings"]}
        self.assertIn("orphan", classes)
        self.assertIn("chain_broken", classes)
        self.assertEqual(r["response"], "contain")

    def test_verified_false_no_reason_omits_parenthetical(self):
        # verify_reason is None (verified False but no reason string captured) — the detail must
        # not read "failed to verify (None)"; the parenthetical is omitted entirely when there is
        # no reason to show (Task 2 render polish).
        r = build_response(_stmt([], chain=_chain(verified=False, verify_reason=None)), None)
        findings = [f for f in r["findings"] if f["class"] == "chain_broken"]
        self.assertEqual(len(findings), 1)
        detail_str = str(findings[0]["detail"])
        self.assertNotIn("(None)", detail_str)
        self.assertIn("failed to verify", detail_str)

    def test_verified_false_empty_string_reason_also_omits_parenthetical(self):
        # C3: verify_reason="" is falsy but not None — the is-None guard alone would still print
        # "failed to verify ()"; the guard must be on falsiness (`if verify_reason:`), not identity.
        r = build_response(_stmt([], chain=_chain(verified=False, verify_reason="")), None)
        findings = [f for f in r["findings"] if f["class"] == "chain_broken"]
        self.assertEqual(len(findings), 1)
        detail_str = str(findings[0]["detail"])
        self.assertNotIn("()", detail_str)
        self.assertIn("failed to verify", detail_str)


class TestChainNonDictFlagged(unittest.TestCase):
    """C2: a `chain` present but not a dict is flagged (deny-biased, mirrors the `acts` check) —
    still emits nothing, but is no longer silent."""

    def test_non_dict_chain_is_flagged_and_emits_nothing(self):
        stmt = _stmt([])
        stmt["chain"] = "not-a-dict"
        r = build_response(stmt, None)
        self.assertFalse(any(f["class"] == "chain_broken" for f in r["findings"]))
        self.assertTrue(any("chain" in f and "not a dict" in f for f in r["flags"]))


class TestChainBrokenRender(unittest.TestCase):
    def test_render_word_is_descriptive_not_bare_class_name(self):
        r = build_response(_stmt([], chain=_chain(verified=False, verify_reason="bad-hash")), None)
        text = render_response(r)
        # must not fall back to the bare "chain_broken: chain_broken" default-word rendering
        self.assertNotIn("chain_broken: chain_broken", text)
        self.assertIn("chain_broken:", text)
        low = text.lower()
        self.assertIn("chain broken", low)
        self.assertIn("contain", low)


class TestAggregateAndRender(unittest.TestCase):
    def test_aggregate_response_is_max_over_findings(self):
        r = build_response(
            _stmt([_act("orphan", seq=1), _act("recorded_open", seq=2, outcome_status="unconfirmed")]),
            _recon(ungoverned=[_ungov()]),
            envelope={"orphan": "contain"},
        )
        self.assertEqual(r["response"], "contain")
        self.assertEqual(r["summary"]["by_response"]["contain"], 1)

    def test_render_states_ungoverned_not_accused(self):
        r = build_response(None, _recon(ungoverned=[_ungov(voucher_no="SI-77")]))
        text = render_response(r)
        self.assertIn("SI-77", text)
        low = text.lower()
        self.assertIn("did not pass through pacioli", low)
        self.assertNotIn("unauthorized", low)
        self.assertNotIn("breach", low)
        self.assertNotIn("intrusion", low)

    def test_render_never_drops_scope(self):
        r = build_response(_stmt([_act("orphan")]), None)
        self.assertIn(r["scope_note"], render_response(r))

    def test_target_mismatch_is_flagged(self):
        r = build_response(_stmt([], target="A"), _recon(target="B"))
        self.assertTrue(any("target" in f for f in r["flags"]))


if __name__ == "__main__":
    unittest.main()
