# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Act-level consent, enforced at the DOCUMENT layer via ``doc_events``.

**Why this is not in ``auth_hooks`` any more.** Consent is a property of an ACT ON A DOCUMENT. The
HTTP auth hook is the wrong altitude for it in two ways that the 2026-07-25 bypass made concrete:

1. *Coverage.* ``check_scope`` only fires for a credential carrying an api-key ``Authorization``
   header, so OAuth ``Bearer``, desk/cookie sessions, background jobs, the scheduler, server scripts
   and the bench console reached the ledger without meeting the gate at all. Consent placed there
   inherited that boundary exactly.
2. *Legibility.* At the transport layer "is this a submit, and of what document" has to be inferred
   from a request shape, which is why the classifier carries ``?cmd=`` dominance, five body-carrying
   RPC rewrites, a ``savedocs`` action map, three REST mounts, container-DocType 2-hop deny-lists and
   a disclosed residual on raw ``docstatus`` writes. Here the same question is ``doc.doctype``,
   ``doc.name`` and the event name. No inference, so no residual.

**Verified against frappe 16 source before this was written** (paths below are frappe's own tree):
``_submit()`` sets ``docstatus = 1`` then ``save()`` -> ``_save()`` (``model/document.py:552``) ->
``run_before_save_methods()`` (``:587``) -> ``run_method("before_submit")`` (``:1407``);
``Document.hook`` (``:1606``) composes each app's ``doc_events`` handler around the document's own
method, and the composed runner wraps handler calls in ``try/finally``, **not** ``try/except``, so a
``frappe.throw`` here aborts the save. ``before_submit`` has exactly two call sites, ``:479``
(``insert``) and ``:587`` (``_save``), and ``set_new_name`` runs at ``insert``+44, before
``run_before_save_methods`` at ``insert``+49 — so ``doc.name`` is populated even for an
insert-as-submitted.

**What still does NOT reach this gate, stated rather than implied.** A write that skips the document
lifecycle entirely: raw SQL, and ``db_update``/``db_set``-style field writes. ERPNext core does this
itself when reposting (``landed_cost_voucher.py`` ``update_landed_cost``, which is what ERPNext
issues #50174 / #51281 / #48348 are about). So no single frappe extension point is a floor: coverage
is a composition, ``auth_hooks`` for what a credential may call and this for whether an act was
consented, with that residual published in the README rather than discovered later.

**The framework's own cascade rides the act it is a consequence of.** Submitting one business
document makes ERPNext submit others (`Payment Ledger Entry`, ledger machinery) inside that same
save, and a human cannot mint a marker for a document whose name does not exist until the submit is
already running. So consent is required for the OUTERMOST act only; nested writes carry it. See
:func:`_enclosing_governed_act` for the mechanism, why it is nesting depth rather than a per-request
stack, and which direction it fails in. Found by the live run on 2026-07-26 after 402 green unit
tests, because the test double was a single document and could not model a cascade.

**The ``enforce_workflow`` gate has the identical misplacement and has NOT moved.** It cannot yet:
``frappe.model.workflow.apply_workflow`` (``model/workflow.py:120``) sets no flag on the document
that this layer could read, so a handler here cannot tell a sanctioned workflow transition apart
from a direct submit — which is the entire distinction that gate makes. Moving it needs a
distinguishable signal first. Named here so nobody concludes it was covered.
"""
import sys
import time

import frappe

from pacioli_guard.enforce import (
    CONSENT_DOCTYPE,  # noqa: F401 — re-exported so operators/tests have one name for it
    CONSENT_HEADER,
    _claim_consent,
    _consent_record,
    _deny,
    _scope_from_doctype,
    _scope_from_legacy_field,
)
from pacioli_guard.scope import MINT_ROUTE_HINT, consent_verdict

SUBMIT = "submit"
CANCEL = "cancel"

# Frame names frappe uses when it writes ONE document, and the module they must belong to.
# KNOWLEDGE-PINNED against frappe 16 `model/document.py`: `insert` (:431) and `_save` (:552) each own
# a single document write and each call `run_before_save_methods`, which fires the handlers below.
# The MODULE is part of the signal, not decoration — see `_enclosing_governed_act`.
_WRITE_FRAMES = frozenset({"_save", "insert"})
_DOCUMENT_MODULE = "frappe.model.document"

# ERPNext's ledger-PREVIEW frames, and the module they must belong to. Deliberately a SEPARATE
# constant, recogniser and act-lookup from the write-frame set above, and `_enclosing_governed_act`
# is NOT touched. That function's two previous implementations BOTH FAILED OPEN (0.9.1 counted write
# frames; 0.9.2 accepted "a different document is being written"), so the new trust this feature
# needs goes in a new function whose own failure direction can be reasoned about on its own.
#
# KNOWLEDGE-PINNED against erpnext 16 `controllers/stock_controller.py`:
#   `show_accounting_ledger_preview` (:2058) and `show_stock_ledger_preview` (:2071) each
#   `doc.run_method("before_gl_preview" | "before_sl_preview")`, then call
#   `get_accounting_ledger_preview` (:2088) / `get_stock_ledger_preview`, then `frappe.db.rollback()`
#   (:2066 / :2080). The preview REALLY POSTS and then rolls the transaction back.
_PREVIEW_FRAMES = frozenset({
    "show_accounting_ledger_preview", "get_accounting_ledger_preview",
    "show_stock_ledger_preview", "get_stock_ledger_preview",
})
_PREVIEW_MODULE = "erpnext.controllers.stock_controller"


def _writing_document(frame):
    """The document a frappe document-write frame is writing, or None if this is not such a frame.

    Both halves matter. The NAME alone is ambiguous: `frappe.client.insert` is an unrelated
    whitelisted REST entry point that happens to share it. The MODULE pins the frame to frappe's own
    document machinery, where `self` is by construction the document being written.
    """
    if frame.f_code.co_name not in _WRITE_FRAMES:
        return None
    if frame.f_globals.get("__name__") != _DOCUMENT_MODULE:
        return None
    return frame.f_locals.get("self")


# Stamped on a document object once THIS gate has established consent for the act on it — either
# by spending a marker, or by establishing that it legitimately rides an act that already did.
#
# CORRECTED 2026-07-26. This comment used to claim `Document.flags` is "never persisted and never
# crosses a request". That is FALSE and frappe gives no such guarantee: `flags` is not in
# `UNPICKLABLE_KEYS` (`model/base_document.py:1591`, which holds only `_parent_doc` and the cached
# properties), and `get_cached_doc` pickles whole documents into redis for an hour
# (`model/document.py:2242-2260`). Two things hold instead, and they are narrower:
#
#   1. That cache is written IMMEDIATELY after a fresh `get_doc` on a miss (`:2248-2254`), strictly
#      before any handler has run, so the object written there carries no stamp yet.
#   2. Structurally, a stamp alone licenses NOTHING. `_enclosing_governed_act` only rides when a LIVE
#      `frappe.model.document` write frame is holding that document on this stack. A flag that
#      survived into redis cannot manufacture a frame, so a stale stamp cannot license a later act
#      the way stale per-request state could.
#
# (2) is the load-bearing one and it does not depend on frappe's caching behaviour staying put.
#
# Its VALUE is the act consent was established for (``SUBMIT`` / ``CANCEL``), not a bare True. A
# bare True was the 0.9.6 shape and it is what let a submit marker license a cancel: the ride
# decision could see THAT an enclosing act was governed but not WHICH act, so it could not refuse
# the crossing. See :func:`_may_ride`.
_CONSENT_ESTABLISHED = "pacioli_consent_established"

# Stamped on a document object that was CREATED inside a governed act, by :func:`after_insert`.
#
# Why this exists rather than reading ``flags.in_insert`` at submit time. ``in_insert`` answers "is
# ``Document.insert`` on the stack RIGHT NOW", not "did this act create this document": frappe
# clears it at ``model/document.py:482`` and ``:507``, so ERPNext's ordinary two-step idiom —
# ``doc.save()`` then ``doc.submit()`` — reaches ``before_submit`` with the flag FALSE on a document
# the act itself just created. That refused documents no human could ever mint a marker for and
# aborted the whole governed act (``serial_batch_bundle.py:1166``/``:1172``,
# ``depreciation.py:245-249``). ``in_insert`` was a PROXY for "created by this act" that held only
# for cascades that create and submit in a single ``insert()`` call.
#
# It carries the same narrow guarantees as ``_CONSENT_ESTABLISHED`` above, for the same reason: a
# stamp alone licenses NOTHING, because it is only ever READ when a live enclosing governed write
# frame is on this stack. A stamp that survived into redis cannot manufacture that frame.
_CREATED_IN_ACT = "pacioli_created_in_act"


def _flag_value(doc, key):
    """The raw stamp, or ``None``. ``_CONSENT_ESTABLISHED`` carries the ACT it was established for,
    not a bare True, because the ride decision needs to know which act it would be riding."""
    flags = getattr(doc, "flags", None)
    if flags is None:
        return None
    try:
        return flags.get(key)
    except AttributeError:
        return getattr(flags, key, None)


def _flag_get(doc, key):
    return bool(_flag_value(doc, key))


def _flag_set(doc, key, value=True):
    flags = getattr(doc, "flags", None)
    if flags is None:
        return
    try:
        flags[key] = value
    except (TypeError, AttributeError):
        try:
            setattr(flags, key, value)
        except Exception:  # noqa: BLE001 — a stamp we cannot record must not break the write
            pass


def _same_document(a, b):
    """Document identity, not OBJECT identity.

    `frappe.get_doc` returns a fresh instance on every call, and `load_doc_before_save` already
    builds a second object for the same document inside `_save` — so `a is b` is a fragile
    invariant to hang the whole gate on. Compared on (doctype, name), falling back to object
    identity when either name is missing, because two unsaved documents both named None are NOT
    the same document.
    """
    if a is b:
        return True
    a_name, b_name = getattr(a, "name", None), getattr(b, "name", None)
    if a_name is None or b_name is None:
        return False
    return (getattr(a, "doctype", None), a_name) == (getattr(b, "doctype", None), b_name)


def _enclosing_governed_act(doc):
    """The ACT (``SUBMIT``/``CANCEL``) of the nearest enclosing write of a DIFFERENT document that
    already established consent, or ``None``. Non-``None`` means this act is happening INSIDE
    another act, i.e. it is a CONSEQUENCE of an act already being governed rather than an act of
    its own.

    Returned as the act rather than a bool since 0.10.0: knowing an enclosing act was governed is
    not enough to decide the ride, because a marker binds to ONE act and a cascade can cross from
    one to the other. See :func:`_may_ride`.

    **Why this exists.** Found by the live run on 2026-07-26, and by no unit test: with the gate on
    and a valid marker present, a governed submit still failed. The Sales Invoice's own check passed,
    and then ERPNext's own accounting machinery — inside that same submit — created and submitted
    `Payment Ledger Entry` documents, and this app demanded consent for those too. A human cannot
    mint a marker for `Payment Ledger Entry ruq5vig9c2`: that name does not exist until the submit is
    already running. Consent binds to the ACT a human authorised, and the framework's consequences
    of that act are part of it.

    **Why "already governed" and not merely "a different document".** That was the 0.9.2
    implementation and it failed OPEN, found by redteam the same day. Only `before_submit` and
    `before_cancel` are gated, so a draft `save()` and an insert of a NON-submittable DocType are
    never gated and never need a marker — yet each puts a `frappe.model.document` write frame
    holding a different document on the stack. Anything submitted underneath one read as "a
    consequence" and skipped consent entirely, with no marker existing anywhere on the site:

      - `POST /api/resource/Subscription` (not submittable) with a back-dated `start_date` and
        `submit_invoice` set. `Document.insert` runs `after_insert` inside its own frame
        (frappe `model/document.py:498`), and `subscription.py:529` submits a Sales Invoice for
        every elapsed billing period. N invoices, N sets of GL entries, zero consent.
      - `POST /api/resource/Item` (not submittable) with `opening_stock` and `valuation_rate`.
        `item.py:192` submits a Stock Entry at a caller-chosen value.
      - (A third instance was cited here from `stock_reconciliation.py:227`. It is WRONG and the
        correction is kept rather than deleted: that submit sits behind `if save:`, `save` defaults
        False (`:169`), and no caller in the shipped tree passes True — so it does not fire in
        ERPNext 16. The class is real and the two instances above are verified; this one was not
        checked before it was written down.)

    So the enclosing frame must have **established consent for its own act** — spent a marker, or
    itself legitimately ridden one. That is stamped on the document object as
    `_CONSENT_ESTABLISHED` and propagated when an act rides, which keeps the chain of custody
    across a cascade of a cascade. An ungoverned outer write now proves nothing, which is the
    property the docstring claimed all along.

    **Why identity and not a frame COUNT.** Counting write frames was the 0.9.1 implementation and it
    failed OPEN too. Two top-level paths put two write-named frames on the stack for one actor
    writing one document:

      A. `frappe.client.insert` (`client.py:208`, whitelisted POST/PUT) -> `insert_doc` (`:511`) ->
         `frappe.get_doc(doc).insert()` (`:527`) -> `Document.insert`. TWO frames named `insert`, and
         a `docstatus: 1` body inserts an already-submitted document. Reachable with exactly the
         grant an operator is told to give the broker, which made it the 2026-07-25 bypass reopened.
      B. `Document._save` (`:571-572`) delegates to `self.insert()` for any NEW document, so
         submitting a new document is `_save` -> `insert` for a single document.

    Both read as depth 2 and skipped consent entirely. The count was never the property we wanted; it
    was a proxy for one, and the proxy had collisions. The question is not "how many write frames"
    but "is some enclosing frame writing a document that is not mine", which is what "my act is a
    consequence of another act" actually means. Same document across two frames is one act. A
    same-named function in another module is not a document write at all.

    **Why not a per-request stack.** A stack with an explicit close needs a pop point, and the only
    candidate is `on_submit` — which is exactly where ERPNext creates its ledger entries. Hook
    ordering inside one event would decide whether the pop ran before or after that cascade, and a pop
    that ran too early would let a BULK submit's documents 2..N ride document 1's marker. That is
    fail-OPEN in the one place this app cannot afford one. A frame walk needs no pairing, keeps no
    per-request state that could leak into the next act, and does not care about hook order.

    **A third residual, published late.** `Document.run_before_save_methods` returns early when
    `flags.ignore_validate` is set (`model/document.py:1399-1400`), BEFORE `before_submit` or
    `before_cancel` is ever run — so this gate does not see those acts at all. Instances verified in
    erpnext v16: a consolidated Sales Invoice CANCEL (`pos_invoice_merge_log.py:431-432`), which
    reverses GL entries, and a `Serial and Batch Bundle` SUBMIT (`stock_controller.py:2517-2521`).
    The second one was added 2026-07-28 and matters more than its line count suggests: the first is
    a POS edge case, the second sits in ordinary material-transfer flow, so an operator sizing this
    residual from the earlier text would have undercounted it. The flag is not remotely settable
    (`flags` is in `RESERVED_KEYWORDS` for both `update` and `set`, and `frappe.call`'s
    `get_newargs` pops it), so this is a coverage limit rather than an attacker lever — but it is a
    real third residual and the docstring previously claimed there were only two.

    **Direction of failure, stated.** If frappe renames these internals or moves the module, no frame
    is recognised as a document write, every act reads as top-level, and cascaded documents are
    REFUSED — the product breaks loudly instead of silently admitting an ungoverned write. That is
    the direction a gate is allowed to be wrong in, and it is why this is a frame walk
    (`sys._getframe`, no source lookup, cheap) rather than `inspect.stack()`.
    """
    frame = sys._getframe(1)
    while frame is not None:
        other = _writing_document(frame)
        if other is not None and not _same_document(other, doc):
            established = _flag_value(other, _CONSENT_ESTABLISHED)
            if established:
                return established
        # A different document IS being written, but this gate never established consent for it.
        # Keep walking rather than concluding anything: an OUTER frame may still be the governed
        # act, and a consequence of a consequence legitimately rides the original.
        frame = frame.f_back
    return None


def _previewing_document(frame):
    """The document an ERPNext ledger-PREVIEW frame is previewing, or None.

    Same two-part signal as :func:`_writing_document` and for the same reason: the name alone is
    ambiguous, the module pins it to ERPNext's own preview machinery where `doc` is by construction
    the document being previewed.
    """
    if frame.f_code.co_name not in _PREVIEW_FRAMES:
        return None
    if frame.f_globals.get("__name__") != _PREVIEW_MODULE:
        return None
    local = frame.f_locals
    return local.get("doc") if local.get("doc") is not None else None


def _previewing_governed_act(doc):
    """The act of an enclosing PREVIEW of a DIFFERENT document that already established consent.

    **Why this exists.** ERPNext previews a posting by performing it and rolling back
    (`stock_controller.py:2058-2066`). The posting creates and submits cascaded documents —
    `Payment Ledger Entry` first — and `before_submit` fires on each. Those documents do not exist
    until the preview runs, so no human could have minted a marker naming them; it is the same
    situation `_enclosing_governed_act` exists for, arriving down a path that is not a document
    write. During a preview the previewed document is NOT being written, so no
    `frappe.model.document` write frame holds it, and the write-frame walk correctly finds nothing.

    **Why it is a separate function.** `_enclosing_governed_act` has failed OPEN twice and its safety
    argument is precisely that a stamp licenses nothing without a live frappe write frame holding the
    document. Widening that function would put this feature inside that argument. This one is
    additive, is consulted only after the write-frame walk has already found nothing, and can be
    reasoned about and tested by itself.

    **What it trusts, stated narrowly.** One thing: that an enclosing frame belonging to ERPNext's
    preview module holds a document THIS gate has already stamped `_CONSENT_ESTABLISHED` — which
    only :func:`_establish_consent_for_preview` sets, and only after verifying a live, human-minted,
    document-and-act-bound marker for that exact document. No marker, no stamp, no ride.

    **Direction of failure.** If ERPNext renames or moves these functions, no preview frame is
    recognised, the cascaded documents fall through to the marker check, and the PREVIEW is refused.
    The product breaks loudly and nothing ungoverned is admitted. Same direction as the write walk.

    **Residual.** A preview does not spend the marker (see :func:`_establish_consent_for_preview`), so
    one marker can drive many previews inside its TTL. A preview commits nothing — ERPNext rolls the
    transaction back — so this buys an attacker repeated projections of a posting a human already
    authorised, not a posting. Stated rather than left implicit.
    """
    frame = sys._getframe(1)
    while frame is not None:
        other = _previewing_document(frame)
        if other is not None and not _same_document(other, doc):
            established = _flag_value(other, _CONSENT_ESTABLISHED)
            if established:
                return established
        frame = frame.f_back
    return None


def _may_ride(doc, action, enclosing_act):
    """May this act ride the consent established for the enclosing act it is a consequence of?

    One question decides both branches: **could a human have named this document when they minted
    the marker?** If yes, they could have been asked, so this act needs its own consent. If no, the
    act is a consequence and rides. The two branches differ only in which signal answers it.

    **SUBMIT — the document must have been CREATED by the act.** Riding on "an enclosing governed
    act is in progress" alone licensed pre-existing drafts the caller named in the request body:
    submitting a Sales Invoice with ``update_stock`` carries the item row's
    ``serial_and_batch_bundle`` LINK straight out of the body (``stock_controller.py:1069``), and
    ``serial_batch_bundle.py:441-449`` then does ``frappe.get_doc("Serial and Batch Bundle",
    <that name>)`` and submits it.

    ``flags.in_insert`` was the signal for that until 0.10.0, and it was a PROXY that did not hold.
    It answers "is ``Document.insert`` on the stack right now", and frappe clears it at
    ``model/document.py:482``/``:507``, so ERPNext's ordinary ``doc.save()`` then ``doc.submit()``
    idiom arrives here with the flag FALSE on a document the act itself created — refusing it and
    aborting the whole governed act (``serial_batch_bundle.py:1166``/``:1172``,
    ``depreciation.py:245-249``). So creation is now RECORDED when it happens, by
    :func:`after_insert`, and ``in_insert`` is kept only as the single-call case it always covered
    correctly. See ``_CREATED_IN_ACT``.

    **CANCEL — the enclosing act must itself be a CANCEL.** A cancel is always of a document that
    already exists and already has a name, so "was it created by the act" cannot discriminate here
    and ``in_insert`` is structurally FALSE at every ``before_cancel`` frappe can produce
    (``Document._cancel`` at ``:1324-1326`` sets ``docstatus = 2`` and calls ``save()`` -> ``_save()``,
    and a document being cancelled has a name and is not ``__islocal``, so ``:571-572`` never
    delegates back to ``insert()``). Requiring it refused every cascaded cancel and took UNDO out
    with it — the 0.9.4 regression.

    What discriminates instead is the ENCLOSING act. An UNDO legitimately cascades into further
    undos: cancelling an invoice makes ERPNext cancel the documents it generated
    (``accounts_controller.py:2001-2005`` cancels the system-generated credit/debit notes, the
    exchange gain/loss journal and the common-party journal, all through the lifecycle). A SUBMIT
    cascading into a cancel is a different thing, and it was a real bypass:

      - ``Sales Invoice.on_submit`` (``sales_invoice.py:507``) -> ``process_asset_depreciation`` ->
        ``depreciate_asset_on_sale`` (``:1508-1516``), which iterates the item rows and does
        ``frappe.get_doc("Asset", d.asset)`` on a BARE ``Link`` the caller supplies in the request
        body (no ``read_only``, no ``fetch_from``) -> ``depreciation.py:481`` ->
        ``asset_depreciation_schedule.py:215-217`` ``current_schedule.cancel()`` on a ``docstatus``
        1 document, through the lifecycle (only ``should_not_cancel_depreciation_entries`` is set,
        NOT ``ignore_validate``, so ``before_cancel`` does fire).
      - ``Unreconcile Payment.on_submit`` (``unreconcile_payment.py:59-64``) walks a child table the
        caller fills and reaches ``accounts/utils.py:857``/``:859`` ``gain_loss_je.cancel()`` on
        submitted Journal Entries. Behind a wider grant than slice-one, but the same shape.

    In both, one ``submit`` marker cancelled a caller-named pre-existing document with no marker of
    its own, and ``consent_verdict``'s marker-to-act binding never ran because the ride path does
    not call it. Requiring the enclosing act to match closes it: those cancels now fall through to
    the marker check, which is correct, because the schedule and the journal DO have names a human
    could be asked to approve.

    **What this costs, stated, and it is not one flow.** Every ERPNext path where a submit cascades
    into a lifecycle cancel now needs a marker for that cancel as well as for the act itself:
    asset sale (``sales_invoice.py:507``), partial-quantity asset sale (``:505`` ->
    ``asset.py:1462`` -> ``:1480``), a credit note against an asset sale (``:1503-1505`` ->
    ``restore_asset`` -> ``depreciation.py:498-500``), Asset Repair capitalization
    (``asset_repair.py:202`` -> ``:210``), Asset Shift Allocation
    (``asset_shift_allocation.py:51-52``), Asset Value Adjustment
    (``asset_value_adjustment.py:181-184``) and ``Unreconcile Payment`` (``:59-64``). Fail-CLOSED,
    and named in the refusal message rather than silent.

    That remedy has to be PRESENTABLE, and in the first cut of this fix it was not:
    ``_presented_consent`` read a single header value and ``consent_verdict`` compared it against the
    record for the document in hand, so a request could satisfy at most ONE marker while
    ``pacioli mint`` issues a fresh random token per marker. The cost was therefore not a second
    marker, it was a hard block with no ordering that worked, and this docstring said otherwise for
    the length of one review. The consent header now carries several markers; each is still bound to
    one document and one act and is still spent once.

    **The prior version of this docstring said "No such lever is known."** It was written without
    the sweep behind it and it was false; the levers above were in the shipped tree the whole time,
    found 2026-07-28 by reading erpnext v16 rather than this file. The candidate it named as the
    unwalked risk — frappe's own link-cancel machinery — turned out to be the SAFE one:
    ``cancel_all_linked_docs`` (``desk/form/linked_with.py:368``) is a flat loop, each cancel's frame
    pops before the next begins, so no enclosing write frame exists and every one of them falls
    through to the marker check.

    **Residual that remains, and it is not merely mechanical.** Under a governed CANCEL, a cascaded
    cancel of a PRE-EXISTING document still rides — the 0.9.3 predicate, restricted to one act. Most
    instances are the framework undoing its own consequences, but at least one is caller-steered in
    exactly the shape that was just closed on the submit side: ``Asset Repair.on_cancel``
    (``asset_repair.py:222``) calls ``cancel_sabb`` (``:215-220``), which does
    ``frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle).cancel()`` on a bare
    ``Link`` the caller fills in the ``stock_items`` child table. So "an undo may cascade into undos"
    is the JUSTIFICATION for this residual, not a description of everything it admits.

    It stays open because it is load-bearing for UNDO and cannot be narrowed without a signal that a
    cascaded cancel is a consequence OF THE ENCLOSING DOCUMENT specifically. frappe's link tables are
    the candidate and have not been walked, so nothing about them is asserted here.
    """
    if action == CANCEL:
        return enclosing_act == CANCEL
    return _flag_get(doc, _CREATED_IN_ACT) or _flag_get(doc, "in_insert")


def _gated(user):
    """Is ``user`` consent-gated? Returns the grant's flag as a bool.

    Reads the grant through ``enforce``'s own readers so there is ONE implementation of "what does
    this user's grant say", including the ``getattr``-with-``None`` handling for a doc loaded before
    ``bench migrate`` added a column, and the deprecated legacy-JSON fallback (a site mid-migration
    must not silently lose its consent gate).

    **STATED RESIDUAL — the gating question fails OPEN, and only this question.** This handler is
    registered on ``doc_events["*"]``, so it runs on every submit on the site: a human clicking
    Submit in the desk UI, and ERPNext's own internal submits. If an unreadable grant refused the
    act, one transient DB error would refuse every submit on a site that never opted into this app,
    and installing pacioli-guard would be indistinguishable from breaking the site. So a grant we
    cannot read reads as "not gated", which is the same posture the whole app already documents:
    no grant means stock frappe, and this only ever narrows a credential that has an explicit grant.
    Once gating IS established, every consent failure below denies. Consequence, said plainly: an
    actor who can break the grant read can skip this gate, and on the api-key paths the credential
    floor still applies to them.
    """
    try:
        scope = _scope_from_doctype(user)
        if scope is None:
            scope = _scope_from_legacy_field(user)
    except Exception:  # noqa: BLE001 — see the residual above; a `"*"` hook must not break a site
        return False
    return bool(scope is not None and getattr(scope, "require_consent", 0))


def _presented_consent():
    """The consent this act carries, or ``None``.

    Returns the raw header value. It may name ONE marker or several: one request can perform more
    than one gated act, since closing the act-crossing bypass means a caller-steered cascaded cancel
    needs its own human authorisation and it happens inside the enclosing act's request. Splitting
    is ``consent_verdict``'s (``_presented_candidates``), so the parsing rule lives in the pure core
    with the comparison it feeds and can be tested without frappe.

    ``None`` is the correct answer for a background job, a server script or the bench console: there
    is no request, so there is no header, and a gated principal that cannot present consent is
    refused by ``consent_verdict`` rather than waved through. Wrapped because reading a request
    header outside a request context is frappe's business, not ours, and an exception there means
    exactly the same thing as an absent header.
    """
    try:
        return frappe.get_request_header(CONSENT_HEADER)
    except Exception:  # noqa: BLE001 — no request context is an absent token, not an error
        return None


def _require_consent(doc, action):
    """Refuse ``action`` on ``doc`` unless a live, single-use marker for exactly this document AND
    this act was minted by a different principal, then spend it.

    Deliberately thin: every decision is the pure core's (``consent_verdict``), the marker load and
    the atomic spend are ``enforce``'s (``_consent_record`` / ``_claim_consent``), and the denial path
    is the same ``_deny`` that audits every other refusal this app makes. This function's only job is
    to hand the document layer's facts to those.
    """
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or not _gated(user):
        return
    # A consequence of an ALREADY-GOVERNED act carries the consent given for that act. See
    # `_enclosing_governed_act` for why "already governed" is the load-bearing half.
    # ...AND this document must have been CREATED BY that act, not merely named during it.
    #
    # CONSENT AMPLIFICATION (redteam 2026-07-26, second pass). Riding on "an enclosing governed act
    # is in progress" alone licenses more than the human approved. The failure that motivated the
    # exception was `Payment Ledger Entry ruq5vig9c2` — a document that DID NOT EXIST until the
    # submit ran, which is why no human could mint a marker for it. But the same predicate also
    # licensed PRE-EXISTING drafts the caller NAMED in the request body, which a human could have
    # been asked to approve. Reachable in the ordinary slice-one flow: submitting a Sales Invoice
    # with `update_stock` carries the item row's `serial_and_batch_bundle` LINK straight from the
    # request body (`stock_controller.py:1069`), and `serial_batch_bundle.py:441-449` then does
    # `frappe.get_doc("Serial and Batch Bundle", <that name>)` and submits it. One marker for one
    # invoice, and stock-ledger-moving documents the human never saw reach docstatus 1.
    #
    # `flags.in_insert` separates the two exactly, using frappe's own signal rather than new state:
    # it is set at `model/document.py:478`, immediately BEFORE `run_before_save_methods()` at :479
    # fires `before_submit`, and cleared at :482. A cascade-created document has it — verified on
    # real bytes for the motivating case, where `create_payment_ledger_entry` builds the PLE with
    # `frappe.get_doc(<dict>)` (no name, so `__islocal`) and `_save` delegates to `insert()`
    # (`:571-572`). A document loaded by name and submitted does NOT have it.
    #
    # ...but ONLY on the submit path, and "created by the act" is RECORDED at `after_insert` rather
    # than inferred from `in_insert` at submit time — see `_CREATED_IN_ACT` for why the flag alone
    # was a proxy that missed every two-step `save(); submit()` creation. On the cancel path the
    # discriminator is the ENCLOSING act instead, because a cancel is always of a document that
    # already has a name. `_may_ride` owns both splits and states the residual each leaves.
    enclosing_act = _enclosing_governed_act(doc)
    # Consulted ONLY when the write-frame walk found nothing, so this can never widen an answer the
    # existing walk already gave. A preview is not a document write, so its cascade arrives here with
    # no enclosing write frame at all. See `_previewing_governed_act`.
    if not enclosing_act:
        enclosing_act = _previewing_governed_act(doc)
    if enclosing_act and _may_ride(doc, action, enclosing_act):
        # Propagate custody: a cascade of a cascade (a GL Entry inside a Payment Ledger Entry
        # inside the consented invoice) must keep riding the original act rather than becoming
        # ungoverned once it is two levels deep. Stamped with THIS act, so a cancel nested under a
        # ridden submit is judged against the submit and refused rather than inheriting a cancel
        # authority from further up the stack.
        _flag_set(doc, _CONSENT_ESTABLISHED, action)
        return
    doctype = getattr(doc, "doctype", None)
    docname = getattr(doc, "name", None)
    record = _consent_record(doctype, docname) if doctype and docname else None
    allowed, reason = consent_verdict(_presented_consent(), doctype, docname, action, record,
                                      time.time(), user)
    if not allowed:
        _deny(
            "consent (document layer)",
            # STATES ITS OWN COVERAGE HONESTLY. This used to end "This gate runs on every path
            # that reaches the document lifecycle, not only on api-key REST calls." The second
            # clause is true and is why this moved off `auth_hooks`. The FIRST IS NOT, and this
            # module's docstring says so above: `run_before_save_methods` returns early on
            # `flags.ignore_validate` (frappe `model/document.py:1399-1400`), BEFORE the
            # `_action == "submit"` branch at `:1405` fires `before_submit` — so a write can reach
            # the document lifecycle and never reach here. An operator sizing a threat model on
            # "every path" would be sizing it on a guarantee this software does not give, in the
            # one message they are certain to read.
            f"This credential requires human consent to {action} a document, and {reason}. "
            f"Refused for {doctype} {docname}. The marker is presented in the {CONSENT_HEADER} "
            f"header. This gate runs at the document layer, so it covers paths an api-key check "
            f"cannot see (OAuth Bearer, desk sessions, background jobs, the scheduler, server "
            f"scripts, the bench console). It does NOT see a write that sets "
            f"`flags.ignore_validate`, or one that skips the document lifecycle entirely "
            f"(raw SQL, `db_update`/`db_set` field writes). {MINT_ROUTE_HINT}.",
        )
        return
    # The spend IS the single-use check — see `_claim_consent`. Losing it denies, and it fails
    # closed, so an unconfirmable spend never becomes a permitted write.
    if not _claim_consent(record):
        _deny(
            "consent (document layer)",
            f"This credential presented a consent marker for {doctype} {docname} that could not be "
            f"spent: it was already used by another request, or the spend could not be confirmed. "
            f"Markers are single-use — a human (not this credential) creates a fresh Pacioli "
            f"Consent Marker for this document and act, bound to a new token. {MINT_ROUTE_HINT}.",
        )
        return
    # Consent for THIS act is now established, so the framework's own consequences of it may ride.
    # Stamped only after the marker is actually spent: an act that was refused, or whose spend could
    # not be confirmed, must not license anything nested beneath it.
    _flag_set(doc, _CONSENT_ESTABLISHED, action)


def before_submit(doc, method=None):
    """``doc_events`` handler. ``method`` is optional because frappe's ``Document.hook`` composer
    inspects the signature and calls a handler either as ``f(doc, method)`` or ``f(doc)``."""
    _require_consent(doc, SUBMIT)


def _establish_consent_for_preview(doc):
    """Require, for a PREVIEW of a submit, the same consent the submit itself needs. Do not spend it.

    **Why consent moved to cover the preview** (John's ruling, 2026-07-29). ERPNext previews a posting
    by performing it and rolling back, so with consent enforced the preview's own cascade was refused
    and `plan_submit` could not complete. Every option that lets the preview through WITHOUT consent
    means teaching this gate to believe a caller's claim that a write will be rolled back, and the
    gate cannot verify that claim. So the preview is gated instead of exempted: previewing a submit
    requires the marker for that submit.

    **The consequence, stated because it changes the ceremony.** Consent is now minted BEFORE the plan
    rather than after it. A human authorises "submit this draft", then the projection is produced
    under that authority. They no longer see the projected GL before consenting — they see the draft,
    which is the document they are consenting to, and the projection becomes disclosure rather than a
    precondition. That is a real change to PLAN -> CONSENT -> PROVE and it is John's call, not an
    implementation detail smuggled in here.

    **Why the marker is NOT spent.** A preview commits nothing, and the marker has to survive for the
    act it previews or consenting once would authorise a projection instead of a posting. Single-use
    still means single POSTING: `_require_consent` spends it at the real submit. The cost is that one
    marker can drive many previews inside its TTL, which is stated in `_previewing_governed_act`.

    **Fails CLOSED and quietly on anything unexpected.** An ungated user returns immediately, so this
    is inert on any site that never opted in. A gated user with no valid marker is denied, which
    refuses the preview and nothing else.
    """
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or not _gated(user):
        return
    # Already established for this document in this request: a nested preview, or a preview inside a
    # governed act. Nothing to verify and nothing to re-stamp.
    if _flag_value(doc, _CONSENT_ESTABLISHED):
        return
    doctype = getattr(doc, "doctype", None)
    docname = getattr(doc, "name", None)
    record = _consent_record(doctype, docname) if doctype and docname else None
    # SUBMIT, not a new "preview" act: the human authorised a posting, and this is that posting being
    # rehearsed. Inventing a third act would mean a marker that authorises a preview and nothing
    # else, which is a second ceremony for no gain.
    allowed, reason = consent_verdict(_presented_consent(), doctype, docname, SUBMIT, record,
                                      time.time(), user)
    if not allowed:
        _deny(
            "consent (preview)",
            f"This credential requires human consent to submit a document, and {reason}. Refused "
            f"for the ledger PREVIEW of {doctype} {docname}. ERPNext previews a posting by "
            f"performing it and rolling back, so a preview needs the same marker as the submit it "
            f"previews. A human (not this credential) must create a Pacioli Consent Marker on this "
            f"site for this exact document with action '{SUBMIT}', bound to a token they generate, "
            f"and that token is presented in the {CONSENT_HEADER} header on the plan call. It is "
            f"NOT spent by the preview, so the same marker still spends on the submit itself. "
            f"{MINT_ROUTE_HINT}.",
        )
        return
    # Stamped WITHOUT spending. `_previewing_governed_act` is what reads this, and only for documents
    # the preview itself creates.
    _flag_set(doc, _CONSENT_ESTABLISHED, SUBMIT)


def before_gl_preview(doc, method=None):
    """``doc_events`` handler on ERPNext's ACCOUNTING-ledger preview entry point.

    `show_accounting_ledger_preview` calls `doc.run_method("before_gl_preview")`
    (`stock_controller.py:2062`) BEFORE it posts, and frappe's `run_method` composes
    `doc_events["*"][method]` (`model/document.py:1252` -> `hook.composer` :1643-1650), so this fires
    ahead of the posting it needs to authorise.
    """
    _establish_consent_for_preview(doc)


def before_sl_preview(doc, method=None):
    """As :func:`before_gl_preview`, for the STOCK-ledger preview (`stock_controller.py:2076`).

    Registered for the same reason: `show_stock_ledger_preview` also posts and rolls back (:2080),
    so it can cascade submits the same way.
    """
    _establish_consent_for_preview(doc)


def before_cancel(doc, method=None):
    """As :func:`before_submit`, for the reversing act. Cancel writes reversing GL entries, so a
    marker minted to post a document must not spend on reversing it — that binding is
    ``consent_verdict``'s, and this passes it the act. Since 0.10.0 the RIDE path enforces the same
    binding independently (:func:`_may_ride`), because riding never reaches ``consent_verdict`` at
    all and a submit marker was licensing cascaded cancels through it."""
    _require_consent(doc, CANCEL)


def after_insert(doc, method=None):
    """``doc_events`` handler. Records that ``doc`` was CREATED inside an act that already
    established consent, so a later ``submit()`` on the same object can be recognised as part of
    that act rather than as a new act needing its own marker.

    **Why a recording hook and not a read of ``flags.in_insert``.** ``in_insert`` is true only while
    ``Document.insert`` is on the stack, and frappe clears it at ``model/document.py:482``/``:507``.
    ERPNext's ordinary cascade idiom is two calls — ``doc.save()`` then ``doc.submit()`` — so by the
    time ``before_submit`` runs, the flag is gone and the document looks exactly like a pre-existing
    one the caller named. That refused documents no human could mint a marker for and aborted the
    enclosing governed act. Recording at creation is the only point where the distinction is still
    visible.

    **Placed at ``after_insert``** (``model/document.py:498``) rather than ``before_insert``
    (``:473``): it runs inside ``insert``'s own frame, so the frame walk still sees the enclosing
    governed act, and ``set_new_name`` (``:474``) has already run, so the document has the name that
    any later refusal message would have to quote.

    **This handler grants nothing on its own.** It writes a stamp that is only ever READ when a live
    enclosing governed write frame is on the stack (:func:`_may_ride` is unreachable otherwise), and
    it writes it only when such a frame is ALREADY there. An insert with no governed act enclosing
    it leaves no stamp, so an ungoverned creation cannot manufacture a later ride.

    **Both walks, for the same reason :func:`_require_consent` needs both.** Inside a ledger PREVIEW
    the previewed document is not being written, so no ``frappe.model.document`` write frame holds it
    and the write walk correctly finds nothing. A preview that creates a document in ERPNext's
    ordinary two-step idiom therefore recorded no creation, and the later ``submit`` arrived looking
    exactly like a pre-existing draft the caller named — refused, taking the whole preview with it
    (``serial_batch_bundle.py:1166``/``:1172``, reached from a preview with ``update_stock``). Added
    0.13.0, after the asymmetry was reproduced against real frames rather than argued from the code.

    This widens nothing that the preview walk did not already license at :func:`_require_consent`:
    the stamp is still only ever READ by :func:`_may_ride`, which is unreachable unless an enclosing
    governed act — write or preview — is found on the live stack at submit time. A pre-existing draft
    the caller named inside a preview still gets no stamp, because ``after_insert`` never runs for it.

    Deliberately does NO grant read and NO database work: this is registered on ``doc_events["*"]``
    and therefore runs on every insert on the site, including on sites that never opted into this
    app. Two ``sys._getframe`` walks with no source lookup are the whole cost, and the second runs
    only when the first found nothing.
    """
    if _enclosing_governed_act(doc) or _previewing_governed_act(doc):
        _flag_set(doc, _CREATED_IN_ACT)
