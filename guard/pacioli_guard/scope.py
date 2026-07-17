# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Guard — pure decision core for per-credential API capability scoping.

No frappe import: ``classify`` + ``is_permitted`` are pure functions, so the security-critical
logic is unit-testable without a running bench. The frappe glue lives in ``enforce.py``.
"""
from __future__ import annotations

import base64
import json
import unicodedata
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from urllib.parse import unquote


def api_key_from_auth_header(auth_header):
    """Extract the api_key from an ``Authorization`` header. Pure.

    Frappe authenticates the *same* ``api_key:api_secret`` credential under BOTH schemes:
    ``token <key>:<secret>`` and ``Basic <base64(key:secret)>``. Recognising only ``token`` was a
    scope bypass — a scoped credential re-sent as Basic auth (e.g. ``curl -u key:secret``) read as
    unscoped and got full access. Returns the key, or ``None`` for an absent header, an
    unrecognised scheme, or a malformed value.
    """
    parts = (auth_header or "").split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts[0].lower(), parts[1].strip()
    if scheme == "basic":
        try:
            value = base64.b64decode(value).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
    elif scheme != "token":
        return None
    if ":" not in value:
        return None
    return value.split(":", 1)[0].strip() or None


_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})


def _coerce_flag(value):
    """Deny-biased truthiness for a security Check flag. Strings coerce by content — so ``"0"`` and
    ``"false"`` are False, unlike Python's ``bool("0")`` which is True; everything else (Frappe's
    ``0``/``1`` ints, real bools) falls back to ``bool``."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return bool(value)


def _coerce_enabled(value):
    """Coerce the ``enabled`` kill-switch flag. The bias here is the OPPOSITE edge of the same
    deny-biased coin as ``_coerce_flag``: ``enabled`` defaults ON, so its **absence** must read as
    enabled — a legacy grant written before the field existed (``None`` / key missing) is NOT a
    kill. Any *present* value coerces through ``_coerce_flag``, so an explicit ``0``/``"false"``
    kills and an ambiguous present value (e.g. ``""``) reads as a kill too — once someone has
    touched the switch, doubt lands on the safe (denied) side."""
    if value is None:
        return True
    return _coerce_flag(value)


