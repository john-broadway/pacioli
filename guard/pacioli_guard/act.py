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
:func:`_inside_another_write` for the mechanism, why it is nesting depth rather than a per-request
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
from pacioli_guard.scope import consent_verdict

SUBMIT = "submit"
CANCEL = "cancel"

# Frame names frappe uses when it writes ONE document, and the module they must belong to.
# KNOWLEDGE-PINNED against frappe 16 `model/document.py`: `insert` (:431) and `_save` (:552) each own
# a single document write and each call `run_before_save_methods`, which fires the handlers below.
# The MODULE is part of the signal, not decoration — see `_inside_another_write`.
_WRITE_FRAMES = frozenset({"_save", "insert"})
_DOCUMENT_MODULE = "frappe.model.document"


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
#   2. Structurally, a stamp alone licenses NOTHING. `_inside_another_write` only rides when a LIVE
#      `frappe.model.document` write frame is holding that document on this stack. A flag that
#      survived into redis cannot manufacture a frame, so a stale stamp cannot license a later act
#      the way stale per-request state could.
#
# (2) is the load-bearing one and it does not depend on frappe's caching behaviour staying put.
_CONSENT_ESTABLISHED = "pacioli_consent_established"


def _flag_get(doc, key):
    flags = getattr(doc, "flags", None)
    if flags is None:
        return False
    try:
        return bool(flags.get(key))
    except AttributeError:
        return bool(getattr(flags, key, False))


def _flag_set(doc, key):
    flags = getattr(doc, "flags", None)
    if flags is None:
        return
    try:
        flags[key] = True
    except (TypeError, AttributeError):
        try:
            setattr(flags, key, True)
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


