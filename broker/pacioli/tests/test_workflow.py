# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the WORKFLOW pure core (pacioli.workflow) — CONSENT's second gate,
the company's own ERPNext Workflow ridden (not replaced) as separation-of-duties law.

All shapes here are knowledge-pinned from frappe/frappe source (frappe/model/workflow.py,
Workflow/Workflow Document State/Workflow Transition doctype JSON) as read 2026-07-02, NOT
live-verified against a bench. Deny-biased throughout — see module docstring in workflow.py.

Run: `python3 -m unittest pacioli.tests.test_workflow` from the broker app root. No frappe required.
"""
import unittest

from pacioli.workflow import (
    Ambiguous,
    Malformed,
    check_submit_gate,
    check_transition,
    classify_transition,
    doc_status,
    find_active,
    governs_op,
    initial_seat,
    self_approval_allowed,
    sod_report,
)


def make_workflow(name="SI Approval", states=None, transitions=None, is_active=1):
    return {
        "name": name,
        "document_type": "Sales Invoice",
        "is_active": is_active,
        "workflow_state_field": "workflow_state",
        "states": states if states is not None else [
            {"state": "Draft", "doc_status": "0", "allow_edit": "Sales User"},
            {"state": "Pending Approval", "doc_status": "0", "allow_edit": "Sales Manager"},
            {"state": "Approved", "doc_status": "1", "allow_edit": "Sales Manager"},
        ],
        "transitions": transitions if transitions is not None else [
            {"state": "Draft", "action": "Submit for Approval", "next_state": "Pending Approval",
             "allowed": "Sales User", "allow_self_approval": "1"},
            {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
             "allowed": "Sales Manager", "allow_self_approval": "0"},
        ],
    }


class TestFindActive(unittest.TestCase):
    def test_no_workflows_is_none(self):
        self.assertIsNone(find_active([]))
        self.assertIsNone(find_active(None))

    def test_exactly_one_is_returned(self):
        wf = make_workflow()
        self.assertEqual(find_active([wf]), wf)

    def test_more_than_one_is_ambiguous_naming_both(self):
        wf1 = make_workflow(name="SI Approval A")
        wf2 = make_workflow(name="SI Approval B")
        result = find_active([wf1, wf2])
        self.assertIsInstance(result, Ambiguous)
        self.assertEqual(set(result.names), {"SI Approval A", "SI Approval B"})


class TestClassifyTransition(unittest.TestCase):
    STATES = [
        {"state": "Draft", "doc_status": "0"},
        {"state": "Pending Approval", "doc_status": "0"},
        {"state": "Approved", "doc_status": "1"},
        {"state": "Cancelled", "doc_status": "2"},
        {"state": "No Status Row"},  # missing doc_status entirely
    ]

    def test_next_state_doc_status_zero_is_non_approving(self):
        t = {"state": "Draft", "action": "Send", "next_state": "Pending Approval"}
        self.assertEqual(classify_transition(self.STATES, t), "non_approving")

    def test_next_state_doc_status_one_is_approving(self):
        t = {"state": "Pending Approval", "action": "Approve", "next_state": "Approved"}
        self.assertEqual(classify_transition(self.STATES, t), "approving")

    def test_next_state_doc_status_two_is_approving(self):
        t = {"state": "Approved", "action": "Cancel", "next_state": "Cancelled"}
        self.assertEqual(classify_transition(self.STATES, t), "approving")

    def test_unknown_next_state_is_approving_deny_biased(self):
        t = {"state": "Draft", "action": "Teleport", "next_state": "Nowhere"}
        self.assertEqual(classify_transition(self.STATES, t), "approving")

    def test_missing_doc_status_on_target_state_is_approving_deny_biased(self):
        t = {"state": "Draft", "action": "Go", "next_state": "No Status Row"}
        self.assertEqual(classify_transition(self.STATES, t), "approving")

    def test_missing_next_state_is_approving_deny_biased(self):
        t = {"state": "Draft", "action": "Nothing"}
        self.assertEqual(classify_transition(self.STATES, t), "approving")


class TestGovernsOp(unittest.TestCase):
    def test_no_workflow_never_governs(self):
        self.assertFalse(governs_op(None, "submit"))
        self.assertFalse(governs_op(None, "cancel"))

    def test_any_active_workflow_governs_submit(self):
        self.assertTrue(governs_op(make_workflow(), "submit"))

    def test_workflow_without_a_cancel_state_does_not_govern_cancel(self):
        # The default fixture has no doc_status "2" state anywhere — cancel stays marker-governed.
        self.assertFalse(governs_op(make_workflow(), "cancel"))

    def test_workflow_with_a_configured_cancel_state_governs_cancel(self):
        states = [
            {"state": "Draft", "doc_status": "0"},
            {"state": "Approved", "doc_status": "1"},
            {"state": "Cancelled", "doc_status": "2"},
        ]
        transitions = [
            {"state": "Draft", "action": "Approve", "next_state": "Approved",
             "allowed": "Sales Manager", "allow_self_approval": "0"},
            {"state": "Approved", "action": "Cancel", "next_state": "Cancelled",
             "allowed": "Sales Manager", "allow_self_approval": "0"},
        ]
        wf = make_workflow(states=states, transitions=transitions)
        self.assertTrue(governs_op(wf, "cancel"))

    def test_unknown_op_does_not_govern(self):
        self.assertFalse(governs_op(make_workflow(), "amend"))


class TestCheckSubmitGate(unittest.TestCase):
    def test_no_workflow_passes(self):
        self.assertEqual(check_submit_gate(None, "submit"), (True, None))

    def test_active_workflow_denies_submit_naming_workflow_and_role(self):
        wf = make_workflow(name="SI Approval")
        ok, reason = check_submit_gate(wf, "submit")
        self.assertFalse(ok)
        self.assertIn("SI Approval", reason)
        self.assertIn("Sales Manager", reason)  # the approving transition's role

    def test_cancel_not_configured_into_workflow_passes(self):
        self.assertEqual(check_submit_gate(make_workflow(), "cancel"), (True, None))

    def test_cancel_configured_into_workflow_denies(self):
        states = [
            {"state": "Draft", "doc_status": "0"},
            {"state": "Approved", "doc_status": "1"},
            {"state": "Cancelled", "doc_status": "2"},
        ]
        transitions = [
            {"state": "Approved", "action": "Cancel", "next_state": "Cancelled",
             "allowed": "Sales Manager", "allow_self_approval": "0"},
        ]
        wf = make_workflow(states=states, transitions=transitions)
        ok, reason = check_submit_gate(wf, "cancel")
        self.assertFalse(ok)
        self.assertIn("Sales Manager", reason)

    def test_ambiguous_workflows_deny_naming_both(self):
        result = find_active([make_workflow(name="A"), make_workflow(name="B")])
        ok, reason = check_submit_gate(result, "submit")
        self.assertFalse(ok)
        self.assertIn("A", reason)
        self.assertIn("B", reason)


class TestCheckTransition(unittest.TestCase):
    def test_no_workflow_denies(self):
        ok, reason, transition = check_transition(None, "Draft", "Submit for Approval")
        self.assertFalse(ok)
        self.assertIsNone(transition)

    def test_missing_current_state_denies(self):
        wf = make_workflow()
        for bad in (None, "", "   "):
            ok, reason, transition = check_transition(wf, bad, "Submit for Approval")
            self.assertFalse(ok)
            self.assertIsNone(transition)

    def test_undefined_action_from_state_denies_naming_legal_actions(self):
        wf = make_workflow()
        ok, reason, transition = check_transition(wf, "Draft", "Teleport")
        self.assertFalse(ok)
        self.assertIn("Submit for Approval", reason)
        self.assertIsNone(transition)

    def test_approving_transition_denies_naming_role(self):
        wf = make_workflow()
        ok, reason, transition = check_transition(wf, "Pending Approval", "Approve")
        self.assertFalse(ok)
        self.assertIn("Sales Manager", reason)
        self.assertIsNone(transition)

    def test_non_approving_transition_is_allowed_and_returned(self):
        wf = make_workflow()
        ok, reason, transition = check_transition(wf, "Draft", "Submit for Approval")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(transition["next_state"], "Pending Approval")


class TestSodReport(unittest.TestCase):
    def test_no_workflow_is_trivially_sod_true(self):
        report = sod_report(None)
        self.assertTrue(report["sod"])
        self.assertEqual(report["approving_transitions"], [])
        self.assertIsNone(report["risk"])

    def test_self_approvable_approving_transition_flags_risk(self):
        wf = make_workflow()  # Draft->Pending Approval allow_self_approval "1" but non-approving;
        # Pending Approval->Approved (approving) has allow_self_approval "0" — so this fixture is
        # actually clean SoD. Build one with a self-approvable APPROVING transition explicitly.
        states = [
            {"state": "Draft", "doc_status": "0"},
            {"state": "Approved", "doc_status": "1"},
        ]
        transitions = [
            {"state": "Draft", "action": "Approve", "next_state": "Approved",
             "allowed": "Sales Manager", "allow_self_approval": "1"},
        ]
        wf2 = make_workflow(states=states, transitions=transitions)
        report = sod_report(wf2)
        self.assertFalse(report["sod"])
        self.assertIsNotNone(report["risk"])
        self.assertIn("Sales Manager", report["risk"])
        self.assertEqual(len(report["approving_transitions"]), 1)
        self.assertTrue(report["approving_transitions"][0]["allow_self_approval"])

    def test_clean_workflow_is_sod_true_with_no_risk(self):
        wf = make_workflow()  # Approve transition has allow_self_approval "0"
        report = sod_report(wf)
        self.assertTrue(report["sod"])
        self.assertIsNone(report["risk"])
        self.assertEqual(len(report["approving_transitions"]), 1)  # only Approve is approving

    def test_explicitly_falsy_allow_self_approval_is_falsy(self):
        # An EXPLICIT off ("0", 0, False, "") is off. None/missing is NOT in this list — frappe's
        # field default is "1", so an absent value must read as ON (the risky direction); see
        # TestSelfApprovalDefault below.
        for falsy in ("0", 0, False, ""):
            states = [{"state": "Draft", "doc_status": "0"}, {"state": "Approved", "doc_status": "1"}]
            transitions = [{"state": "Draft", "action": "Approve", "next_state": "Approved",
                           "allowed": "Sales Manager", "allow_self_approval": falsy}]
            wf = make_workflow(states=states, transitions=transitions)
            report = sod_report(wf)
            self.assertTrue(report["sod"], f"allow_self_approval={falsy!r} should be falsy")


class TestSelfApprovalDefault(unittest.TestCase):
    """frappe's allow_self_approval defaults to "1" — a transition dict that simply LACKS the key
    (or carries None) must read as self-approval ON, the risky direction. Reading it as off would
    invert frappe's documented default and print "no self-approval risk" for the riskiest case."""

    def _wf_with_self_approval(self, **kwargs):
        states = [{"state": "Draft", "doc_status": "0"}, {"state": "Approved", "doc_status": "1"}]
        t = {"state": "Draft", "action": "Approve", "next_state": "Approved",
             "allowed": "Sales Manager"}
        t.update(kwargs)
        return make_workflow(states=states, transitions=[t])

    def test_missing_key_reads_as_frappe_default_on(self):
        self.assertTrue(self_approval_allowed({"action": "Approve"}))

    def test_none_value_reads_as_frappe_default_on(self):
        self.assertTrue(self_approval_allowed({"action": "Approve", "allow_self_approval": None}))

    def test_explicit_off_reads_as_off(self):
        for falsy in ("0", 0, False, ""):
            self.assertFalse(self_approval_allowed({"allow_self_approval": falsy}), repr(falsy))

    def test_explicit_on_reads_as_on(self):
        for on in ("1", 1, True):
            self.assertTrue(self_approval_allowed({"allow_self_approval": on}), repr(on))

    def test_sod_report_flags_a_transition_missing_the_key(self):
        report = sod_report(self._wf_with_self_approval())  # no allow_self_approval key at all
        self.assertFalse(report["sod"])
        self.assertIsNotNone(report["risk"])

    def test_sod_report_flags_a_none_value(self):
        report = sod_report(self._wf_with_self_approval(allow_self_approval=None))
        self.assertFalse(report["sod"])