def _coerce_limit(value):
    """Coerce ``rate_limit_per_minute`` to an int. ``None`` (field absent on a pre-CONTAIN grant),
    garbage, or anything non-numeric coerces to ``0`` = **no limit** — backward compatible: a grant
    written before the field existed keeps working unlimited, exactly as it did. Negative values
    are nonsense config and also mean no limit (``is_rate_allowed`` treats every limit <= 0 as
    unenforced)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_rate_allowed(limit, current_count):
    """Pure per-window rate decision. ``limit <= 0`` (incl. the coerced default) means no limit.
    ``current_count`` is the number of requests observed in the current window **including this
    one** (i.e. the post-increment counter value), so a limit of N allows exactly N requests per
    window: the N-th passes, the (N+1)-th is denied."""
    limit = _coerce_limit(limit)
    if limit <= 0:
        return True
    return current_count <= limit


def _clean_allowlist(items):
    """Keep only non-blank string entries — drops ``None``, non-strings, and empty/whitespace rows
    a NULL/blank DocType child row (``reqd`` is app-level only) could otherwise inject into an
    allowlist, where they crash matching or flip 'allow all' into 'deny all'."""
    return [s for s in (items or []) if isinstance(s, str) and s.strip()]


# Frappe REST /api/resource endpoints → permission verb, keyed by HTTP method.
_RESOURCE_VERB = {"GET": "read", "POST": "create", "PUT": "write", "PATCH": "write", "DELETE": "delete"}

# The four CRUD verbs a resource grant can be narrowed to.
_CRUD_VERBS = frozenset({"read", "create", "write", "delete"})


def _clean_verbs(items):
    """Normalise a resource-verb allowlist. Returns ``None`` for an **unspecified** grant (``None``
    in) — that means "all verbs", backward compatible with a grant that predates per-verb scoping.
    A **present** list (even empty) returns a frozenset of the known CRUD verbs in it: ``["read"]``
    → ``{"read"}`` (read-only), and an explicitly empty ``[]`` → ``frozenset()`` (deny every resource
    verb — an operator who unticked all four boxes means it, and must not silently get full access).
    Unknown/garbage entries are dropped."""
    if items is None:
        return None
    return frozenset(
        s.strip().lower() for s in items
        if isinstance(s, str) and s.strip().lower() in _CRUD_VERBS
    )


@dataclass(frozen=True)
class ApiScope:
    """A capability grant carried by a scoped credential.

    :param methods: allowed ``/api/method`` targets — exact dotted names or ``fnmatch`` globs
        (e.g. ``"erpnext.selling.*"``). A document-method call matches as ``"<DocType>.<method>"``.
    :param allow_resource: may the credential use ``/api/resource`` CRUD at all? Defaults to
        ``False`` — method-only unless resource access is explicitly granted (this denies the
        raw-CRUD bypass by default).
    :param resource_doctypes: when ``allow_resource``, an optional DocType allowlist. An empty set
        means "all DocTypes" (still subject to the user's own role permissions downstream — scope
        narrows, never widens).
    :param enabled: the **kill switch**. ``False`` denies every request from this credential,
        regardless of allowlists. Defaults ``True``, and a grant that never carried the field
        (pre-CONTAIN) reads as enabled — absence is not a kill.
    :param rate_limit_per_minute: the **speed limit** — max requests per fixed one-minute window.
        ``0`` (the default, and what a pre-CONTAIN grant coerces to) means no limit.
    :param resource_verbs: the CRUD verbs ``/api/resource`` access is narrowed to (a subset of
        ``read``/``create``/``write``/``delete``). ``None`` means **unspecified = all verbs** —
        backward compatible: a grant that names a DocType without narrowing verbs admits every verb
        on it, exactly as before. A frozenset (even empty) means **exactly those verbs**: ``{"read"}``
        makes a genuinely read-only resource credential (the same key can no longer POST/PUT/DELETE),
        and ``frozenset()`` denies every resource verb (an operator who unticked all four verb boxes
        gets what they meant, not full access). Narrowing is **per-credential** in this version (it
        applies to all the credential's granted DocTypes); per-DocType verb granularity is a future
        increment.
    :param enforce_workflow: **opt-in, off by default.** When ``True``, the guard additionally
        refuses a docstatus-changing call (``submit``/``cancel`` by method name, or a raw
        ``PUT``/``PATCH`` carrying a ``docstatus`` key) against a doctype that has an active Frappe
        Workflow, unless the call IS ``frappe.model.workflow.apply_workflow`` (see
        :func:`is_docstatus_changing` / :data:`APPLY_WORKFLOW_METHOD`). Off by default and absence
        reads as off — like every gate added after this app's first release, turning a NEW gate on
        by default could newly deny previously-passing calls from a live credential on upgrade, so
        it is opt-in per credential, never site-wide-default-on.
    """

    methods: frozenset = field(default_factory=frozenset)
    allow_resource: bool = False
    resource_doctypes: frozenset = field(default_factory=frozenset)
    enabled: bool = True
    rate_limit_per_minute: int = 0
    resource_verbs: frozenset = None
    enforce_workflow: bool = False

    @classmethod
    def from_dict(cls, d):
        """Build from the JSON ``api_scope`` grant; return ``None`` if it is not a usable mapping."""
        if not isinstance(d, dict):
            return None
        return cls(
            methods=frozenset(_clean_allowlist(d.get("methods"))),
            allow_resource=_coerce_flag(d.get("allow_resource")),
            resource_doctypes=frozenset(_clean_allowlist(d.get("resource_doctypes"))),
            enabled=_coerce_enabled(d.get("enabled")),
            rate_limit_per_minute=_coerce_limit(d.get("rate_limit_per_minute")),
            resource_verbs=_clean_verbs(d.get("resource_verbs")),
            # Deny-biased like allow_resource (bool(None) is False already), but routed through
            # the same strict string coercion so "0"/"false" from a legacy JSON blob can't be
            # mistaken for truthy the way bare Python bool() would read them.
            enforce_workflow=_coerce_flag(d.get("enforce_workflow")),
        )

    @classmethod
    def from_grant(cls, allow_resource, method_patterns, resource_doctypes,
                   enabled=None, rate_limit_per_minute=None, resource_verbs=None,
                   enforce_workflow=None):
        """Build from the structured *API Key Scope* DocType fields — the pure seam the frappe
        glue feeds (an ``allow_resource`` Check plus two child-table allowlists, the CONTAIN pair
        — the ``enabled`` kill switch + ``rate_limit_per_minute`` — the per-credential
        ``resource_verbs`` narrowing, and the opt-in ``enforce_workflow`` gate). Normalises through
        ``from_dict`` so deny-by-default and Check-int→bool coercion have one home; the frappe wall
        only plucks primitives, it never shapes the security decision. ``enabled=None`` (field
        absent on a not-yet-migrated doc) reads as enabled; ``rate_limit_per_minute=None`` reads as
        no limit; ``resource_verbs=None`` or empty reads as all verbs; ``enforce_workflow=None``
        reads as off — each backward compatible with an older grant.
        """
        return cls.from_dict(
            {
                "allow_resource": allow_resource,
                "methods": list(method_patterns or []),
                "resource_doctypes": list(resource_doctypes or []),
                "enabled": enabled,
                "rate_limit_per_minute": rate_limit_per_minute,
                # None stays None (unspecified = all verbs); a list — even empty — is passed through
                # verbatim so an explicit "deny all resource verbs" is not coerced back to "all".
                "resource_verbs": resource_verbs if resource_verbs is None else list(resource_verbs),
                "enforce_workflow": enforce_workflow,
            }
        )


def classify(path, http_method, run_method=None, cmd=None):
    """Map a request ``(path, http_method[, run_method][, cmd])`` to a scope target. Pure.

    Returns one of:
      * ``("method", "<dotted.name>")``            — ``/api/method/*``, a legacy ``?cmd=``, or a document ``run_method``
      * ``("resource", ("<DocType>", "<verb>"))``  — ``/api/resource`` CRUD (verb ∈ read/create/write/delete)
      * ``("other", None)``                        — anything unrecognised (a scoped credential fails closed)

    **``cmd`` dominates.** Frappe's dispatcher (``app.py``) routes on ``frappe.form_dict.cmd``
    *before* it looks at the URL path (``handler.handle`` → ``execute_cmd(cmd)``), so a request
    carrying ``?cmd=X`` runs ``X`` regardless of the path. Classifying by path alone let a credential
    with one allowlisted method smuggle *any* whitelisted call via ``?cmd=`` — a total bypass. When
    ``cmd`` is present it IS the target.

    Handles all three Frappe REST mounts: bare ``/api``, the identical ``/api/v1`` alias, and the
    ``/api/v2`` surface (``/document/`` instead of ``/resource/``; doc-method name carried in the
    path). Unknown shapes fall through to ``("other", None)`` so a scoped credential fails closed.

    Thin 2-tuple wrapper over :func:`_classify_full` — the provenance (``resolved``) signal that
    drives deny-unknown method scoping is dropped here; use :func:`method_target_resolved` for that,
    fed by the SAME underlying computation (zero drift between the two).
    """
    kind, target, _resolved = _classify_full(path, http_method, run_method, cmd)
    return (kind, target)


def method_target_resolved(path, http_method, run_method=None, cmd=None):
    """Pure: does this request's classified ``("method", …)`` target already carry a per-doctype
    resolution — i.e. was the doctype supplied by the ROUTE itself (URL path / item context), not
    just asserted by the caller as part of an otherwise-bare dotted name?

    ``True`` for: a v1 resource-item ``run_method`` call (the doctype is the URL's own path
    segment), a v2 path-carried doc-method (``/document/<dt>/<name>/method/<m>/``), and a v2
    two-segment controller method (``/method/<dt>/<method>``). ``False`` for a bare
    ``/api/method/<name>``, a legacy ``?cmd=<name>``, and a v2 single-segment ``/method/<name>`` —
    each of these is just a dotted STRING the caller wrote; ``"Sales Invoice.submit"`` reaching this
    function via a bare ``/api/method/`` path is syntactically identical to a genuine per-doctype
    call and this function correctly says ``False`` for it — the route carried no doctype, the
    string merely looks like one. Meaningless (returns ``False``) for a ``resource``/``other`` kind.

    Fed by the exact same traversal as :func:`classify` (:func:`_classify_full`) — never re-derived
    from the resulting target's string shape, which cannot distinguish a route-supplied doctype from
    a caller-asserted one.
    """
    _kind, _target, resolved = _classify_full(path, http_method, run_method, cmd)
    return resolved


def _classify_full(path, http_method, run_method=None, cmd=None):
    """The real classifier — computes the 3-tuple ``(kind, target, resolved)`` that both
    :func:`classify` and :func:`method_target_resolved` are thin wrappers over. See both for the
    public contract; ``resolved`` is only meaningful when ``kind == "method"``."""
    if isinstance(cmd, str):
        if cmd.strip():
            return ("method", cmd.strip(), False)
    elif cmd:
        # form_dict extraction seam: frappe pre-parses a JSON body into form_dict verbatim, so a
        # hostile/malformed body can carry `"cmd": [...]` / `{...}` / a bare int/bool -- a truthy
        # value that is NOT a plain string. What frappe's own execute_cmd() would do with such a
        # cmd is unverified here (needs bench pin); either way this pure function must never crash
        # on it (the old `cmd.strip()` did, unconditionally, for every one of these shapes) --
        # deny-biased on ambiguity, matching `_run_doc_method_doctype`'s posture elsewhere in this
        # module, rather than silently falling through to path-based classification, which the real
        # "cmd dominates" dispatcher may not actually be using for this request.
        return ("other", None, False)
    if path.startswith("/api/v2/"):
        return _classify_v2_full(path, http_method)
    if path.startswith("/api/v1/"):
        path = "/api" + path[len("/api/v1"):]  # v1 rules are mounted identically at bare /api
    if path.startswith("/api/method/"):
        name = path[len("/api/method/"):].split("/", 1)[0].strip()
        return ("method", name, False)
    if path.startswith("/api/resource/"):
        segs = [unquote(p) for p in path[len("/api/resource/"):].split("/") if p]
        doctype = segs[0] if segs else ""
        is_item = len(segs) >= 2  # a doc name is present -> item url, else collection url
        # Frappe honours ?run_method ONLY on an item url: read_doc (GET) and execute_doc_method
        # (POST). On a COLLECTION (create_doc POST / document_list GET) and on item PUT/PATCH/DELETE
        # (update_doc / delete_doc) it IGNORES run_method and performs real CRUD — so treating those
        # as a method call reopens the raw-CRUD bypass. Classify by the real verb there.
        if run_method and is_item and http_method in ("GET", "POST"):
            # The doctype came from the URL's OWN path segment -- a genuine per-doctype resolution.
            return ("method", f"{doctype}.{run_method}", True)
        # A POST to an ITEM url with no visible method name is execute_doc_method with an unknown
        # method — fail closed rather than mislabel it as a resource create.
        if is_item and http_method == "POST":
            return ("other", None, False)
        return ("resource", (doctype, _RESOURCE_VERB.get(http_method, "read")), False)
    return ("other", None, False)


def _classify_v2_full(path, http_method):
    """Classify a Frappe **v2** REST path (mounted at ``/api/v2``), returning the 3-tuple
    :func:`_classify_full` produces. Pure. v2 uses ``/document/`` where v1 uses ``/resource/``, and
    carries a doc-method name in the PATH (``/document/<dt>/<name>/method/<m>/``) rather than a form
    param — so that case must resolve to a ``("method", …)`` target, never resource access.
    Unrecognised shapes return ``("other", None, False)``.
    """
    rest = path[len("/api/v2"):]
    if rest.startswith("/method/"):
        parts = [p for p in rest[len("/method/"):].split("/") if p]
        if len(parts) == 1:
            return ("method", unquote(parts[0]), False)
        if len(parts) == 2:  # /method/<doctype>/<method> — a controller method, doctype from PATH
            return ("method", f"{unquote(parts[0])}.{unquote(parts[1])}", True)
        return ("other", None, False)
    if rest.startswith("/document/"):
        parts = [unquote(p) for p in rest[len("/document/"):].split("/") if p]
        if not parts:
            return ("other", None, False)
        doctype = parts[0]
        # doc-method: /document/<dt>/<path:name>/method/<m> — <path:name> may contain "/" (frappe
        # allows slashes in doc names, e.g. naming series ACC/2024/00001), so the "method" literal
        # and method name are the LAST two segments, NOT a fixed index. A fixed index let a
        # slash-named doc-method fall through to resource CRUD (a resource grant could run submit).
        if len(parts) >= 4 and parts[-2] == "method":
            return ("method", f"{doctype}.{parts[-1]}", True)
        return ("resource", (doctype, _RESOURCE_VERB.get(http_method, "read")), False)
    return ("other", None, False)


# The read-only, exact-name RPCs a bare (doctype-unresolved) ``methods`` grant may still honor.
# Deny-unknown means a method grant only fires ON A RESOLVED TARGET (see `method_resolved` on
# `is_permitted`) EXCEPT for this tiny curated set — each is read-only (no docstatus/data mutation),
# reviewed on admission. Membership here is NECESSARY-NOT-SUFFICIENT: `scope.methods` must still
# grant the exact name via its own fnmatch allowlist; this frozenset only lifts the resolution
# requirement, it grants nothing by itself. NO escape hatch: a new entry is a reviewed, changelogged,
# version-bumped act — never a runtime knob (see the guard README/CHANGELOG "deny-unknown" section).
SAFE_METHODS = frozenset({
    "frappe.auth.get_logged_user",                                         # read-only identity probe (doctor)
    "frappe.desk.form.linked_with.get_submitted_linked_docs",               # read-only linked-doc lookup (UNDO graph)
    "erpnext.controllers.stock_controller.show_accounting_ledger_preview",  # read-only PLAN preview (savepoint→rollback)
})

# The 2-hop laundering vector no classifier can resolve: a tool/container DocType whose whitelisted
# controller method drives writes to OTHER doctypes named in the request BODY/child-rows or in the
# SAVED RECORD (not in the classifiable (path, method) signature), so a granted `<DocType>.<method>`
# reaches doctypes that appear NOWHERE the classifier can see. A method target whose doctype-part is
# in this set is denied regardless of grant or resolution — ungrantable, not just unresolved.
#
# Curated deny-list (container-doctype 2-hop hardening, John's ruling 2026-07-10 — docs/plans/
# 2026-07-10-container-doctype-2hop-hardening.md): the broker governs a small fixed set of doctypes;
# these are multi-write tool-DocTypes it NEVER uses, so hard-denying them is deny-more-only and
# zero-regression. `Bulk Update` reads document_type/field/update_value from the SAVED RECORD.
# `Data Import`/`Bank Statement Import` `form_start_import` insert+submit whatever `reference_doctype`
# the record names (source-traced). `Unreconcile Payment`/`Repost Accounting Ledger` are the plausible
# siblings of the same shape — a hard-deny is safe regardless of full-depth tracing since the broker
# uses none of them. NOT here: `Payment Reconciliation` — the broker's own F-R2 reconcile needs it and
# its malicious use is byte-identical to the legit one, so it cannot be classifier-closed without
# breaking the broker; it stays a disclosed residual + operator rule (grant only to the broker's cred).
_UNGRANTABLE_METHOD_DOCTYPES = frozenset({
    "Bulk Update",
    "Data Import",
    "Bank Statement Import",
    "Unreconcile Payment",
    "Repost Accounting Ledger",
})


def _fold_doctype(s):
    """Normalize a doctype-part to the equivalence class MariaDB/MySQL's default collation compares
    under, for the hard-deny lookup ONLY. Every source that can populate a method target's
    doctype-part (a URL path segment, a `doctype`/`dt`/`document.doctype` body field) is a plain
    attacker-controlled string, and frappe's own `tabDocType.name` column compares
    case-INSENSITIVELY *and* (under `utf8mb4_general_ci`/`unicode_ci`) accent-INSENSITIVELY -- so
    "bulk update"/"BULK UPDATE"/"Bülk Update" all plausibly resolve to the SAME real DocType at the
    DB layer even though Python's `==`/`in` do not. NFKD-decompose, strip the combining marks (the
    accents), then casefold: `Bülk Update` -> `bulk update`. (NEEDS BENCH PIN for the exact
    collation frappe ships, but folding here can only ever deny MORE than the raw-string version,
    never less, so it does not need bench confirmation to be the correct default posture.) This
    folds ONLY the hard-deny check; `scope.methods` fnmatch (a credential's own GRANT) stays exact,
    so this only ever subtracts from what a grant would otherwise allow, on this one doctype-part."""
    decomposed = unicodedata.normalize("NFKD", s)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return without_marks.casefold()


_UNGRANTABLE_METHOD_DOCTYPES_FOLDED = frozenset(
    _fold_doctype(d) for d in _UNGRANTABLE_METHOD_DOCTYPES)


def is_permitted(scope, kind, target, *, method_resolved=False):
    """Pure allow/deny decision.

    ``scope is None`` (an unscoped credential — a browser session or a normal full-access key)
    always returns ``True``, so this only ever *narrows* an explicitly-scoped credential and is
    fully backward compatible.

    ``method_resolved`` (deny-biased default ``False``) is the deny-unknown gate for ``kind ==
    "method"``: a ``methods`` grant is honored only when the call is doctype-RESOLVED (the caller
    passes ``True`` — fed by :func:`method_target_resolved` / a body-doctype rewrite) OR the bare
    target name is one of the tiny curated :data:`SAFE_METHODS`. A grant on any OTHER bare/unresolved
    method name — however broad the pattern — no longer suffices; a pure classifier cannot enumerate
    every dangerous generic RPC, so the default is denied-until-reviewed, not open-until-enumerated.
    """
    if scope is None:
        return True
    if not scope.enabled:
        return False  # the kill switch dominates every allowlist — defense in depth: even if the
        # glue forgets to check it separately, a disabled credential can never pass this gate
    if kind == "method":
        if not isinstance(target, str) or not target:
            return False
        # Doctype names never contain "." (a frappe constraint), so the doctype-part of a rewritten
        # "<DocType>.<method>" target is everything before the FIRST dot -- split(".", 1), NOT
        # rsplit: a method name that itself carries dots (a malformed/hostile run_doc_method
        # `method`, e.g. "x.bulk_update") must not slide the doctype boundary and dodge the hard-deny.
        # .strip() so a whitespace-padded segment ("Bulk Update " from an un-stripped URL path) can't
        # evade the exact-name set either.
        doctype_part = target.split(".", 1)[0].strip() if "." in target else None
        # hard deny — checked BEFORE the grant, never expressible as a grant at all; folded
        # case-AND-accent, see _fold_doctype above for why that (not exact string) is the floor.
        if doctype_part is not None and _fold_doctype(doctype_part) in _UNGRANTABLE_METHOD_DOCTYPES_FOLDED:
            return False
        granted = any(fnmatchcase(target, pat) for pat in scope.methods if isinstance(pat, str))
        return granted and (method_resolved or target in SAFE_METHODS)
    if kind == "resource":
        if not scope.allow_resource:
            return False
        doctype = target[0] if target else None
        verb = target[1] if target and len(target) > 1 else None
        # Per-credential verb narrowing, checked first: resource_verbs None is "all verbs" (backward
        # compatible / unspecified); a present set (even empty) denies any verb it does not list —
        # this is what makes a read-only resource credential real (a DocType allowlist alone admitted
        # every verb), and it honors an all-unticked grant as deny-all rather than full access.
        if scope.resource_verbs is not None and verb not in scope.resource_verbs:
            return False
        if not scope.resource_doctypes:
            return True
        return doctype in scope.resource_doctypes
    return False  # unknown call class + a scoped credential -> fail closed


# The sanctioned Workflow-transition path. A call to this method IS the workflow (it's how a
# non-approving transition or an approving human moves docstatus under a configured Workflow) —
# never treated as a bypass attempt by `is_docstatus_changing`, regardless of what shape it would
# otherwise match. Recognised HERE (the pure core), not in the frappe glue, so the security-
# critical judgement stays in one tested place, matching the rest of this module.
APPLY_WORKFLOW_METHOD = "frappe.model.workflow.apply_workflow"

_DOCSTATUS_METHOD_NAMES = frozenset({"submit", "cancel"})
# `run_doc_method` is reachable as the bare name (v1/v2 `/method/run_doc_method`) AND as its fully
# dotted form via `?cmd=` — both resolve to the same function; scope both spellings identically.
_RUN_DOC_METHOD_NAMES = frozenset({"run_doc_method", "frappe.handler.run_doc_method"})

# The Desk UI's Save/Submit/Cancel endpoint. Unlike a document ``run_method=submit``, its own
# method name ("savedocs") says NOTHING about docstatus — the transition is driven by the ``action``
# body param ("Save"=draft, everything else moves docstatus) and the target doctype lives in the
# ``doc`` body param, not the method name. So it needs its own recognition (added 2026-07-03 after a
# redteam found it slipped the suffix check entirely — the single most common real submit path in
# Frappe's own UI). KNOWLEDGE-PINNED: the ``{"Save":0,"Submit":1,"Update":1,"Cancel":2}`` action map
# and the ``doc``/``action`` param names are read from frappe source (frappe/desk/form/save.py), not
# live-verified here.
SAVEDOCS_METHOD = "frappe.desk.form.save.savedocs"
_DRAFT_ACTION = "Save"
# A create (POST) carrying one of these docstatus values is an insert-as-submitted — the only
# docstatus value legitimate on a create is draft (0/absent), so 1/2 (int or string) is the signal.
_SUBMITTING_DOCSTATUS = frozenset({1, 2, "1", "2"})


def _doctype_from_doc_param(form):
    """Pull the target DocType out of a ``doc`` body param (savedocs and friends put the whole doc
    there, as a JSON string or an already-parsed dict). Deny-biased: anything unparseable / shapeless
    / missing a doctype returns ``None``, which the glue reads as "couldn't confirm" → deny."""
    if not isinstance(form, dict):
        return None
    doc = form.get("doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except (ValueError, TypeError):
            return None
    if isinstance(doc, dict):
        dt = doc.get("doctype")
        return dt.strip() if isinstance(dt, str) and dt.strip() else None
    return None


def is_docstatus_changing(kind, target, http_method, form):
    """Pure: does this classified call look like an attempt to move a document's ``docstatus``
    outside Frappe's own Workflow machinery? Feeds the guard's opt-in ``enforce_workflow`` gate
    (``enforce.py``) — this function only judges the SHAPE of the call; whether the target doctype
    actually carries an active Workflow is a separate, frappe-internal lookup this pure function
    has no way to make (no I/O here).

    The shapes that match — the vectors a scoped-but-otherwise-permitted credential could use to
    walk around a configured approval chain without ever calling the broker:

    * ``kind == "method"`` and the target's method name (the text after the LAST ``.``) is
      ``"submit"`` or ``"cancel"`` — this single check covers THREE routing mechanisms for free,
      because ``classify`` maps all of them to the identical ``("method", "<DocType>.submit")``
      shape before this function ever sees it: a document ``run_method`` call (v1 item-url), the
      v2 path-carried doc-method, and a legacy ``?cmd=`` naming the same target string.
    * ``kind == "method"`` and the target IS :data:`SAVEDOCS_METHOD` (the Desk UI Save/Submit/Cancel
      endpoint) with an ``action`` that is NOT a plain draft ``"Save"`` — its method name never ends
      in ``submit``/``cancel`` so the suffix check above misses it entirely; the docstatus move is
      in the ``action`` param. A missing/unknown action is treated as docstatus-moving (deny-biased);
      only an explicit ``"Save"`` (draft) passes.
    * ``kind == "resource"`` and ``http_method`` is ``PUT``/``PATCH`` and ``form`` carries a
      ``docstatus`` key at all — the raw-REST update sneaky path. The legitimate way to move
      ``docstatus`` is ``apply_workflow`` (or the classified submit/cancel above), never a direct
      field write, so the mere PRESENCE of the key in an update body is the signal. This deliberately
      does NOT read or compare the current ``docstatus`` (a fragile extra DB read this pure
      function can't make anyway) — presence alone is treated as an attempt.
    * ``kind == "resource"`` and ``http_method`` is ``POST`` (a create) and ``form``'s ``docstatus``
      is a SUBMITTING value (1/2) — the insert-as-submitted path. Here presence is NOT enough: a
      create legitimately carries ``docstatus: 0`` (a draft), so only 1/2 is the signal.
      **KNOWLEDGE-PINNED, NOT LIVE-VERIFIED:** this reads ``form`` as ``enforce.py`` hands it over
      (frappe's ``frappe.form_dict``). Whether a ``PUT``/``PATCH`` sent with a raw JSON body (as
      opposed to form-encoded/query-string params) reliably surfaces its ``docstatus`` key through
      ``frappe.form_dict`` has not been exercised against a live bench —
      ``broker/pacioli/erpnext.py`` documents the SAME class of uncertainty for a different call
      (it puts ``run_method`` in the query string specifically "so the classifier's ``form_dict``
      read always sees it, whatever the body encoding" — a guarantee this gate's JSON-body read
      does not obviously inherit). Live falsification is a future bench gate.

    ``APPLY_WORKFLOW_METHOD`` itself is NEVER flagged — it is the sanctioned path this whole gate
    exists to funnel callers toward.

    The honest residual (stated in the README/CHANGELOG), NOT covered here: a generic RPC that
    flips docstatus on the doctype named IN ITS BODY — ``frappe.client.submit``/``.cancel`` (whose
    name-suffix DOES match, but whose derived "doctype" ``"frappe.client"`` never matches a real
    Workflow so the gate passes), and ``frappe.client.set_value``, v2 ``run_doc_method``, top-level
    ``bulk_update``/``bulk_delete`` (whose names never match the suffix check at all). Each takes
    its real doctype from the request body in a shape this pure check does not parse — invisible to
    the gate for the doctype it actually touches. Extending to each is open-ended per-RPC body
    parsing; ``savedocs`` above is covered specifically because it is the Desk UI's own path (high
    real-world traffic) and its ``doc`` param is a stable, documented place to read the doctype.
    """
    if kind == "method":
        if not isinstance(target, str) or not target:
            return False
        if target == APPLY_WORKFLOW_METHOD:
            return False
        if target == SAVEDOCS_METHOD:
            action = form.get("action") if isinstance(form, dict) else None
            return action != _DRAFT_ACTION
        _, _, method_name = target.rpartition(".")
        return method_name in _DOCSTATUS_METHOD_NAMES
    if kind == "resource":
        if not isinstance(form, dict):
            return False
        if http_method in ("PUT", "PATCH"):
            return "docstatus" in form
        if http_method == "POST":
            value = form.get("docstatus")
            if value is not None and not isinstance(value, (int, str)):
                return True  # unreadable docstatus in a create (list/dict) -- deny-biased, never a TypeError
            return value in _SUBMITTING_DOCSTATUS
        return False
    return False


# -- Body-doctype rewrite for per-doctype SCOPE enforcement (submit/cancel only) ------------------
#
# The generic-RPC footgun (documented above, and in the README/CHANGELOG as the guard's honest
# residual): a `methods` grant matches by NAME only, so `frappe.client.submit`/`.cancel`,
# `run_doc_method`, and the Desk `savedocs` endpoint can move ANY doctype's docstatus once a
# credential holds that one broad method name -- `resource_doctypes` never applies to a `("method",
# …)` target, and these RPCs' real target doctype lives in the request BODY, invisible to
# `classify`'s pure (path, http_method, run_method, cmd) signature. `body_scoped_target` closes it
# for submit/cancel specifically (the shapes ERPNext's Journal Entry submit/cancel override forces
# a caller onto, since JE drops `@frappe.whitelist()` from its overridden base methods and the
# item-URL `run_method` vector 403s): it rewrites the classified target to the SAME
# `("method", "<DocType>.submit"/"cancel")` shape the URL-path vector already produces, so
# `is_permitted`'s existing `kind == "method"` branch (a `scope.methods` fnmatch) enforces it
# identically -- no new enforcement branch, no `resource_doctypes` involvement.
#
# Deliberately narrow: `frappe.client.save`/`.insert` and any `run_doc_method` call naming a
# non-submit/cancel controller method stay doctype-blind, unchanged -- a documented, still-open
# residual, not silently folded in.

_BODY_SUBMIT_METHOD = "frappe.client.submit"
_BODY_CANCEL_METHOD = "frappe.client.cancel"
# The Desk UI's OWN cancel endpoint (distinct from frappe.client.cancel) — `cancel(doctype, name,
# …)` in frappe/desk/form/save.py: `doc = frappe.get_doc(doctype, name); doc.cancel()`. Plain
# sibling (doctype, name) params, same doc.cancel()-directly-bypasses-is_whitelisted-on-override
# property as frappe.client.cancel — so it is the SAME override-doctype cancel shape and MUST be
# scoped per-doctype, not left doctype-blind. (Found by the guard-bypass redteam, 2026-07-06.)
_DESK_CANCEL_METHOD = "frappe.desk.form.save.cancel"
# Frappe's bulk submit/cancel/update RPC: `submit_cancel_or_update_docs(doctype, docnames,
# action, data)` in frappe/desk/doctype/bulk_update/bulk_update.py — `_bulk_action` calls
# `doc.submit()` / `doc.cancel()` / `doc.update(data); doc.save()` directly on up to 500 docs.
# The submit/cancel actions are the same override-doctype-capable shape; `action="update"` is a
# doctype-blind arbitrary field write we do NOT confer through a blind grant → fail closed.
# (Found by the guard-bypass redteam, 2026-07-06 — a CRITICAL doctype-blind bypass of this residual.)
_BULK_UPDATE_METHOD = "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs"
_BULK_ACTION_VERB = {"submit": "submit", "cancel": "cancel"}  # "update"/unknown → deny-closed

# `frappe.desk.form.save.submit` is a bare module-level ALIAS of savedocs (`submit = savedocs` in
# frappe/desk/form/save.py) — @frappe.whitelist marks the function OBJECT, so the alias is
# independently reachable at /api/method/frappe.desk.form.save.submit and IS the endpoint the Desk
# UI actually hits for every Submit-button click (frappe/public/js/…/save.js). A literal string
# match on SAVEDOCS_METHOD alone misses it, so it is handled identically to savedocs (action-based).
# (Completeness audit, 2026-07-06.)
_SAVE_SUBMIT_ALIAS = "frappe.desk.form.save.submit"

# The docstatus-by-body class: these RPCs do `existing.update(doc); doc.save()` (or insert a doc as
# given), and frappe's `Document.save()` auto-detects a docstatus 0→1 / 1→2 in the BODY and runs the
# real submit/cancel hooks (`check_docstatus_transition`). So a body carrying `docstatus: 1|2` is a
# submit/cancel move on a doctype-in-body method — the same bypass class as the named endpoints, by
# CONTENT not name. Recognising the class (not just enumerating names) is what makes this robust to
# the next alias frappe adds. `frappe.client.save`/`.insert` carry a single nested `doc`; the
# multi-doc ones carry a `docs` list (mixed doctypes possible → a docstatus-changing item deny-closes,
# since one per-doctype grant cannot authorise a mixed batch). Draft bodies (docstatus 0/absent) are
# left to the existing doctype-blind CREATE residual, unchanged. (Completeness audit, 2026-07-06.)
_CLIENT_SAVE_METHODS = frozenset({"frappe.client.save", "frappe.client.insert"})
# `frappe.client.insert_many` / `frappe.client.bulk_update` (v1, dotted) AND the bare `bulk_update`
# (frappe's SEPARATE v2 `/api/v2/method/bulk_update` — a distinct undecorated function, classify()
# resolves it to the single-segment bare name) all carry a `docs` list of body-doctype docs.
_MULTI_DOC_SAVE_METHODS = frozenset(
    {"frappe.client.insert_many", "frappe.client.bulk_update", "bulk_update"})
# `frappe.desk.form.save.discard` moves a DRAFT (docstatus 0 → 2) via `Document.discard()` — a
# docstatus change on plain sibling (doctype, name) params, distinct from cancel. Scoped as the
# per-doctype `"<DocType>.discard"` grant (matching how a URL-path run_method=discard classifies),
# so a credential cannot discard arbitrary doctypes' drafts. (Completeness audit, 2026-07-06.)
_DESK_DISCARD_METHOD = "frappe.desk.form.save.discard"
# `cancel_all_linked_docs(docs)` cancels a whole list of {doctype,name} linked docs via direct
# `doc.cancel()` — always a docstatus move, always a (possibly mixed-doctype) batch → deny-closed for
# any scoped credential (the broker cancels a graph through its own per-node-marker cascade instead).
_CANCEL_ALL_LINKED_METHOD = "frappe.desk.form.linked_with.cancel_all_linked_docs"
_DOCSTATUS_BODY_VERB = {1: "submit", 2: "cancel"}


def _parse_doc(value):
    """A body param that carries a document as a JSON string or an already-parsed dict → the dict,
    else ``None``. Frappe pre-parses a JSON request body into ``form_dict`` verbatim, so ``value``
    is usually already a dict; the string branch covers form-encoded bodies."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


_DENY = object()  # sentinel: a submit/cancel move we cannot express as one per-doctype grant


def _docstatus_body_verb(doc):
    """Given a parsed doc dict, classify its BODY ``docstatus``: ``"submit"``/``"cancel"`` for a
    1/2 move, ``None`` for a draft (0 or absent — the create residual, unchanged), or :data:`_DENY`
    for a present-but-unparseable docstatus (can't confirm it is NOT a submit/cancel)."""
    if not isinstance(doc, dict):
        return _DENY
    ds = doc.get("docstatus")
    if ds is None:
        return None
    try:
        ds_int = int(ds)
    except (ValueError, TypeError):
        return _DENY
    if ds_int == 0:
        return None
    return _DOCSTATUS_BODY_VERB.get(ds_int, _DENY)


def _iter_body_docs(value):
    """Yield parsed doc dicts from a body param that may be a JSON-list string, a list, or a single
    dict — for the multi-doc save RPCs (``insert_many``/``bulk_update``/``cancel_all_linked_docs``)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    for item in items:
        parsed = _parse_doc(item) if not isinstance(item, dict) else item
        if isinstance(parsed, dict):
            yield parsed


# savedocs' `action` -> the docstatus verb it drives (mirrors frappe's own
# `{"Save": 0, "Submit": 1, "Update": 1, "Cancel": 2}` map in frappe/desk/form/save.py -- Update
# re-saves an already-submitted doc via the same `doc.submit()` call Submit makes, so it carries
# the identical permission requirement). `"Save"` (draft) is deliberately absent: it is not a
# submit/cancel at all, handled as its own branch below.
_SAVEDOCS_ACTION_VERB = {"Submit": "submit", "Update": "submit", "Cancel": "cancel"}


def _run_doc_method_doctype(form):
    """Pull the target DocType out of a ``run_doc_method`` RPC's form params. Deny-biased:
    unparseable/missing/AMBIGUOUS returns ``None``.

    The doctype can travel in three places, and WHICH one frappe actually acts on is version- and
    presence-dependent:
      - v1 (``frappe.handler.run_doc_method``): ``dt`` IS the doctype name directly when present (the
        doc is looked up in the DB by ``dt``/``dn``); when ``dt`` is absent the doc travels in-memory
        in the ``docs`` param (JSON string / dict carrying its own ``doctype``).
      - v2 (``frappe.api.v2.run_doc_method(method, document, kwargs)``): the doc travels in the
        ``document`` param -- and v2's signature has **no ``dt`` param at all**, so frappe SILENTLY
        DROPS a ``dt`` sent alongside ``document`` and acts on ``document``.

    That version split is a cross-doctype spoof if this function just trusts ``dt``: a caller scoped
    to ``Sales Invoice.*`` sends ``dt="Sales Invoice"`` + ``document={"doctype":"Journal Entry"}``;
    a dt-first guard authorizes ``Sales Invoice.submit`` while frappe v2 submits the Journal Entry
    (and the same decoy slides a ``Bulk Update`` ``document`` past the hard-deny). This pure function
    can't see the request path/version, so rather than guess frappe's arg precedence it is
    **deny-biased on ambiguity**: it collects the doctype named by EVERY present source and resolves
    only when they AGREE (one distinct doctype). Zero sources, or two sources naming different
    doctypes, return ``None`` -> the caller fails closed. A legitimate client names one doctype in
    one place; only a spoof names two.
    """
    candidates = set()
    dt = form.get("dt")
    if isinstance(dt, str) and dt.strip():
        candidates.add(dt.strip())
    for key in ("docs", "document"):
        doctype = _doctype_from_doc_param({"doc": form.get(key)})
        if doctype:
            candidates.add(doctype)
    if len(candidates) != 1:
        return None  # ambiguous (dt decoy vs body doc) or absent -- deny-biased
    return next(iter(candidates))


def _body_doctype_rewrite(doctype, verb):
    """Shared success/fail-closed shape for :func:`body_scoped_target`: a recognised submit/cancel
    RPC whose doctype extracted cleanly rewrites to the per-doctype method target; one whose
    doctype could NOT be extracted fails CLOSED (``("other", None)``) -- it must never fall back to
    the original doctype-blind target, or a credential holding a blind grant on that method name
    keeps the very bypass this closes."""
    if not doctype:
        return ("other", None)
    return ("method", f"{doctype}.{verb}")


def body_scoped_target(kind, target, http_method, form):
    """Resolve a body-doctype submit/cancel RPC to the SAME per-doctype
    ``("method", "<DocType>.submit"/"cancel")`` shape :func:`classify` already produces for a
    URL-path ``run_method`` call. Pure -- ``enforce.py`` is the only caller, feeding it the already-
    classified ``(kind, target)`` plus the request's ``http_method`` and merged ``form_dict``.

    Recognises the frappe RPCs that perform a docstatus change (submit / cancel / discard) by
    carrying the target doctype in the request body — the dedicated endpoints AND the
    docstatus-by-body-content class:
      - ``frappe.model.workflow.apply_workflow`` (nested ``doc``, same shape as ``frappe.client.submit``)
        — rewritten to ``"<DocType>.apply_workflow"`` (NOT ``.submit``/``.cancel``: this is the
        sanctioned Workflow-transition path, scoped as its own grant, never folded into the plain
        submit/cancel one);
      - ``frappe.client.submit`` / ``.cancel`` (nested ``doc`` / plain ``doctype``+``name``);
      - the Desk ``frappe.desk.form.save.cancel`` and ``.discard`` (plain ``doctype``+``name``);
      - the bulk ``…bulk_update.submit_cancel_or_update_docs`` (``doctype`` + ``action``);
      - ``frappe.desk.form.save.savedocs`` AND its alias ``…save.submit`` (``action``-driven);
      - ``run_doc_method`` (bare + dotted ``frappe.handler.run_doc_method``) resolved to
        ``"<DocType>.<method>"`` for EVERY inner method (v1 ``dt``/``docs``, v2 ``document``) — not
        just submit/cancel/discard, since the bare name is doctype-AND-method-blind;
      - ``frappe.client.save``/``.insert`` when the body ``doc`` carries ``docstatus`` 1/2 (a
        submit/cancel via ``Document.save()``'s docstatus-transition auto-detection);
      - ``frappe.client.insert_many`` / ``.bulk_update``, the bare v2 ``bulk_update``, and
        ``cancel_all_linked_docs`` — multi-doc batches — deny-closed the moment they carry a
        docstatus-changing item (a mixed-doctype batch cannot be authorised by one per-doctype grant).

    KNOWN RESIDUAL (a pure, I/O-free classifier cannot close it): a 2-hop laundering vector where the
    target doctype is NOT in the request at all — e.g. a ``Bulk Update`` DocType record whose own
    ``document_type``/``field=docstatus`` fields drive a submit/cancel when its instance method runs.
    Closing that needs a DB read (out of this function's contract). The real backstop is posture:
    grant per-doctype patterns, not raw generic-RPC method names — see the guard README "deny-unknown"
    note. Flagged, not silently omitted.

    KNOWN HARD-DENY (not a residual — closed by ``is_permitted`` directly, not here): the ``Bulk
    Update`` 2-hop laundering vector (its instance method reads ``document_type``/``field`` from the
    SAVED RECORD, never the request body) is an *ungrantable* target regardless of grant/resolution
    — see ``is_permitted``'s ``_UNGRANTABLE_METHOD_DOCTYPES`` check, which runs BEFORE the grant
    check on ANY method target whose doctype-part is ``"Bulk Update"`` (both this function's own
    rewrite of the bulk RPC above, and a direct ``run_doc_method``/URL-path route to the same
    instance method). Other container-DocType vectors of the same 2-hop shape remain un-audited — a
    post-Gate-10 follow-up, not silently claimed closed.

    Returns:
      * ``("method", "<DocType>.<verb>")`` -- doctype (and, for ``run_doc_method``, method name) all
        extracted successfully: ``submit``/``cancel``/``discard``/``apply_workflow`` for the named
        endpoints above, or any inner method name for ``run_doc_method``. The caller enforces this
        via ``is_permitted``'s ``kind == "method"`` branch ONLY (``scope.methods`` fnmatch, with the
        deny-unknown ``method_resolved=True`` this rewrite exists to supply) -- never
        ``resource_doctypes``/``resource_verbs``, matching the URL-path shape byte-for-byte.
      * ``("other", None)`` -- a RECOGNISED body-doctype RPC whose doctype (or, for
        ``run_doc_method``, method name) could not be confirmed, or a multi-doc batch containing a
        docstatus-changing item. Fails closed.
      * ``None`` -- not a body-doctype submit/cancel shape at all: a different method, or a
        ``savedocs``/``save``/``insert`` plain DRAFT (docstatus 0/absent). The caller keeps enforcing
        the ORIGINAL ``classify()`` target — which, under 0.6.0 deny-unknown, is now DENIED as a
        bare/unresolved method unless it is on ``SAFE_METHODS`` (the pre-0.6.0 "doctype-blind CREATE
        residual" is thus no longer grantable by name; a scoped credential creates drafts via
        ``POST /api/resource/<DocType>`` under ``allow_resource``).
    """
    if kind != "method" or not isinstance(target, str) or not isinstance(form, dict):
        return None
    if target == APPLY_WORKFLOW_METHOD:
        # apply_workflow(doc, action) calls doc.submit()/doc.cancel() internally -- a doctype-blind
        # submit/cancel if left as the bare method name. Deliberately NOT put on SAFE_METHODS (it
        # mutates docstatus); resolved per-doctype instead, same nested `doc` param shape as
        # frappe.client.submit (the broker sends {"doc": {"doctype": .., "name": ..}, "action": ..}).
        # Rewritten to "<DocType>.apply_workflow" -- not ".submit"/".cancel" -- so a credential must
        # hold the apply_workflow-shaped grant specifically (not silently ride an existing
        # ".submit"/".cancel" grant), and so `is_docstatus_changing`'s own APPLY_WORKFLOW_METHOD
        # exemption (the sanctioned path) is not accidentally re-flagged by the generic ".submit"/
        # ".cancel" suffix check once rewritten.
        return _body_doctype_rewrite(_doctype_from_doc_param(form), "apply_workflow")
    if target == _BODY_SUBMIT_METHOD:
        return _body_doctype_rewrite(_doctype_from_doc_param(form), "submit")
    if target in (_BODY_CANCEL_METHOD, _DESK_CANCEL_METHOD, _DESK_DISCARD_METHOD):
        # Unlike submit/savedocs, these take (doctype, name) as PLAIN SIBLING params, never a nested
        # `doc` dict -- so they do NOT go through _doctype_from_doc_param. cancel/desk-cancel are a
        # cancel; desk-discard is a distinct draft-destroying move scoped as "<DocType>.discard".
        verb = "discard" if target == _DESK_DISCARD_METHOD else "cancel"
        doctype = form.get("doctype")
        return _body_doctype_rewrite(
            doctype.strip() if isinstance(doctype, str) and doctype.strip() else None, verb
        )
    if target == _BULK_UPDATE_METHOD:
        # Bulk submit/cancel/update: `doctype` is a plain sibling param; the docstatus verb is in
        # `action`. Only submit/cancel rewrite to a per-doctype grant; "update" (and any unknown or
        # missing action) fails CLOSED -- a bulk arbitrary-field write must never ride a blind grant.
        # `action` isinstance-guarded before the dict lookup: `_BULK_ACTION_VERB.get(action)` would
        # raise TypeError for an UNHASHABLE action (a list/dict body glitch), not a lookup miss --
        # form_dict extraction seam, treated the same as any other unrecognised action (deny-closed
        # below), never a crash.
        action = form.get("action")
        verb = _BULK_ACTION_VERB.get(action) if isinstance(action, str) else None
        if verb is None:
            return ("other", None)
        doctype = form.get("doctype")
        return _body_doctype_rewrite(
            doctype.strip() if isinstance(doctype, str) and doctype.strip() else None, verb
        )
    if target in (SAVEDOCS_METHOD, _SAVE_SUBMIT_ALIAS):
        # `frappe.desk.form.save.submit` is the same function as savedocs (a bare alias) — the Desk
        # UI's real Submit-click endpoint — so it is judged by the same `action` param.
        action = form.get("action")
        if action == _DRAFT_ACTION:
            return None  # a plain draft save is not submit/cancel -- out of this increment's scope
        # isinstance-guarded before the dict lookup for the same reason as the bulk-action fix above:
        # `_SAVEDOCS_ACTION_VERB.get(action)` on an unhashable action (list/dict) raises TypeError,
        # not a lookup miss -- treated as an unrecognised action (deny-closed below), never a crash.
        verb = _SAVEDOCS_ACTION_VERB.get(action) if isinstance(action, str) else None
        if verb is None:
            return ("other", None)  # unknown/missing action -- can't confirm this ISN'T a
            # docstatus move, deny-biased (unlike the confirmed-Save case above)
        return _body_doctype_rewrite(_doctype_from_doc_param(form), verb)
    if target in _CLIENT_SAVE_METHODS:
        # frappe.client.save/.insert primarily CREATE drafts, but frappe's Document.save() detects a
        # docstatus 1/2 carried in the body and runs the real submit/cancel hooks. So a body with
        # docstatus 1/2 is a per-doctype submit/cancel move; a draft (0/absent) stays the documented
        # doctype-blind create residual (unchanged, returns None).
        verb = _docstatus_body_verb(_parse_doc(form.get("doc")))
        if verb is _DENY:
            return ("other", None)
        if verb is None:
            return None
        return _body_doctype_rewrite(_doctype_from_doc_param(form), verb)
    if target in _MULTI_DOC_SAVE_METHODS:
        # insert_many / client.bulk_update carry a `docs` LIST — mixed doctypes possible, so ANY
        # docstatus-changing item deny-closes (one per-doctype grant cannot authorise a mixed batch);
        # an all-draft batch stays the create residual (None).
        for d in _iter_body_docs(form.get("docs")):
            if _docstatus_body_verb(d) is not None:  # submit/cancel OR unparseable-docstatus
                return ("other", None)
        return None
    if target == _CANCEL_ALL_LINKED_METHOD:
        # A batch cancel of linked docs (any/mixed doctypes) — never expressible as one per-doctype
        # grant; a scoped credential cancels a graph via the broker's per-node-marker cascade instead.
        return ("other", None)
    if target in _RUN_DOC_METHOD_NAMES:
        # Resolve to "<DocType>.<method>" for EVERY inner method, not just submit/cancel/discard --
        # `run_doc_method` is doctype-AND-method-blind by name alone (the bare "run_doc_method"
        # string says nothing about which doctype or which controller method it dispatches to), so
        # a credential granted only e.g. "Sales Invoice.get_pdf" must not reach ANY other doctype's
        # get_pdf (or any other method) through it. A missing/non-str method name can't be resolved
        # at all -- deny-close rather than fall back to the doctype-blind bare name.
        method_name = form.get("method")
        if not isinstance(method_name, str) or not method_name.strip():
            return ("other", None)
        return _body_doctype_rewrite(_run_doc_method_doctype(form), method_name.strip())
    return None


def docstatus_target_doctype(kind, target, form=None):
    """Pure: extract the DocType name a docstatus-changing ``(kind, target[, form])`` names, for the
    Workflow-existence lookup ``enforce.py`` feeds to frappe's internal ``get_workflow_name``.
    Returns ``None`` when no doctype can be derived (the glue then treats that the same as a
    failed lookup — deny-biased, never read as "no workflow").

    ``kind == "method"`` and target is :data:`SAVEDOCS_METHOD`: the doctype is NOT in the method
    name — it lives in the ``doc`` body param (:func:`_doctype_from_doc_param`, deny-biased).
    Otherwise the target is ``"<DocType>.<method>"`` — split on the LAST ``.`` so a doctype name
    with no dot of its own resolves correctly. A target with no ``.`` at all returns ``None`` (a
    defensive edge: a bare ``cmd=submit`` with no prefix).

    ``kind == "resource"``: the target IS ``(doctype, verb)`` — the first element, verbatim.
    """
    if kind == "method" and isinstance(target, str):
        if target == SAVEDOCS_METHOD:
            return _doctype_from_doc_param(form)
        if "." in target:
            return target.rsplit(".", 1)[0]
    if kind == "resource" and isinstance(target, (tuple, list)) and target:
        return target[0]
    return None
