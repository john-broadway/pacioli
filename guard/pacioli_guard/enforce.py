# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Guard — frappe glue: enforce a credential's ApiScope at the ``auth_hooks`` chokepoint.

Registered via ``hooks.py`` ``auth_hooks``. Frappe runs auth hooks inside ``validate_auth()``,
AFTER the api-key authenticates (sets the user) and BEFORE the request dispatches — and without a
try/except around the hook loop, so a ``frappe.PermissionError`` raised here becomes a real 403.
**No frappe core files are modified** — this ships as an installable app.
"""
from __future__ import annotations

import datetime
import json
import math
import time
import zoneinfo

import frappe

from pacioli_guard.scope import (
    APPLY_WORKFLOW_METHOD,
    ApiScope,
    api_key_from_auth_header,
    body_scoped_target,
    classify,
    # NOTE: `consent_verdict`, `docstatus_action` and `docstatus_target_docname` are no longer
    # imported here. They were the transport layer's way of inferring the consented act out of a
    # request shape; the document layer reads it off the document instead (`pacioli_guard.act`).
    # They remain part of the pure core's tested surface, they are simply no longer load-bearing.
    docstatus_target_doctype,
    is_docstatus_changing,
    is_permitted,
    is_rate_allowed,
    method_target_resolved,
)

# The grant is read from the dedicated **API Key Scope** DocType (one per credential-owning User,
# with child-table method/DocType allowlists). The legacy prototype stored the same grant as a JSON
# blob in an ``api_scope`` field on User; that path is kept as a deprecated one-version fallback so
# existing setups keep enforcing while they migrate. Either source shapes the SAME ``ApiScope`` via
# the pure core — ``scope.py`` is untouched.
SCOPE_DOCTYPE = "API Key Scope"
LEGACY_SCOPE_FIELD = "api_scope"
# The consent floor. A marker is minted off-box by a human hand and lands here as a hash bound
# to one document; the gate spends it once. See `consent_verdict` in scope.py for why credential
# scope alone cannot close the direct-submit bypass.
CONSENT_DOCTYPE = "Pacioli Consent Marker"
CONSENT_HEADER = "X-Pacioli-Consent"


def _scope_for_request():
    """Return the ``ApiScope`` for the credential authenticating THIS request, or ``None``.

    Gated on an api-key ``Authorization`` header being present (``token``/``Basic`` — the schemes
    ``api_key_from_auth_header`` recognises). Plain desk/cookie sessions and ``Bearer``/OAuth carry
    no api-key here and return ``None`` (left completely untouched).

    The scope subject is ``frappe.session.user`` — the identity the request ACTUALLY executes as.
    By the time frappe runs ``auth_hooks`` it has already authenticated the credential and settled
    ``session.user`` (``validate_auth`` runs the api-key/OAuth auth BEFORE ``validate_auth_via_hooks``),
    INCLUDING resolving a ``Frappe-Authorization-Source`` non-User doctype to its owning user. Reading
    frappe's already-settled identity — rather than re-deriving it from the header — means the enforced
    scope can never diverge from the executing principal (if a session cookie overrode the api key, we
    scope the cookie's user, which is what the request runs as — fail-safe), and needs no version-
    fragile mirror of frappe's own resolution. ``Guest``/empty (the credential didn't authenticate,
    e.g. a malformed token frappe rejected) returns ``None``: we no-op and frappe's own final guard
    401s the request. A ``None`` return means *unscoped*; the DocType grant is read first, legacy second.
    """
    if not api_key_from_auth_header(frappe.get_request_header("Authorization")):
        return None
    user = frappe.session.user
    if not user or user == "Guest":
        return None
    scope = _scope_from_doctype(user)
    if scope is not None:
        return scope
    return _scope_from_legacy_field(user)


def _scope_from_doctype(user):
    """Build the ``ApiScope`` from the user's *API Key Scope* DocType grant, or ``None`` if the user
    has no such grant (genuinely unscoped). The frappe wall only plucks primitives off the doc and
    its child rows; ``ApiScope.from_grant`` does all the security-relevant shaping.
    """
    name = frappe.db.get_value(SCOPE_DOCTYPE, {"user": user}, "name")
    if not name:
        return None
    doc = frappe.get_doc(SCOPE_DOCTYPE, name)
    # Per-credential resource-verb narrowing. A migrated doc carries the four Check fields (default
    # 1 = all verbs, so a pre-narrowing grant is unchanged); the operator unticks a verb to deny it,
    # and unticking all four denies all resource CRUD (respected, not silently widened). A doc loaded
    # before migrate added the columns has NO verb attributes — pass None so the pure core reads it
    # as unspecified = all verbs (absence is not a narrowing), keeping a pre-narrowing install green.
    if hasattr(doc, "verb_read"):
        resource_verbs = [v for v in ("read", "create", "write", "delete")
                          if getattr(doc, f"verb_{v}", 1)]
    else:
        resource_verbs = None
    return ApiScope.from_grant(
        doc.allow_resource,
        [row.pattern for row in (doc.methods or [])],
        [row.ref_doctype for row in (doc.resource_doctypes or [])],
        # CONTAIN pair. getattr-with-None: a doc loaded before `bench migrate` added the columns
        # has neither attribute — the pure core reads None as enabled / no-limit (absence is not
        # a kill), so a pre-CONTAIN install keeps working through an upgrade.
        enabled=getattr(doc, "enabled", None),
        rate_limit_per_minute=getattr(doc, "rate_limit_per_minute", None),
        resource_verbs=resource_verbs,
        # Opt-in Workflow-bypass gate (belt alongside the broker's own agent-path gate — see
        # workflow.py's "Honest limit #1"). getattr-with-None: a doc loaded before `bench migrate`
        # added the column has no attribute at all — the pure core reads None as OFF (absence is
        # not an opt-in), so a pre-Workflow-gate install keeps working through the upgrade with
        # the new gate silently off until turned on per-credential.
        enforce_workflow=getattr(doc, "enforce_workflow", None),
        # The consent gate, threaded exactly like enforce_workflow above and for the same reason:
        # a doc loaded before `bench migrate` added the column has no attribute at all, and the
        # pure core must read that absence as OFF. A gate that switched itself on during an
        # upgrade would start refusing every submit a live broker makes.
        require_consent=getattr(doc, "require_consent", None),
        # The site-wide resource grant, threaded exactly like the two gates above but for the
        # MIRROR reason: those read None as OFF so an upgrade never starts REFUSING; this reads
        # None as OFF so an upgrade never starts GRANTING. A doc written before `bench migrate`
        # added the column has no attribute at all, and that absence must not widen a live
        # credential to every DocType on the site.
        allow_all_doctypes=getattr(doc, "allow_all_doctypes", None),
    )


def _scope_from_legacy_field(user):
    """Deprecated fallback: the prototype JSON blob on ``User.<LEGACY_SCOPE_FIELD>``. Returns
    ``None`` when the field is absent (DocType-only install) or empty — for a user with no grant at
    all, unscoped is the intended stock behaviour, identical to pre-app Frappe.
    """
    if not frappe.db.has_column("User", LEGACY_SCOPE_FIELD):
        return None
    raw = frappe.db.get_value("User", user, LEGACY_SCOPE_FIELD)
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return ApiScope.from_dict(raw)


def _deny(reason, message):
    """Deny the request: best-effort audit row, then the 403 throw — in that order, decoupled.

    The log write is wrapped so **a failure to LOG can never suppress the DENY**: whatever
    ``frappe.log_error`` raises (Error Log itself broken, DB write refused mid-migration, log
    rotation racing), the ``PermissionError`` still fires. Logging is fail-open; denying is not.
    Uses frappe's own ``log_error`` (the stock *Error Log* DocType) rather than a bespoke log
    DocType — see the changelog draft for the call.
    """
    try:
        frappe.log_error(
            title=f"Pacioli Guard denied a request ({reason})",
            message=message,
        )
    except Exception:
        pass  # a broken audit trail must never become a broken boundary
    frappe.throw(message, frappe.PermissionError)


def _rate_window_count(user):
    """Count this request against ``user``'s fixed one-minute window and return the new count
    (including this request), or ``None`` if the cache is unusable.

    Fixed windows are the honest-and-cheap choice: one ``INCR`` + ``EXPIRE`` per request, no
    sliding-log storage — at the cost of the classic boundary burst (a credential can spend a
    full budget in the last second of one window and another in the first second of the next,
    briefly 2× the nominal rate). That is acceptable for velocity *damping*; this is not a meter.
    """
    key = f"pacioli_guard|rate|{user}|{int(time.time() // 60)}"
    try:
        cache = frappe.cache()
        count = cache.incr(key)
        cache.expire(key, 120)  # windows self-clean; 2x the window so a live key never lapses early
        return int(count)
    except Exception:
        return None


# Sentinel: the internal workflow-existence lookup itself raised. Distinct from `None`/`""`
# ("no active workflow" — a real, legitimate answer) so the deny-biased branch in check_scope can
# tell "confirmed no workflow" apart from "couldn't confirm anything" without conflating the two.
_WORKFLOW_LOOKUP_FAILED = object()


def _active_workflow_name(doctype):
    """Internal (frappe-cached) Workflow-existence lookup for the ``enforce_workflow`` gate.

    Calls ``frappe.model.workflow.get_workflow_name(doctype)`` directly — this hook runs as
    frappe-internal code (inside ``validate_auth``, after the api-key has already authenticated),
    so it reads frappe's own workflow machinery with NO System-Manager REST wall (unlike the
    broker, which has to go through a permissioned API call to read Workflow config) and with NO
    recursion risk (``auth_hooks`` fire once per HTTP request; this is a plain internal
    ORM/cache call, not another request re-entering ``validate_auth``).

    KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (mirrors ``broker/pacioli/workflow.py``'s own "Honest
    limit #2" register): that ``get_workflow_name`` exists at this import path, is frappe-cached,
    and returns a falsy value (``None`` or ``""`` — frappe's own source has returned both across
    versions) for "no active workflow" and a workflow-name string otherwise, was read from frappe
    source, not exercised against a live bench. Live falsification is a future bench gate, not
    implied by anything here.

    Also knowledge-pinned: unlike the broker's own ``find_active`` (which explicitly detects and
    refuses on more than one active Workflow for a doctype — an :class:`~pacioli.workflow.Ambiguous`
    sentinel), this frappe-internal function is NOT known to expose that ambiguity at all — if a
    site somehow carries more than one active Workflow for one doctype, this lookup silently
    returns whichever one frappe's own cache/query happened to pick. This gate has no way to
    surface that the way the broker's pure core does; it is not assumed to mirror those sentinels.

    Deny-biased on error: if the lookup itself raises (frappe internals unavailable, a DB hiccup,
    version drift), returns :data:`_WORKFLOW_LOOKUP_FAILED` rather than propagating or guessing —
    the caller treats that as "assume governed" for a call that already looks docstatus-changing.
    An unverifiable answer is never read as "no workflow", matching the house rule the broker's
    pure core states explicitly (Malformed/Ambiguous both refuse, never guess).
    """
    try:
        return frappe.model.workflow.get_workflow_name(doctype)
    except Exception:
        return _WORKFLOW_LOOKUP_FAILED


def check_scope():
    """``auth_hook`` entrypoint. No-op for unscoped credentials; 403 for out-of-scope ones.

    NOTE (honest scope — the surface this DOES cover, and the known gaps):
    - Covers the HTTP REST surface across all three frappe mounts: bare ``/api``, the ``/api/v1``
      alias, and ``/api/v2`` (``/document/`` CRUD + path-carried doc-methods) — ``/method`` calls,
      ``/resource``|``/document`` CRUD, and doc-method calls (v1 ``run_method`` / v2 ``.../method/<m>``).
    - SUBJECT: the scope enforced is that of ``frappe.session.user`` — the identity the request runs
      as. Frappe settles it (api-key/OAuth auth, incl. ``Frappe-Authorization-Source`` resolution)
      BEFORE this hook, so the scope can't diverge from the executing principal (if a session cookie
      overrode the api key, we scope the cookie's user — fail-safe).
    - Credential SCHEMES (which auth forms open the scoping gate):
        * ``token <key>:<secret>`` / ``Basic base64(<key>:<secret>)`` (``curl -u``) — SCOPED.
        * ``Frappe-Authorization-Source`` header (key bound to a NON-User doctype) — SCOPED: frappe
          resolves it into ``session.user`` before this hook, so the credential is scoped to its
          owning user with no re-derivation here.
        * ``Bearer <oauth-token>`` (OAuth2) — NOT scoped (deliberate; the gate closes because
          ``api_key_from_auth_header`` ignores bearer). OAuth carries frappe's own scopes; governing
          it is a separate leg ("OAuth Token Scope"), stated not silently skipped.
    - NOT covered (out of band, never fail-open): internal ``frappe.client`` RPC and background jobs
      (non-credential context — no Authorization header, so ``_scope_for_request`` returns None).
    - **Deny-unknown method scoping**: a ``methods`` grant is honored on a ``kind == "method"`` call
      only when the target is doctype-RESOLVED — either the URL/route itself carried the doctype
      (v1 item ``run_method``, v2 path-carried doc-method, v2 two-segment controller method, or a
      body-doctype rewrite via ``body_scoped_target`` below), or the bare name is one of the tiny
      curated ``SAFE_METHODS`` (``scope.py``) — everything else is denied even if a pattern in
      ``scope.methods`` would otherwise fnmatch it. This closes the generic-RPC footgun at its root:
      a pure classifier cannot enumerate every dangerous bare RPC, so an unresolved grant is
      denied-until-reviewed rather than open-until-enumerated. ``body_scoped_target`` (``scope.py``)
      additionally rewrites the body-carrying RPCs — ``frappe.client.submit``/``.cancel``, the Desk
      ``savedocs``/``.submit``/``.cancel``/``.discard``, the bulk submit/cancel RPC,
      ``frappe.model.workflow.apply_workflow``, ``run_doc_method`` (EVERY inner method, not just
      submit/cancel/discard), ``frappe.client.insert``/``.save``, the multi-doc saves
      (``bulk_update``, ``frappe.client.insert_many``, ``frappe.client.bulk_update``) and
      ``frappe.desk.form.linked_with.cancel_all_linked_docs`` — to the same per-doctype ``("method", "<DocType>.<verb>")`` shape the
      URL-path ``run_method`` vector already produces, so a credential granted only
      ``"Sales Invoice.submit"`` can no longer submit a Journal Entry through
      ``frappe.client.submit``, and a credential granted only ``"Sales Invoice.get_pdf"`` can no
      longer reach ANY other doctype's ``get_pdf`` (or any other method) through bare
      ``run_doc_method``. HARD-DENIED regardless of grant or resolution, checked BEFORE the grant
      and folded case-and-accent (see ``_fold_doctype``), in two sets:
        * ``_UNGRANTABLE_METHOD_DOCTYPES`` (method branch only) — the 2-hop laundering vectors whose
          own instance method reads its target doctype from the SAVED RECORD or the body, never the
          classifiable request: ``Bulk Update``, ``Data Import``, ``Bank Statement Import``,
          ``Unreconcile Payment``, ``Repost Accounting Ledger`` (John's ruling 2026-07-10). STILL
          DISCLOSED, not closed: ``Payment Reconciliation`` is NOT in that set because the broker's
          own reconcile needs it and its hostile use is byte-identical to the legitimate one — it
          stays an operator rule (grant only to the broker's credential).
        * ``_UNGRANTABLE_DOCTYPES`` (BOTH branches) — this app's own control plane:
          ``API Key Scope`` + its two child tables, and ``Pacioli Consent Marker``. A credential
          that can write its own grant is not scoped (it can widen ``methods`` or untick
          ``require_consent``, and the grant doc autonames ``field:user`` so its name is guessable),
          and one that can insert a marker mints its own consent. Ungrantable, not merely ungranted
          — an operator cannot hand these out by listing them (floor audit F2, 2026-07-26).
    - RESOURCE branch semantics: an EMPTY ``resource_doctypes`` allowlist DENIES, matching
      ``methods``. It previously granted every DocType on the site, so ticking the master
      ``allow_resource`` Check and saving before filling the table was the widest grant this app
      could express. Site-wide access is still expressible but is stated with the parent-level
      ``allow_all_doctypes`` Check, and it does not reach the hard-denied control plane above
      (floor audit F1, 2026-07-26). ⚠️ This named a literal ``"*"`` row until 2026-08-11; that
      gesture is unstorable (``ref_doctype`` is a validated Link) and was retired on 2026-07-29 —
      see ``scope.py``'s own correction.
      The ``enforce_workflow`` gate below judges the SAME body-doctype-rewritten target (since
      0.5.1 — see the ``wf_kind``/``wf_target`` note at that gate), so a workflow-governed submit/
      cancel on a doctype named only in the request body (Journal Entry rides EXCLUSIVELY on this
      path) is caught, not just the URL-path ``run_method`` shape. Its remaining residual is narrow:
      a raw ``docstatus``-field write (``frappe.client.set_value`` on ``docstatus``) is not rewritten
      — mitigated because the base deny-unknown gate above denies such bare methods outright, and the
      raw-REST ``PUT …?docstatus=`` path is caught separately. See the README/CHANGELOG.

    CONTAIN order (kill → rate → scope → workflow), each with a distinct message and an audit row
    via ``_deny``:
    - **Kill switch** first and unconditional: a disabled grant denies before anything else is
      even read (no cache touch, no classification).
    - **Rate** counts EVERY request the scoped credential makes — permitted or not — because the
      limit contains the credential's total velocity, not its success rate. A cache failure while
      a limit is set fails CLOSED for that credential: whoever set a limit opted into containment,
      and an uncountable window can't honestly be called under it.
    - **Workflow bypass (opt-in, off by default — `ApiScope.enforce_workflow`)**: runs AFTER the
      existing scope allowlist, on an already-permitted call. When on, a docstatus-changing call
      (``submit``/``cancel`` by method name — covering the v1 ``run_method``, v2 path-doc-method,
      and legacy ``?cmd=`` routes alike — or a raw ``PUT``/``PATCH`` carrying a ``docstatus`` key)
      against a doctype with an active Frappe Workflow is refused unless the call IS
      ``frappe.model.workflow.apply_workflow``. This upgrades "governs the agent's path" (the
      broker's own gate, ``pacioli.workflow`` — see its "Honest limit #1") to "governs every
      **api-key** path through this credential" — but it is still only a credential-layer
      boundary: OAuth Bearer, desk/cookie sessions, background jobs, and the bench console are
      out of band here exactly as they are for every other gate in this hook (see "Credential
      SCHEMES" and "NOT covered" above). Off by default per-credential: turning a NEW gate on can
      newly deny previously-passing calls the moment it's flipped, so a site-wide or default-on
      posture would break live credentials on upgrade — the same lesson CONTAIN's fields learned.
    """
    scope = _scope_for_request()
    if scope is None:
        return
    user = frappe.session.user
    # `_deny` always raises frappe.PermissionError — but the explicit `return` after each call means
    # control flow never *depends* on that external contract. If a future frappe ever made `throw`
    # fall through for some input, a denied request would still stop here, not slide into the next
    # gate (the kill/rate cases have no downstream re-check the way scope does via is_permitted).
    if not scope.enabled:
        _deny(
            "kill switch",
            f"This credential's API Key Scope ({user}) is disabled. "
            "Every request is denied until it is re-enabled.",
        )
        return
    if scope.rate_limit_per_minute > 0:
        count = _rate_window_count(user)
        if count is None or not is_rate_allowed(scope.rate_limit_per_minute, count):
            _deny(
                "rate limit",
                f"This credential is over its rate limit of "
                f"{scope.rate_limit_per_minute} requests per minute."
                + ("" if count is not None else " (rate counter unavailable — failing closed)"),
            )
            return
    req = getattr(frappe.local, "request", None)
    if req is None:
        # Floor audit F6 (2026-07-26). This used to `return`, which skipped the scope allowlist,
        # the workflow gate and the consent gate entirely — after the kill switch and rate limit
        # had already run and passed. Almost certainly unreachable (a scope only exists here
        # because an Authorization header was readable, which needs a request), but a fail-OPEN
        # inside a fail-closed file must not rest on that inference: if it ever does become
        # reachable, an unclassifiable call from a scoped credential is a refusal.
        _deny(
            "no request context",
            "This credential is scoped, but the request context could not be read, so this call "
            "could not be classified against its allowlist. A scoped credential fails closed "
            "rather than skip its own gates.",
        )
        return
    form = frappe.form_dict or {}
    run_method = form.get("run_method")
    cmd = form.get("cmd")  # legacy RPC: frappe routes on cmd BEFORE the path — it is the real target
    kind, target = classify(req.path, req.method, run_method, cmd)
    # Body-doctype rewrite (NOT submit/cancel only — `body_scoped_target` also rewrites
    # apply_workflow, discard, and EVERY inner method of run_doc_method; the "submit/cancel only"
    # this said until 2026-08-11 was contradicted by that function's own docstring in the same
    # package): frappe.client.submit/.cancel, run_doc_method, and
    # savedocs Submit/Update/Cancel carry their real target doctype in the request BODY, invisible
    # to `classify`'s pure (path, http_method, run_method, cmd) signature — the generic-RPC footgun.
    # This resolves them to the SAME ("method", "<DocType>.submit"/"cancel") shape the URL-path
    # run_method vector already produces, into NEW perm_kind/perm_target variables.
    #
    # ⚠️ This used to end "`kind`/`target` below (the enforce_workflow gate) are DELIBERATELY left
    # untouched". Not since 0.5.1: the workflow gate ~30 lines below reads
    # `wf_kind, wf_target = (kind, target) if body_target is None else body_target`, i.e. it judges
    # the REWRITTEN target, and this module's own header records that change. The comment survived
    # the code it described.
    body_target = body_scoped_target(kind, target, req.method, form)
    perm_kind, perm_target = (kind, target) if body_target is None else body_target
    # Deny-unknown provenance: a body-doctype rewrite (body_target is not None) IS a resolution --
    # the doctype came from the request body, not a caller-asserted string. A non-"method" kind never
    # needs the method-resolution signal (resource/other take their own branches in is_permitted).
    # Otherwise fall back to method_target_resolved's read of the SAME classify() traversal, so a
    # v1/v2 URL-path-resolved doc-method (run_method / two-segment / path-carried) is honored and a
    # bare /api/method/<name> or ?cmd= is not, unless that bare name is on SAFE_METHODS.
    method_resolved = (
        body_target is not None
        or perm_kind != "method"
        or method_target_resolved(req.path, req.method, run_method, cmd)
    )
    if not is_permitted(scope, perm_kind, perm_target, method_resolved=method_resolved):
        _deny(
            "out of scope",
            f"This credential is scoped and is not permitted to call this endpoint "
            f"({perm_kind}: {perm_target}).",
        )
        return
    # The workflow gate must judge the REAL target, not classify()'s pre-rewrite one. A body-doctype
    # submit/cancel (frappe.client.submit/.cancel, the Desk cancel, bulk submit/cancel, run_doc_method,
    # savedocs) classifies as a generic method name like "frappe.client.submit" — feeding THAT to the
    # gate yields doctype "frappe.client", so no workflow is ever found and the gate silently no-ops.
    # Journal Entry rides EXCLUSIVELY on this path (its overridden submit/cancel aren't whitelisted), so
    # without this it would be the one doctype with zero workflow protection. Use the rewritten
    # ("method","<DocType>.submit") target when body_scoped_target produced one; a body_target that
    # fails closed already denied above (is_permitted), so here it is None or a real rewrite.
    wf_kind, wf_target = (kind, target) if body_target is None else body_target
    if scope.enforce_workflow and is_docstatus_changing(wf_kind, wf_target, req.method, form):
        doctype = docstatus_target_doctype(wf_kind, wf_target, form)
        workflow_name = _active_workflow_name(doctype) if doctype else _WORKFLOW_LOOKUP_FAILED
        if workflow_name is _WORKFLOW_LOOKUP_FAILED or workflow_name:
            if isinstance(workflow_name, str):
                governed_by = f"active Workflow {workflow_name!r} governs it"
            else:
                governed_by = (
                    "the workflow-existence lookup could not confirm this doctype is NOT "
                    "governed, so this refuses rather than guess"
                )
            _deny(
                "workflow bypass",
                f"This credential's enforce_workflow gate refused a docstatus-changing request "
                f"({wf_kind}: {wf_target!r}) on doctype {doctype!r} — {governed_by}. Call "
                f"{APPLY_WORKFLOW_METHOD} instead of a direct submit/cancel or docstatus write.",
            )
            return
    # ---- consent: MOVED to the document layer (2026-07-26) ------------------------------------
    # The consent gate used to live right here, and this is the wrong altitude for it. Consent is a
    # property of an ACT ON A DOCUMENT; this hook only ever sees an api-key HTTP request. Enforced
    # here it (a) could not cover OAuth Bearer, desk/cookie sessions, background jobs, the scheduler,
    # server scripts or the bench console — every one of which reaches the ledger without passing
    # this function at all — and (b) had to infer "is this a submit, and of what document" from a
    # request shape, which is the whole reason the classifier carries `?cmd=` dominance, nine
    # body-carrying RPC rewrites, a `savedocs` action map and a disclosed residual on raw `docstatus`
    # writes. It now lives in `pacioli_guard.act`, on `doc_events` `before_submit`/`before_cancel`,
    # where the same question is `doc.doctype`, `doc.name` and the event name.
    #
    # There is deliberately no consent check left in this function. Two gates cannot both hold the
    # single-use marker: whichever ran first would spend it and the second would correctly refuse a
    # marker that was already burned. One act, one claim, one place.
    #
    # What stays here is what this altitude CAN see and the document layer cannot: which credential
    # is acting, and therefore what it may call at all. A door admits; it does not decide.


def _expiry_instant(row):
    """The marker's expiry as epoch seconds. Prefers ``expires_at_epoch``; falls back to the Datetime.

    **Why a second field rather than better arithmetic on the first one.** A naive ``Datetime`` means
    whatever ``System Settings.time_zone`` says AT SPEND TIME, and that produced three defects with
    one cause: a TTL spanning a DST transition is off by the shift; an unreadable site zone falls back
    to UTC and lengthens the marker's life for any site east of UTC; and an administrator changing the
    site zone silently re-times every live marker. Epoch seconds have no timezone to disagree about,
    so fixing the representation closes all three at once instead of patching each.

    The Datetime is kept as the human-readable rendering, and is the ONLY source for markers minted
    before this field existed — an upgrade must not invalidate a marker someone is mid-ceremony with.
    A present-but-unusable epoch falls through to that same path rather than being trusted; both
    branches yield ``None`` when unreadable, which ``consent_verdict`` treats as expired.

    ⚠️ **ZERO MEANS UNSET, and that is not a guess** — verified by running the migration on a live
    bench. A frappe ``Float`` column defaults to **0, not NULL**, so after ``bench migrate`` every
    marker minted before this field existed reads ``0.0``. Falsiness made the fallback work by
    accident; it is explicit here instead. A genuine expiry of 0 would be 1970 — already expired by
    any clock — so reading it as unset costs nothing and never extends a lifetime.
    """
    epoch = _finite(row.get("expires_at_epoch"))
    if epoch:
        return epoch
    return _epoch(row.get("expires_at"))


def _consent_record(doctype, docname):
    """Load the live consent marker for one document, normalised for the pure core.

    Fails CLOSED: any lookup error reads as "no marker", never as "no gate". The stored fields
    are `ref_doctype`/`ref_docname` because `doctype` is reserved on every frappe doc.
    """
    try:
        row = frappe.db.get_value(
            CONSENT_DOCTYPE, {"ref_doctype": doctype, "ref_docname": docname},
            ["name", "token_hash", "ref_doctype", "ref_docname", "ref_action",
             "expires_at", "expires_at_epoch", "burned", "minted_by"],
            as_dict=True)
    except Exception:  # noqa: BLE001 — a broken lookup must deny, not open the gate
        return None
    if not row:
        return None
    return {
        "name": row.get("name"),
        "token_hash": row.get("token_hash"),
        "doctype": row.get("ref_doctype"),
        "docname": row.get("ref_docname"),
        "action": row.get("ref_action"),
        "expires_at": _expiry_instant(row),
        "burned": row.get("burned"),
        "minted_by": row.get("minted_by"),
    }


def _site_timezone():
    """The SITE's timezone, or ``None`` if it cannot be read. Never the process's.

    A frappe Datetime is stored NAIVE and in the site's own zone
    (``frappe.utils.now_datetime()``), so the site is the only authority on what wall time a stored
    expiry means. Reading it through the container clock is what broke 0.10.0 — see :func:`_epoch`.
    """
    try:
        return zoneinfo.ZoneInfo(frappe.utils.get_system_timezone())
    except Exception:  # noqa: BLE001 — an unreadable site zone must not raise inside the gate
        return None


def _finite(value):
    """A number, or ``None`` if it is not a usable instant. Deny-biased by construction.

    ``None`` is what ``_epoch``'s callers treat as "expired", so refusing here refuses the marker.
    `nan` and `inf` both make every ``>=`` comparison False, which would make a marker immortal
    rather than expired — so they must never leave this module as numbers.
    """
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _as_instant(moment):
    """A datetime to epoch seconds, resolving a NAIVE value through the SITE's zone.

    An already-aware value carries its own offset and is left alone. When the site's zone cannot be
    read we assume UTC rather than the process's zone, because it is deterministic.

    🔴 **THE DIRECTION OF THAT FALLBACK IS NOT UNIFORMLY SAFE, and this docstring used to claim it
    was** ("for a site running behind UTC it makes a marker expire EARLIER than intended, never
    later" — the qualifier was there, but "never later" read as a general guarantee). Corrected
    2026-07-29 after an independent review measured it:

    * A site BEHIND UTC (the Americas) — the fallback shortens the marker's life. Fail-CLOSED.
    * A site AHEAD of UTC — it LENGTHENS it, by the site's offset. **Fail-OPEN.** For
      ``Asia/Kolkata`` that is +5.5 hours, and Kolkata is not hypothetical: it is frappe's own
      hardcoded default when ``System Settings.time_zone`` is unset
      (``frappe/utils/data.py``: ``... or "Asia/Kolkata"``). East of UTC generally, up to +14h.

    So this fallback is a last resort, not a safety net, and the honest summary is that it trades a
    deterministic wrong answer for a nondeterministic one. The real fix is for the stored value to
    carry its own offset rather than depend on a mutable global read at spend time; that is a
    storage change and is recorded as owed.
    """
    if moment.tzinfo is not None:
        return moment.timestamp()
    return moment.replace(tzinfo=_site_timezone() or datetime.timezone.utc).timestamp()


def _epoch(value):
    """Coerce a stored expiry to epoch seconds, or ``None`` if it cannot be read.

    ``None`` is the deny-biased answer: `consent_verdict` treats an unreadable expiry as expired,
    so a marker whose lifetime cannot be established is never spendable.

    **The clock domain is the whole difficulty, and 0.10.0 got it wrong.** ``expires_at`` arrives as
    a naive datetime in the SITE's zone, and this used to call ``.timestamp()`` on it directly —
    which resolves a naive value through the **process's** zone. ``consent_verdict`` compares the
    result against ``time.time()``, which is true UTC. On the ordinary frappe deployment (container
    in UTC, site in local time) the two disagreed by the site's offset, so **every marker was born
    expired and no governed write could proceed**. Found on a live site at ``America/Chicago``
    against a UTC container: a marker minted for +10 minutes read as expired by 4h50m. Fail-CLOSED,
    so never an escape, but it made ``require_consent`` unusable and the refusal told the operator
    to mint a fresh marker that would be expired too.

    Nothing could fail on it because every test ran in one clock domain, and the lab site was UTC
    like its container. The dimension the doubles lacked was the difference between two clocks.

    **NON-FINITE VALUES ARE UNREADABLE, and until 2026-07-29 they were not.** ``inf``/``nan`` sailed
    through the numeric branch untouched, and ``consent_verdict`` compares ``now >= expires_at`` —
    under IEEE-754 that is False for both, so such a marker NEVER expired. Permanently valid: the
    exact inverse of this function's own deny-biased contract. Unreachable through the current writer
    (a ``DATETIME(6)`` column will not store "inf") and so latent rather than live, but the WRITE side
    (``plan_consent_marker``) was hardened against precisely this hazard in the same change that
    added it, and this reader — the one that hardening was modelled on — was not. Found by an
    independent review. One more call site, an alternate store, or a JSON-backed cache turns it live.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _finite(value)
    if isinstance(value, datetime.datetime):
        return _as_instant(value)
    for parse in (
        lambda v: _as_instant(datetime.datetime.fromisoformat(str(v))),
        lambda v: _finite(float(v)),
    ):
        try:
            return parse(value)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def _claim_consent(record):
    """Spend the marker atomically. Returns ``True`` only if THIS request won the single-use claim.

    The spend IS the single-use check. It used to be a read-then-write — ``consent_verdict`` read
    ``burned`` and a separate ``set_value`` wrote it — so two requests presenting one marker in the
    same instant could both pass the verdict before either write landed, and single-use is the
    entire anti-replay property. A conditional ``UPDATE ... WHERE burned = 0`` makes the database
    the arbiter instead: the row is addressed by primary key, so exactly one statement can match it
    while unburned. Closes floor-audit F4 (2026-07-26).

    Fails CLOSED, and that is the whole reason this returns a bool instead of writing and shrugging.
    If the statement raises, or the driver gives us no affected-row count we can read, this returns
    ``False`` and the caller denies the request. An unverifiable claim is never read as a won claim
    — the same deny-biased posture as :func:`_active_workflow_name`'s sentinel. The cost of that
    choice is honest: a driver that never reports ``rowcount`` would refuse every governed write
    rather than silently permit replays, which is the correct direction to fail in.

    ``frappe.db._cursor.rowcount`` is the only affected-row signal frappe exposes (there is no
    public accessor; read from ``database/database.py:149`` in frappe 16). It is private, so it is
    read through ``getattr`` chains and any surprise reads as a lost claim rather than an exception.
    """
    name = (record or {}).get("name")
    if not name:
        return False
    try:
        frappe.db.sql(
            f"update `tab{CONSENT_DOCTYPE}` set burned = 1 where name = %s and burned = 0",
            (name,),
        )
        affected = getattr(getattr(frappe.db, "_cursor", None), "rowcount", None)
    except Exception:  # noqa: BLE001 — a failed claim must deny, never open the gate
        return False
    # Exactly one row may be claimed. 0 = another request already spent it (or it vanished);
    # anything unreadable or unexpected is not evidence that we won.
    return affected == 1
