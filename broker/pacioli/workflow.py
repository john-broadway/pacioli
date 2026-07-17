# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — WORKFLOW: CONSENT's second gate, ridden not replaced (pure core).

The root README's duality table carries an honest asterisk: the human-minted **marker** is
out-of-band consent, but it is not separation of duties — one human can plan, mint their own
marker, and submit. This module closes that gap the same way the rest of the spine closes every
gap: by **riding the company's own configuration**, never inventing new rules on top of it.

**The shape.** If a company has configured an active ERPNext **Workflow** on Sales Invoice, that
workflow IS the company's own CONSENT law for this doctype:

1. Any active workflow makes **submission** entirely workflow-governed — direct `submit` is
   refused, full stop, naming the workflow and the role(s) on its approving transition(s). The
   approving transition belongs to a human with that role, not the broker.
2. The broker may still perform **non-approving** transitions on the agent's behalf (e.g.
   Draft → Pending Approval) — a transition whose `next_state` keeps `doc_status "0"`. Any
   transition whose `next_state` carries `doc_status "1"` or `"2"` is refused broker-side, EVEN IF
   the scoped credential could technically call `apply_workflow` and succeed — belt (this module
   refuses) and suspenders (frappe's own `has_approval_access` / role check downstream).
3. **No workflow configured on the doctype = this gate passes** — submission stays
   marker-governed exactly as it is today. CONSENT enforces the company's own approval rules; it
   does not invent new ones for a company that never configured a Workflow.
4. **Cancel** is governed by this same module only if the company's own workflow configuration
   maps a state to `doc_status "2"` — i.e. the company built cancellation into the workflow.
   Otherwise cancel stays marker-governed, unchanged. We govern by what the company configured,
   never by assuming ERPNext's default duality.
5. Every classification here is **deny-biased**: an unknown next state, a missing `doc_status`, a
   missing/blank current state, an undefined action, more than one active workflow found for
   the doctype (ambiguous — not verified as forbidden by frappe), or a **malformed workflow body**
   (an empty dict, ``None``, a bare string — an unverifiable gate source must refuse, never read
   as "no workflow") all refuse rather than guess.

**Honest limit #1 — this gate governs the agent's path only.** Frappe does NOT enforce Workflow
on a direct `docstatus` change: `validate_workflow` only fires when the document's
`workflow_state` field itself changes on save, so a plain submit that never touches that field
passes frappe with nothing but the generic DocType submit permission — the Workflow is otherwise
decorative. This module's refusal is therefore the **only** thing stopping *this broker* from
submitting around a company's configured approval chain; it does not and cannot make ERPNext
itself enforce the workflow against any other caller (the bench admin console, a script, a report
button). Bench-side (guard-side) enforcement of Workflow against every calling path is a named
future increment, not implied by anything here.

**Honest limit #2 — knowledge-pinned, not live-verified.** Every frappe shape this module's
callers depend on (`Workflow`/`Workflow Document State`/`Workflow Transition` fieldnames,
`apply_workflow`'s exact contract, `has_approval_access`'s self-approval default) was read from
frappe/frappe source (branch version-15) on 2026-07-02 — it has not been exercised against a live
bench. Live falsification is Gate 5, a separate bench session; nothing here claims otherwise.

**Self-approval is not blocked by frappe by default.** `allow_self_approval` on a Workflow
Transition defaults to `"1"` — a company that leaves it at the default lets the same human plan,
approve, and submit. This module does not refuse that (CONSENT enforces the company's own rules;
it does not invent stricter ones), but `sod_report` surfaces it honestly as a risk flag so the
operator can see it plainly, not read a governed workflow as automatically meaning separated
duties.

No frappe, no I/O, no clock — every input here is data the glue already fetched.
"""
from __future__ import annotations

from dataclasses import dataclass

_FALSY_STRINGS = ("", "0", "false", "no", "none")


@dataclass(frozen=True)
class Ambiguous:
    """Sentinel: more than one active Workflow was found governing one doctype. Ambiguous
    configuration — never verified as forbidden by frappe, so the gates that see this refuse
    and name every workflow found, rather than pick one silently.

    :param names: the names of every active workflow found, in the order returned.
    """

    names: tuple


@dataclass(frozen=True)
class Malformed:
    """Sentinel: the fetched workflow config contains something that is not a workflow document
    (an empty dict, ``None``, a bare string). Redteam-proven bypass this closes: a malformed
    *single* element used to flow through :func:`find_active` unchecked, and both
    :func:`check_submit_gate` (``is None``) and :func:`governs_op` (``not workflow``) then read
    it as "no workflow" — the gate silently disabled with a valid marker in hand. A malformed
    body is an *unverifiable* gate source, and an unverifiable gate source refuses (the house
    rule) — every consumer denies on this sentinel by name, never guesses.

    :param detail: a short human description of what was malformed (never the raw garbage).
    """

    detail: str


def truthy(value):
    """Normalise a frappe Check-field value (``"1"``/``"0"`` strings, ``1``/``0`` ints, bools) to
    a real bool. Generic and direction-neutral by design — it carries NO default for a missing
    value. Where frappe's own field default matters (``allow_self_approval`` defaults to ON),
    the caller must use :func:`self_approval_allowed`, which maps missing/None to the frappe
    default, not through this helper."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_STRINGS
    return bool(value)


def self_approval_allowed(transition):
    """Does this transition permit self-approval? A **missing key or a None value reads as ON**
    — frappe's ``allow_self_approval`` field default is ``"1"``, so an absent value means the
    company never turned it off, which is the *risky* direction. (Reading absence as "off" would
    invert frappe's documented default and print "no self-approval risk" for exactly the
    workflows that have the most of it.) An explicit value goes through :func:`truthy`."""
    if not isinstance(transition, dict) or "allow_self_approval" not in transition:
        return True
    value = transition["allow_self_approval"]
    if value is None:
        return True
    return truthy(value)


def doc_status(row):
    """The one normaliser for a state row's ``doc_status``: the stripped string of the value,
    with ``None``/missing/non-dict → ``""`` (never the string ``"None"``), and ints normalised
    to their digit string (a bench that serialises the Select field as ``0``/``2`` instead of
    ``"0"``/``"2"`` still classifies correctly). Every ``doc_status`` comparison in this module
    and the tool layer goes through here — one shape, one place."""
    if not isinstance(row, dict):
        return ""
    value = row.get("doc_status")
    return "" if value is None else str(value).strip()


def _is_workflow_doc(item):
    """A minimally-plausible workflow document: a non-empty dict. (The client layer additionally
    guarantees a non-blank name; this pure check is the belt under it.)"""
    return isinstance(item, dict) and bool(item)


def find_active(workflows):
    """From the fetched list of active workflows for one doctype (the client already filters
    ``is_active=1``): ``0`` → ``None`` (this gate passes downstream — no workflow configured);
    exactly ``1`` → that workflow dict; ``>1`` → an :class:`Ambiguous` sentinel naming all of
    them. Any element that is not a non-empty dict → a :class:`Malformed` sentinel — checked
    FIRST, so garbage in a multi list is never masked as merely "ambiguous", and a malformed
    single element is never handed onward to gates that would read it as "no workflow".
    One-active-workflow-per-doctype is not verified as enforced by frappe, so more than one is
    treated as ambiguous configuration, never as "pick the first."""
    workflows = list(workflows or [])
    if not workflows:
        return None
    bad = [w for w in workflows if not _is_workflow_doc(w)]
    if bad:
        kinds = ", ".join(sorted({type(w).__name__ for w in bad}))
        return Malformed(detail=f"{len(bad)} of {len(workflows)} workflow record(s) are not "
                                f"workflow documents (got: {kinds})")
    if len(workflows) == 1:
        return workflows[0]
    names = tuple(str(w.get("name")) for w in workflows)
    return Ambiguous(names=names)


def classify_transition(states, transition):
    """``"non_approving"`` iff ``transition``'s ``next_state`` maps to a state row whose
    ``doc_status == "0"``; anything else — ``doc_status`` ``"1"``/``"2"``, an unknown next state,
    or a state row with no ``doc_status`` at all — is ``"approving"``. Deny-biased: an unresolvable
    classification is never treated as safe to let the broker perform."""
    next_state = transition.get("next_state") if isinstance(transition, dict) else None
    if not next_state:
        return "approving"
    for row in states or []:
        if isinstance(row, dict) and row.get("state") == next_state:
            return "non_approving" if doc_status(row) == "0" else "approving"
    return "approving"  # unknown next_state — deny-biased


def governs_op(workflow, op):
    """Does this workflow govern ``op``? ``op="submit"``: ``True`` whenever ``workflow`` is
    given at all — any active workflow makes submission workflow-governed, full stop.
    ``op="cancel"``: ``True`` iff the company's own configuration maps some state row to
    ``doc_status "2"``; otherwise ``False`` and cancel stays marker-governed as today. (A
    transition's ``next_state`` can only ever point at one of these same state rows, so the
    state scan IS the complete check — no separate transition walk.) We govern by what the
    company configured, never by inventing a rule frappe itself doesn't have."""
    if not workflow:
        return False
    if op == "submit":
        return True
    if op == "cancel":
        states = [s for s in (workflow.get("states") or []) if isinstance(s, dict)]
        return any(doc_status(s) == "2" for s in states)
    return False  # an op this module doesn't know about is never treated as governed


def _approving_roles(workflow):
    """The role(s) named on every approving transition — used to make a submit-gate refusal
    concrete ("request the workflow transition instead of role X") rather than generic."""
    states = workflow.get("states") or []
    roles = set()
    for t in workflow.get("transitions") or []:
        if isinstance(t, dict) and classify_transition(states, t) == "approving" and t.get("allowed"):
            roles.add(str(t["allowed"]))
    return roles


def check_submit_gate(workflow, op):
    """Deny-biased gate for the caller-side check in ``_governed_write``. ``workflow`` is the
    result of :func:`find_active` — ``None`` (gate passes: no workflow configured), a single
    workflow dict, an :class:`Ambiguous` sentinel (refused, naming every workflow found), or a
    :class:`Malformed` sentinel (refused — an unverifiable gate source never reads as "no
    workflow"). Anything else — including an empty dict, which is falsy and would slip past
    :func:`governs_op` as ungoverned — is refused by the default-deny floor at the end: only an
    affirmative ``None`` passes this gate without a workflow. When a real workflow governs ``op``
    (:func:`governs_op`), the refusal NAMES the workflow and the role(s) on its approving
    transition(s), so the reason tells the agent exactly who to ask."""
    if workflow is None:
        return (True, None)
    if isinstance(workflow, Malformed):
        return (False, f"workflow configuration is malformed ({workflow.detail}) — an "
                       "unverifiable gate source refuses, it never reads as 'no workflow'")
    if isinstance(workflow, Ambiguous):
        names = ", ".join(repr(n) for n in workflow.names)
        return (False, f"{len(workflow.names)} active Workflows govern this doctype "
                       f"({names}) — ambiguous configuration, refusing rather than guessing")
    if not _is_workflow_doc(workflow):
        # Default-deny floor (belt for future callers that bypass find_active): a non-dict or an
        # empty dict is falsy, so governs_op would read it as "not governed" — refuse it here.
        return (False, "unrecognised workflow gate input — refusing (default-deny; "
                       "only 'no workflow configured' passes without a workflow)")
    if governs_op(workflow, op):
        roles = _approving_roles(workflow)
        role_text = ", ".join(sorted(repr(r) for r in roles)) if roles else "a human role"
        return (False, f"{op} is governed by Workflow {workflow.get('name')!r}; the approving "
                       f"transition belongs to role {role_text} — request the workflow "
                       "transition instead of a direct submit")
    return (True, None)


def initial_seat(workflow):
    """Where a FRESH draft belongs under ``workflow``: ``(state_field, state, None)`` on success,
    ``(None, None, reason)`` when this workflow cannot seat one. Frappe's own convention, ridden
    not replaced: a new document under an active workflow is seated at ``workflow.states[0].state``
    — ``frappe/model/workflow.py`` does exactly this for a new doc with no state (knowledge-pinned
    2026-07-17, same caveat as the rest of this module: NOT live-verified). The F1 fix uses this
    for the amend re-draft, which the bench's REST insert does NOT seat (live-observed on the lab
    bench 2026-07-17: the amendment arrived with a null state and no legal transition — stuck).

    Deny-biased, like every classification here: a missing/blank ``workflow_state_field``, no
    states, a malformed first state row, or a first state that does not map to ``doc_status "0"``
    (a draft must never wear a state the workflow maps to submitted or cancelled) all return a
    reason instead of a guess — the caller refuses rather than creating the stuck draft."""
    if not _is_workflow_doc(workflow):
        return (None, None, "not a workflow document — cannot determine an initial seat")
    field = workflow.get("workflow_state_field")
    if not isinstance(field, str) or not field.strip():
        return (None, None, "workflow names no workflow_state_field — cannot seat a draft")
    states = workflow.get("states")
    if not isinstance(states, list) or not states:
        return (None, None, "workflow has no states — cannot determine the initial state")
    first = states[0]
    if not isinstance(first, dict) or not isinstance(first.get("state"), str) \
            or not first["state"].strip():
        return (None, None, "workflow's first state row is malformed — cannot determine "
                            "the initial state")
    if doc_status(first) != "0":
        return (None, None, f"workflow's first state {first['state']!r} maps to doc_status "
                            f"{doc_status(first)!r} rather than \"0\" — a fresh draft cannot "
                            "be seated at a submitted or cancelled state")
    # Both halves stripped, symmetrically (review finding [5]): a state name carrying stray
    # whitespace would seat the draft at a string no transition row's raw == match ever finds —
    # stuck again, with receipts asserting the seat.
    return (field.strip(), first["state"].strip(), None)


def check_transition(workflow, current_state, action):
    """Validate a requested ``action`` from the document's ``current_state``. Deny-biased:
    a missing workflow, a missing/blank ``current_state`` (frappe can leave a doc's workflow
    state empty until the next save), or an ``action`` not defined from that state (naming the
    legal actions actually available) all refuse. A matching transition that classifies as
    approving refuses too, naming the human role it belongs to — this function only ever hands
    back a *non-approving* transition on success. Returns ``(ok, reason, transition_or_None)``."""
    if not workflow:
        return (False, "no active workflow governs this document", None)
    if not isinstance(current_state, str) or not current_state.strip():
        return (False, "document has no workflow state to transition from "
                       "(unset or unreadable) — refusing", None)
    transitions = [t for t in (workflow.get("transitions") or []) if isinstance(t, dict)]
    from_here = [t for t in transitions if t.get("state") == current_state]
    matches = [t for t in from_here if action and t.get("action") == action]
    if not matches:
        legal = sorted({t.get("action") for t in from_here if t.get("action")})
        legal_text = ", ".join(repr(a) for a in legal) if legal else "none"
        return (False, f"{action!r} is not a legal transition from state {current_state!r}; "
                       f"legal actions from here: {legal_text}", None)
    states = workflow.get("states") or []
    approving = [t for t in matches if classify_transition(states, t) == "approving"]
    if approving:
        roles = sorted({str(t["allowed"]) for t in approving if t.get("allowed")})
        role_text = ", ".join(repr(r) for r in roles) if roles else "a human role"
        return (False, f"{action!r} from {current_state!r} is an approving transition "
                       f"(belongs to role {role_text}); request the human perform it "
                       "instead of the broker", None)
    return (True, None, matches[0])


def sod_report(workflow):
    """Per approving transition on ``workflow``: ``{action, allowed_role, allow_self_approval}``.
    ``sod`` is ``False`` iff ANY approving transition permits self-approval — through
    :func:`self_approval_allowed`, so a transition that simply LACKS the field (or carries None)
    reads as frappe's own default, which is ON: a company that never touched that default has
    the same human plan, approve, and submit, and that must surface as the risk it is, never as
    "no self-approval risk". This function does not refuse on it: CONSENT enforces the company's
    own configured rules, it does not invent stricter ones — it only surfaces the honest risk
    text so an operator (or `workflow_status`) can see it plainly."""
    if not workflow:
        return {"approving_transitions": [], "sod": True, "risk": None}
    states = workflow.get("states") or []
    approving = []
    for t in workflow.get("transitions") or []:
        if isinstance(t, dict) and classify_transition(states, t) == "approving":
            approving.append({
                "action": t.get("action"),
                "allowed_role": t.get("allowed"),
                "allow_self_approval": self_approval_allowed(t),
            })
    self_approvable = [a for a in approving if a["allow_self_approval"]]
    risk = None
    if self_approvable:
        acts = ", ".join(sorted(repr(a["action"]) for a in self_approvable if a["action"]))
        roles = ", ".join(sorted(repr(a["allowed_role"]) for a in self_approvable
                                 if a["allowed_role"]))
        risk = (f"Workflow {workflow.get('name')!r} permits self-approval on approving "
                f"transition(s) {acts} (role {roles or 'unnamed'}) — frappe's "
                "allow_self_approval defaults to on, so this is not true separation of duties "
                "unless the company turned it off")
    return {"approving_transitions": approving, "sod": not self_approvable, "risk": risk}