class TestMalformedConfig(unittest.TestCase):
    """A malformed workflow body must never read as "no workflow" (silently disabling the gate)
    and must never crash a tool call — find_active returns a Malformed sentinel and every gate
    consumer refuses it by name."""

    def test_single_empty_dict_is_malformed_not_none(self):
        result = find_active([{}])
        self.assertIsInstance(result, Malformed)

    def test_single_none_element_is_malformed(self):
        self.assertIsInstance(find_active([None]), Malformed)

    def test_single_non_dict_element_is_malformed(self):
        self.assertIsInstance(find_active(["some-string"]), Malformed)

    def test_malformed_wins_over_ambiguous_in_a_multi_list(self):
        # Ambiguous must not mask garbage: a multi list with any malformed element is Malformed.
        result = find_active([make_workflow(name="A"), {}])
        self.assertIsInstance(result, Malformed)
        result = find_active([None, make_workflow(name="A"), make_workflow(name="B")])
        self.assertIsInstance(result, Malformed)

    def test_check_submit_gate_refuses_malformed(self):
        ok, reason = check_submit_gate(find_active([{}]), "submit")
        self.assertFalse(ok)
        self.assertIn("malformed", reason.lower())

    def test_check_submit_gate_default_denies_unrecognised_input(self):
        # Belt for future callers: anything that is not None/Ambiguous/Malformed/a plain
        # non-empty dict is refused, never read as "no workflow".
        for garbage in ("some-string", 42, [], {}):
            ok, reason = check_submit_gate(garbage, "submit")
            self.assertFalse(ok, repr(garbage))

    def test_governs_op_is_not_reached_with_an_empty_dict_gate_still_denies(self):
        # The specific bypass the redteam proved: {} is falsy, so governs_op({}) is False —
        # check_submit_gate must deny BEFORE consulting governs_op.
        ok, _ = check_submit_gate({}, "submit")
        self.assertFalse(ok)
        ok, _ = check_submit_gate({}, "cancel")
        self.assertFalse(ok)


