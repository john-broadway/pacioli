# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Guard — frappe glue: enforce a credential's ApiScope at the ``auth_hooks`` chokepoint.

Registered via ``hooks.py`` ``auth_hooks``. Frappe runs auth hooks inside ``validate_auth()``,
AFTER the api-key authenticates (sets the user) and BEFORE the request dispatches — and without a
try/except around the hook loop, so a ``frappe.PermissionError`` raised here becomes a real 403.
**No frappe core files are modified** — this ships as an installable app.
"""
from __future__ import annotations

import json
import time

import frappe

from pacioli_guard.scope import (
    APPLY_WORKFLOW_METHOD,
    ApiScope,
    api_key_from_auth_header,
    body_scoped_target,
    classify,
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
      ``frappe.model.workflow.apply_workflow``, and ``run_doc_method`` (EVERY inner method, not just
      submit/cancel/discard) — to the same per-doctype ``("method", "<DocType>.<verb>")`` shape the
      URL-path ``run_method`` vector already produces, so a credential granted only
      ``"Sales Invoice.submit"`` can no longer submit a Journal Entry through
      ``frappe.client.submit``, and a credential granted only ``"Sales Invoice.get_pdf"`` can no
      longer reach ANY other doctype's ``get_pdf`` (or any other method) through bare
      ``run_doc_method``. HARD-DENIED regardless of grant or resolution: any method target whose
      doctype-part is ``"Bulk Update"`` (the 2-hop laundering vector — its own instance method reads
      the target doctype from the SAVED RECORD, never the request). STILL OPEN: other
      container-DocType vectors of that same 2-hop shape are un-audited — a post-Gate-10 follow-up.
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
        return
    form = frappe.form_dict or {}
    run_method = form.get("run_method")
    cmd = form.get("cmd")  # legacy RPC: frappe routes on cmd BEFORE the path — it is the real target
    kind, target = classify(req.path, req.method, run_method, cmd)
    # Body-doctype rewrite (submit/cancel only): frappe.client.submit/.cancel, run_doc_method, and
    # savedocs Submit/Update/Cancel carry their real target doctype in the request BODY, invisible
    # to `classify`'s pure (path, http_method, run_method, cmd) signature — the generic-RPC footgun.
    # This resolves them to the SAME ("method", "<DocType>.submit"/"cancel") shape the URL-path
    # run_method vector already produces, into NEW perm_kind/perm_target variables — `kind`/`target`
    # below (the enforce_workflow gate) are DELIBERATELY left untouched, so that gate's own
    # (separately documented, still-open) generic-RPC residual and its existing tests are unaffected.
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