def _inside_another_write(doc):
    """True when this act is happening INSIDE the write of a DIFFERENT document, i.e. it is a
    CONSEQUENCE of an act already being governed rather than an act of its own.

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
    `before_cancel` is ever run — so this gate does not see those acts at all. ERPNext sets it and
    changes docstatus anyway in at least two places, including a consolidated Sales Invoice CANCEL
    (`pos_invoice_merge_log.py:431-432`), which reverses GL entries. The flag is not remotely
    settable (`flags` is in `RESERVED_KEYWORDS` for both `update` and `set`, and `frappe.call`'s
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
        if other is not None and not _same_document(other, doc) and _flag_get(other, _CONSENT_ESTABLISHED):
            return True
        # A different document IS being written, but this gate never established consent for it.
        # Keep walking rather than concluding anything: an OUTER frame may still be the governed
        # act, and a consequence of a consequence legitimately rides the original.
        frame = frame.f_back
    return False


def _may_ride(doc, action):
    """May this act ride the consent established for the enclosing act it is a consequence of?

    Split by ACT, because the signal that answers "the human could not have named this document"
    only exists on one of them.

    **SUBMIT — require ``flags.in_insert``.** The document must have been CREATED by the act, not
    merely NAMED during it. Riding on "an enclosing governed act is in progress" alone licensed
    pre-existing drafts the caller named in the request body: submitting a Sales Invoice with
    ``update_stock`` carries the item row's ``serial_and_batch_bundle`` LINK straight out of the body
    (``stock_controller.py:1069``), and ``serial_batch_bundle.py:441-449`` then does
    ``frappe.get_doc("Serial and Batch Bundle", <that name>)`` and submits it. ``in_insert`` is
    frappe's own signal for the distinction, set at ``model/document.py:478`` immediately before
    ``run_before_save_methods()`` at ``:479`` fires ``before_submit`` and cleared at ``:482``.

    **CANCEL — ``in_insert`` is structurally impossible, so requiring it refuses everything.** That
    was the 0.9.4 regression, and it is the reason this function exists as its own predicate.
    ``in_insert`` is written in exactly one place in frappe 16 — ``Document.insert`` (``:478``/``:482``
    and ``:499``/``:507``). ``Document._cancel`` (``:1324-1326``) sets ``docstatus = 2`` and calls
    ``save()`` -> ``_save()``, which never sets it; and a document being cancelled has a name and is
    not ``__islocal``, so ``_save`` never delegates to ``insert()`` (``:571-572``). So the flag is
    FALSE at every ``before_cancel`` frappe can produce, so the ride condition is UNSATISFIABLE on
    that path by any input: a cascaded cancel could only ever fall through to the marker check and be
    refused, for a document name no human could have minted a marker against.

    **How badly that bites was measured, not assumed** (live, public bench, 2026-07-26 —
    ``deploy/bench/live-proof-095-cancel.py``). A governed Sales Invoice CANCEL on 0.9.4 **succeeded**:
    docstatus 2, no refusal. ERPNext reverses that ledger through ``make_reverse_gl_entries`` and
    ``db_set``-style writes which skip the document lifecycle entirely — the residual this module
    already publishes — so nothing reached ``before_cancel`` on a second document and there was
    nothing for the dead ride to deny. **The defect is therefore LATENT, not active**, and the first
    draft of this docstring claimed the opposite before the bench was asked. It bites the first time
    any code — ERPNext's or an adopter's — cancels a second document through the lifecycle inside a
    governed cancel. Fail-CLOSED either way, so never an escape.

    **STATED RESIDUAL, not a closed hole.** On the cancel path this restores exactly the 0.9.3
    predicate — an enclosing act that established its own consent licenses the cancels nested under
    it — so a cancel of a PRE-EXISTING document that a caller can steer ERPNext into performing
    inside a governed act would ride. No such lever is known (the amplification vector that motivated
    0.9.4 reaches ``get_doc(<name>).submit()``, not ``.cancel()``), but "none known" is not "none",
    and the honest place for that is here rather than a claim that it is closed. Narrowing it needs a
    signal that a cascaded cancel is a CONSEQUENCE of the enclosing document — frappe's own
    link-cancel machinery is the candidate, and it has not been walked yet, so it is not asserted.
    """
    if action == CANCEL:
        return True
    return _flag_get(doc, "in_insert")


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


def _presented_token():
    """The consent token this act carries, or ``None``.

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
    # `_inside_another_write` for why "already governed" is the load-bearing half.
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
    # ...but ONLY on the submit path. `in_insert` is structurally False at every `before_cancel`, so
    # requiring it there refused every cascaded cancel and took UNDO out with it. `_may_ride` owns
    # that split and states the residual it leaves.
    if _inside_another_write(doc) and _may_ride(doc, action):
        # Propagate custody: a cascade of a cascade (a GL Entry inside a Payment Ledger Entry
        # inside the consented invoice) must keep riding the original act rather than becoming
        # ungoverned once it is two levels deep.
        _flag_set(doc, _CONSENT_ESTABLISHED)
        return
    doctype = getattr(doc, "doctype", None)
    docname = getattr(doc, "name", None)
    record = _consent_record(doctype, docname) if doctype and docname else None
    allowed, reason = consent_verdict(_presented_token(), doctype, docname, action, record,
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
            f"(raw SQL, `db_update`/`db_set` field writes).",
        )
        return
    # The spend IS the single-use check — see `_claim_consent`. Losing it denies, and it fails
    # closed, so an unconfirmable spend never becomes a permitted write.
    if not _claim_consent(record):
        _deny(
            "consent (document layer)",
            f"This credential presented a consent marker for {doctype} {docname} that could not be "
            f"spent: it was already used by another request, or the spend could not be confirmed. "
            f"Markers are single-use — a human mints a fresh one with `pacioli mint`.",
        )
        return
    # Consent for THIS act is now established, so the framework's own consequences of it may ride.
    # Stamped only after the marker is actually spent: an act that was refused, or whose spend could
    # not be confirmed, must not license anything nested beneath it.
    _flag_set(doc, _CONSENT_ESTABLISHED)


def before_submit(doc, method=None):
    """``doc_events`` handler. ``method`` is optional because frappe's ``Document.hook`` composer
    inspects the signature and calls a handler either as ``f(doc, method)`` or ``f(doc)``."""
    _require_consent(doc, SUBMIT)


def before_cancel(doc, method=None):
    """As :func:`before_submit`, for the reversing act. Cancel writes reversing GL entries, so a
    marker minted to post a document must not spend on reversing it — that binding is
    ``consent_verdict``'s, and this passes it the act."""
    _require_consent(doc, CANCEL)