class TestDocStatusNormalisation(unittest.TestCase):
    """doc_status(row): the one normaliser for a state row's doc_status — stripped string,
    None/missing -> "" (never the string "None"), ints normalised to their digit string."""

    def test_none_and_missing_are_empty_string(self):
        self.assertEqual(doc_status({"state": "X", "doc_status": None}), "")
        self.assertEqual(doc_status({"state": "X"}), "")
        self.assertEqual(doc_status(None), "")
        self.assertEqual(doc_status("not-a-dict"), "")

    def test_ints_normalise_to_digit_strings(self):
        self.assertEqual(doc_status({"doc_status": 0}), "0")
        self.assertEqual(doc_status({"doc_status": 2}), "2")

    def test_whitespace_is_stripped(self):
        self.assertEqual(doc_status({"doc_status": " 0 "}), "0")

    def test_classify_accepts_int_zero_as_non_approving(self):
        states = [{"state": "Draft", "doc_status": 0}, {"state": "Pending", "doc_status": 0}]
        t = {"state": "Draft", "action": "Send", "next_state": "Pending"}
        self.assertEqual(classify_transition(states, t), "non_approving")

    def test_governs_op_cancel_sees_int_two(self):
        states = [{"state": "Approved", "doc_status": 1}, {"state": "Cancelled", "doc_status": 2}]
        wf = make_workflow(states=states, transitions=[])
        self.assertTrue(governs_op(wf, "cancel"))


class TestInitialSeat(unittest.TestCase):
    """initial_seat(workflow): where a FRESH draft belongs in an active workflow — frappe's own
    convention (frappe/model/workflow.py seats a new document at ``workflow.states[0].state``),
    ridden not replaced. Deny-biased: an unseatable workflow returns (None, None, reason) — the
    caller refuses rather than creating the workflow-stateless draft F1 proved is stuck."""

    def test_seats_at_the_first_state_via_the_configured_field(self):
        field, state, reason = initial_seat(make_workflow())
        self.assertEqual(field, "workflow_state")
        self.assertEqual(state, "Draft")
        self.assertIsNone(reason)

    def test_a_custom_state_field_is_honoured_never_hardcoded(self):
        wf = make_workflow()
        wf["workflow_state_field"] = "approval_state"
        field, state, reason = initial_seat(wf)
        self.assertEqual(field, "approval_state")
        self.assertEqual(state, "Draft")
        self.assertIsNone(reason)

    def test_missing_or_blank_state_field_is_unseatable(self):
        for bad in (None, "", "   "):
            wf = make_workflow()
            wf["workflow_state_field"] = bad
            field, state, reason = initial_seat(wf)
            self.assertIsNone(field)
            self.assertIsNone(state)
            self.assertIn("workflow_state_field", reason)
        wf = make_workflow()
        del wf["workflow_state_field"]
        field, state, reason = initial_seat(wf)
        self.assertIsNone(state)
        self.assertIn("workflow_state_field", reason)

    def test_empty_or_missing_states_are_unseatable(self):
        wf = make_workflow(states=[])
        field, state, reason = initial_seat(wf)
        self.assertIsNone(state)
        self.assertIn("state", reason)
        wf = make_workflow()
        del wf["states"]
        _, state, reason = initial_seat(wf)
        self.assertIsNone(state)
        self.assertIsNotNone(reason)

    def test_a_malformed_first_state_row_is_unseatable(self):
        for bad_first in ("not-a-dict", None, {}, {"doc_status": "0"}, {"state": "   "}):
            wf = make_workflow(states=[bad_first, {"state": "Draft", "doc_status": "0"}])
            _, state, reason = initial_seat(wf)
            self.assertIsNone(state, f"seated on malformed first row {bad_first!r}")
            self.assertIsNotNone(reason)

    def test_a_first_state_that_is_not_a_draft_state_is_unseatable(self):
        # A draft (docstatus 0) must never wear a state the workflow maps to submitted or
        # cancelled — deny rather than label the document with a lie.
        for ds in ("1", "2", 1, None):
            wf = make_workflow(states=[{"state": "Posted", "doc_status": ds}])
            _, state, reason = initial_seat(wf)
            self.assertIsNone(state, f"seated on doc_status {ds!r}")
            self.assertIn("doc_status", reason)

    def test_int_zero_doc_status_still_seats(self):
        wf = make_workflow(states=[{"state": "Draft", "doc_status": 0}])
        field, state, reason = initial_seat(wf)
        self.assertEqual((field, state, reason), ("workflow_state", "Draft", None))

    def test_field_and_state_are_both_stripped_symmetrically(self):
        # Review finding [5]: a whitespace-carrying state name written verbatim seats the draft
        # at a string no transition row matches — stuck again, with receipts asserting the seat.
        wf = make_workflow(states=[{"state": " Draft ", "doc_status": "0"}])
        wf["workflow_state_field"] = " workflow_state "
        field, state, reason = initial_seat(wf)
        self.assertEqual((field, state, reason), ("workflow_state", "Draft", None))

    def test_a_non_dict_workflow_is_unseatable_not_a_crash(self):
        for bad in (None, {}, "wf", 7):
            field, state, reason = initial_seat(bad)
            self.assertIsNone(state)
            self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()
