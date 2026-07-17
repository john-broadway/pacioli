# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — ERPNEXT: the REST client (glue, stdlib-only).

Thin, shape-pinned calls to a bench's documented REST surface, over an **injected transport**
(``transport(method, url, headers, params, body) -> (status, json_or_None)``) so every request the
broker can send is unit-tested without a network; a urllib default transport is provided for real
use. The calls become *proven* only against a live bench (SPEC §7) — the tests pin what we send,
the live falsification pins what the bench accepts.

Deliberate shapes (each is a security decision, not a convenience):

* **Submit rides the v1 doc-method surface** — ``POST /api/resource/Sales Invoice/<name>`` with
  ``run_method=submit`` in the **query string**. That is the one submit shape ``guard`` can scope
  narrowly (it classifies as the method ``"Sales Invoice.submit"``); the generic
  ``frappe.client.submit`` RPC takes its target from the request body and cannot be scoped by
  doctype. The query string (not the form body) is used so the classifier's ``form_dict`` read
  always sees it, whatever the body encoding.
* **The broker never sends** ``adv_adj`` **or rewrites** ``posting_date`` — the two levers that
  slip past ERPNext's period locks (the closed-books check, SPEC §3).
* **Doc names are fully URL-quoted** (``safe=""``) — Frappe allows ``/`` in names (naming series),
  and an unquoted slash would silently address a different resource path.
* **Period locks are read, never guessed** (``get_period_locks``): the frozen-till-date boundary
  is read from **both** ``Company.accounts_frozen_till_date`` (the v16 source) and the legacy
  ``Accounts Settings.acc_frozen_upto`` (v15) — the LATER of the two if both carry a value — plus
  the latest submitted Period Closing Voucher. **The Accounting Period check is doctype- and
  date-range-aware (F-S1)** — ``get_period_locks`` takes ``doctype``/``posting_date`` as REQUIRED
  parameters (no default: a default would silently reintroduce doctype-blindness), LISTs the
  periods that CONTAIN ``posting_date`` for this company, then reads each hit's ``disabled`` flag
  and ``closed_documents`` child rows with a second, full-document GET (the list endpoint never
  expands child tables) — refusing only when a period is enabled AND closes *this* doctype,
  matching ERPNext's own enforcement (``accounting_period.py``) instead of the prior
  fail-safe-not-fail-correct shape that refused every doctype past the latest period's end date
  regardless of what that period actually closed. **The LIST itself no longer filters on
  ``disabled`` (F-C1, v15 compatibility)** — that column is v16-only (absent from a v15 bench's
  ``Accounting Period`` schema), and frappe's list-filter builder has no meta-validation or
  sanitizer (``frappe/model/db_query.py::build_filter_conditions``/``prepare_filter_condition``),
  so filtering on a column a v15 bench doesn't have turns every governed op into an "unknown
  column" 500/417 — a v15-wide outage, not a lock. ``disabled`` is read instead from the same
  full-document item GET that already fetches ``closed_documents``: absent (the v15 shape) is
  treated as enabled (v15 has no period-disable concept — that's correct, not a fallback), and a
  truthy ``disabled`` on the full doc (v16) skips the period before its ``closed_documents`` rows
  are even inspected — a disabled period locks nothing, the same F-S1/PHASE-T semantics as before,
  now read from the item GET instead of the LIST filter. See ``get_period_locks``'s own docstring
  for the deny-bias rules and the deliberately-not-modeled ``exempted_role``. An unreadable lock
  source, or a malformed period/child row (including an unparseable ``disabled`` value), **raises**
  — the closed-books check refuses on "can't verify", it never treats unreadable or unparseable as
  unlocked.
* **Amend is a resource CREATE** (``POST /api/resource/Sales Invoice``, the collection URL) —
  ERPNext has no native ``amend()`` server method, so the amended draft is inserted with the
  payload :func:`pacioli.amend.amend_payload` builds (never the raw source document). Honest
  guard implication: ``pacioli_guard``'s resource grants are **not verb-granular**, so the
  Sales Invoice resource grant that already admits reads admits this create too.

**Breadth (Purchase Invoice) — doctype-generic client methods.** The read/plan/execute/amend
calls were confirmed generic from ERPNext source (the doc-method submit/cancel surface, the
resource-CRUD amend shape, and the native ledger-preview RPC all take ``doctype`` as a plain
argument) and generalized accordingly: ``get_document``/``list_documents``/``submit_document``/
``cancel_document``/``get_doc_for_amend``/``find_amendments``/``create_amended_draft`` all take an
explicit ``doctype``; :data:`SUPPORTED_DOCTYPES` is the broker's own "I've been built and tested
for these" allowlist (Sales Invoice, Purchase Invoice), distinct from — and belt-and-suspenders
alongside — ``pacioli_guard``'s per-credential ``resource_doctypes`` grant (SPEC, guard README).
**Honest limit:** every Purchase Invoice request shape here is knowledge-pinned from ERPNext's
documented REST conventions (the same generic surface Sales Invoice already rides, source-read
2026-07-03) — it has **not** been live-verified against a bench; live falsification of the PI path
is a future bench gate, exactly like the rest of this module (SPEC §7).

**Spine fix — the v16 frozen-books gap.** ``get_period_locks`` previously read only ``Accounts
Settings.acc_frozen_upto`` for the frozen-till-date boundary. ERPNext v16 migrated that field onto
``Company.accounts_frozen_till_date`` (``erpnext/patches/v16_0/migrate_account_freezing_settings_
to_company.py`` moves the value; the field is absent from ``accounts_settings.json`` on a v16
bench) — the real enforcement (``general_ledger.check_freezing_date``) reads Company, not Accounts
Settings, so the broker's lock was silently always-absent against a v16 bench. This now reads
**both** sources (Company for v16, Accounts Settings for an unmigrated v15 bench) and honors the
LATER date if both carry a value. **Reading Company is a NEW scope requirement** — an existing
broker credential scoped before this fix needs a new grant (Company DocType read) or every
plan/submit/cancel on that target will raise (an unreadable Company doc is a deny, never a silent
"no lock" — see ``pacioli/doctor.py``'s Company-read probe).

**Breadth (Payment Entry) — a third doctype.** :data:`SUPPORTED_DOCTYPES` gains ``"Payment
Entry"`` (``party_field="party"`` — the header-level field Payment Entry carries for both
Customer and Supplier payments; an Internal Transfer payment carries no party at all, which the
list tier surfaces as an absent field like any other doctype's missing value, same as everywhere
else in this module). Payment Entry's own ``references`` child rows (one per invoice/order/JE it
settles) are read and disclosed at the tool layer (``tools.py``), not here — this client stays as
doctype-blind for Payment Entry as it already was for Sales/Purchase Invoice. ``get_gl_entries``'s
field list now also carries ``against_voucher_type``/``against_voucher`` — for a Payment Entry
cancel a single voucher can touch N invoices at once (unlike SI/PI's one-document cancel radius),
so the projected-reversal rows need to say which invoice each is against.

**Breadth (Journal Entry) — a fourth doctype, and the first with no header-level party.**
:data:`SUPPORTED_DOCTYPES` gains ``"Journal Entry"`` with ``party_field=None`` — confirmed from
``journal_entry.json`` (the ERPNext v16 source checkout): the parent doctype carries no
Customer/Supplier-shaped field at all, only a boolean ``party_not_required`` and per-line
``party``/``party_type`` inside the ``accounts`` child table. ``_list_fields`` (below) treats
``None`` as "omit the party column", the one genuine consumer-side branch this forces
(``list_documents``'s ``party_field`` parameter already accepted any string; passing ``None``
through was the only gap). Journal Entry ALSO carries neither ``status`` nor ``grand_total`` —
confirmed absent from the same JSON — so its list-tier field set is its own branch, not a
one-field patch: ``docstatus`` (frappe's universal field, always present) stands in for
``status``, and ``total_debit``/``total_credit`` (the parent's own balance-check fields, set by
ERPNext's ``set_total_debit_credit`` on every save/validate) stand in for ``grand_total``, plus
``voucher_type`` for context (Bank/Cash/Contra/etc. — the field the JE-specific plan risk flags in
``tools.py`` key off). The read/plan/execute/amend surface itself needs **no** further client
change — ``get_document``/``submit_document``/``cancel_document``/``get_doc_for_amend``/
``find_amendments``/``create_amended_draft``/``get_gl_entries``/``get_active_workflows``/
``apply_workflow`` were already fully doctype-generic (design confirmed from source: the doc-method
submit/cancel surface, the resource-CRUD amend shape, and the native ledger-preview RPC all take
``doctype`` as a plain argument, and ``show_accounting_ledger_preview`` dispatches to
``doc.make_gl_entries()`` polymorphically — ``JournalEntry.make_gl_entries`` matches the call
shape exactly, confirmed by reading ``journal_entry.py``).

:meth:`get_accounts_settings` is new here — a small, doctype-blind read of the site's single
``Accounts Settings`` doctype for whichever fields the caller names. It exists for the Journal
Entry-specific ``plan_cancel`` disclosure in ``tools.py`` (whether
``unlink_payment_on_cancellation_of_invoice`` is on — it changes a cancel's blast radius from "a
generic-link cancel refusal" to "a silent raw-SQL unlink of other submitted JEs/Payment Entries",
scout-je.md §2/§5), but the method itself names no doctype and is reusable by any future disclosure
that needs another Accounts Settings field — unlike ``get_period_locks``, an unreadable read here
is the CALLER's decision whether to refuse or treat as absent (this method itself just raises on
an unreadable bench response, the same as every other read in this client).

**F-R1 — the settling-PE disclosure on cancel, doctype-generic.**
:meth:`get_settling_references` reads **Payment Ledger Entry** (the settlement ledger since
ERPNext v14) filtered on ``against_voucher_type``/``against_voucher_no`` = the target document —
doctype-blind by construction, so it surfaces whatever settles the target (a Payment Entry, most
commonly, since it alone sits in stock ERPNext's ``auto_cancel_exempted_doctypes``, but the read
never assumes that union stays that short). This exists because a cancel of ANY supported doctype
can silently unlink a settling voucher's allocation with no doc event and no separate consent —
ERPNext's own cancel blast-radius read (``get_submitted_linked_docs``) structurally cannot surface
it, since the exempt list removes the settling voucher from that traversal's allowed-source set at
two points (frappe's ``linked_with.py``). GL-entries-shaped (explicit fields/filters,
``limit_page_length: "0"``, F-V1 law) with the same structured-deny-on-non-list-body house pattern
:meth:`get_period_locks` already applies to its Accounting Period LIST read. Raises on an
unreadable response, same as every read in this client — ``tools.py`` is where that becomes a
whole-plan refusal (deny-biased, pin sheet ``docs/plans/2026-07-07-fr1-settling-pe-disclosure.md``)."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from pacioli.amend import amend_payload
# Package-private, same discipline as doctor.py importing registry._resolve_ref /
# runtime._SEAL_KEY_BYTES: glue reusing a pure-core helper rather than re-deriving its own copy of
# the ISO-date shape (and risking the two silently drifting apart). check_red_line (plan.py) is
# the pure core that ultimately enforces posting_date's shape; this client validates it BEFORE
# spending a network round-trip building a query on an unverifiable date (F-S1).
from pacioli.plan import _is_iso_date

PREVIEW_METHOD = "erpnext.controllers.stock_controller.show_accounting_ledger_preview"

SALES_INVOICE = "Sales Invoice"
PURCHASE_INVOICE = "Purchase Invoice"
PAYMENT_ENTRY = "Payment Entry"
JOURNAL_ENTRY = "Journal Entry"

# The two submit/cancel TRANSPORTS a SUPPORTED_DOCTYPES entry can name (see "submit_via" below).
SUBMIT_VIA_RUN_METHOD = "run_method"
SUBMIT_VIA_CLIENT_RPC = "client_rpc"

# The body-doctype override-submit RPC names (client_rpc transport). Knowledge-pinned from frappe
# source (frappe/client.py): `submit(doc)` does `frappe.get_doc(frappe.parse_json(doc)); doc.submit()`
# — the FULL doc travels in the body, reconstructed server-side, never re-fetched from the DB.
# `cancel(doctype, name)` does `frappe.get_doc(doctype, name); wrapper.cancel()` — doctype/name are
# plain sibling params; the doc is loaded fresh from the DB, no body payload needed. Both are
# ordinary /api/method RPCs, so their return value rides frappe's `response["message"]` envelope
# exactly like `ledger_preview`/`apply_workflow` — never the /api/resource `"data"` envelope.
_CLIENT_SUBMIT_METHOD = "frappe.client.submit"
_CLIENT_CANCEL_METHOD = "frappe.client.cancel"

# The broker's own per-doctype config (design §B) — "I've been built and tested for these", not
# the guard's per-credential resource-doctype allowlist. A pacioli_doctype outside this set is a
# structured deny at the tool layer (tools.py), never reaches this client. Knowledge-pinned, not
# live-verified (see module docstring). Journal Entry's party_field is None — it carries no
# header-level party at all (see module docstring); every consumer of this dict must treat None
# as "there is no party column to splice in", never as a missing/blank string.
#
# **submit_via** (override-doctype submit path, PHASE L / SCOPED-TOKEN-PROOF.md): SI/PI/PE stay on
# the proven `SUBMIT_VIA_RUN_METHOD` (the URL-path `run_method=submit`/`cancel` doc-method surface
# — the only shape `pacioli_guard` could ORIGINALLY scope per-doctype, since `doctype` travels in
# the URL). Journal Entry alone is `SUBMIT_VIA_CLIENT_RPC`: `JournalEntry` overrides
# `submit()`/`cancel()` (>100-row background queuing,
# `erpnext/accounts/doctype/journal_entry/journal_entry.py:186,195`) WITHOUT `@frappe.whitelist()`,
# so frappe's REST handler 403s the run_method vector for JE specifically — SI/PI/PE override
# neither and are unaffected. `frappe.client.submit`/`.cancel` is body-doctype (the doctype travels
# in the request body, not the URL), which is why this is only safe to enable now that
# `pacioli_guard.scope.body_scoped_target` parses that body and enforces the credential's
# per-doctype grant on it exactly as strictly as the URL-path shape — see guard CHANGELOG 0.5.0.
# KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (Gate 10, next armed window).
SUPPORTED_DOCTYPES = {
    SALES_INVOICE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD},
    PURCHASE_INVOICE: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD},
    PAYMENT_ENTRY: {"party_field": "party", "submit_via": SUBMIT_VIA_RUN_METHOD},
    JOURNAL_ENTRY: {"party_field": None, "submit_via": SUBMIT_VIA_CLIENT_RPC},
}


def _list_fields(doctype, party_field):
    """The list-tier field set. For every doctype except Journal Entry: the doctype's own party
    field spliced in (``customer`` for Sales Invoice, ``supplier`` for Purchase Invoice, ``party``
    for Payment Entry) alongside the original baked list, unchanged from before this breadth
    increment. **Journal Entry is its own branch, not a one-field patch** — confirmed absent from
    ``journal_entry.json``: no header-level party field (so nothing to splice), no ``status``
    field (``docstatus`` is Journal Entry's only status signal), and no ``grand_total`` (its
    balance lives in ``total_debit``/``total_credit`` instead). ``voucher_type`` rides along too —
    the field the JE-specific plan risk flags (``tools.py``) key off, useful context for anyone
    listing journal entries."""
    if doctype == JOURNAL_ENTRY:
        return ["name", "docstatus", "voucher_type", "company", "posting_date",
                "total_debit", "total_credit", "modified"]
    return ["name", "status", "docstatus", party_field, "company", "posting_date",
            "grand_total", "modified"]


class ErpnextError(Exception):
    """A refused/failed bench call. Carries the HTTP ``status`` (or ``None`` for a shape problem).
    Messages carry the bench's own reason but never credential material.

    ``answered`` (transport taxonomy, docs/plans/2026-07-07-transport-taxonomy.md) is truthy ONLY
    when an int HTTP status arrived together with a successfully-parsed frappe JSON body (the bench
    definitely saw and refused the call), or when the status is a pre-processing rejection (429/413,
    which trip before the handler ever runs, any body). It defaults ``False`` — a raw exception, a
    connection-level failure, or a non-JSON ("proxy-shaped") body is never assumed to be an answered
    refusal; ``spine``/``cascade`` read this attribute (via ``getattr(exc, "answered", False)``, so
    even a bare, unconverted exception classifies deny-biased as "no answer") to decide whether an
    exception from the mutating call releases the consent marker (answered) or must spend it and
    resolve via a governed readback (everything else)."""

    def __init__(self, message, status=None, answered=False):
        super().__init__(message)
        self.status = status
        self.answered = answered


_PRE_HANDLER_STATUSES = (429, 413)  # rate-limited / body-too-large — trip before dispatch (Scout A)


# Frappe stamps these keys on its V1 error envelopes (`report_error` sets ``exc_type``
# unconditionally on every error response, frappe/utils/response.py). A generic JSON-speaking
# proxy's error body ({"error": ...}, {"message": ...}) carries neither — and the list must STAY
# this narrow: "message"/"data"/"error" are exactly the keys proxies use too (redteam catch).
_FRAPPE_ENVELOPE_KEYS = ("exc_type", "_server_messages")


def _answered(status, payload):
    """The transport taxonomy's classification rule (docs/plans/2026-07-07-transport-taxonomy.md):
    an int status with a parsed JSON body carrying FRAPPE's own error-envelope evidence
    (``exc_type`` / ``_server_messages``) means the bench definitely saw and answered the call;
    429/413 are always pre-processing rejections, safe to treat as answered wherever emitted,
    body or not (status alone decides for those two). A dict WITHOUT frappe's envelope keys is a
    JSON-speaking proxy's error page — unknown progress, treated exactly like no answer
    (redteam-reproduced: a Traefik/ALB 502 with ``{"error": "Bad Gateway"}`` must NOT release
    the marker). Anything else is deny-biased ambiguity — ``False``."""
    if status in _PRE_HANDLER_STATUSES:
        return True
    return isinstance(payload, dict) and any(k in payload for k in _FRAPPE_ENVELOPE_KEYS)


def _extract_server_reason(payload):
    """Pull the human reason out of frappe's error envelope, defensively."""
    if not isinstance(payload, dict):
        return ""
    parts = []
    if payload.get("exc_type"):
        parts.append(str(payload["exc_type"]))
    raw = payload.get("_server_messages")
    if isinstance(raw, str):
        try:
            for item in json.loads(raw):
                msg = json.loads(item) if isinstance(item, str) else item
                if isinstance(msg, dict) and msg.get("message"):
                    parts.append(str(msg["message"]))
                elif isinstance(msg, str):
                    parts.append(msg)
        except (ValueError, TypeError):
            parts.append(raw)
    elif payload.get("message"):
        parts.append(str(payload["message"]))
    return ": ".join(parts)


def default_transport(method, url, headers, params=None, body=None, timeout=30):
    """The real (urllib) transport. Returns ``(status, parsed_json_or_None)``."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers={**headers, "Accept": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — scheme validated by registry
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        # An answered HTTP error response — handled FIRST and separately from the broadened
        # OSError catch below, even though HTTPError is itself a URLError/OSError subclass: this
        # branch means the bench (or a proxy standing in for it) actually sent a status line, so
        # it must classify via `_call`'s own status+body rule, never as "no answer".
        return exc.code, _parse_json(exc.read())
    except OSError as exc:
        # Broadened from (URLError, TimeoutError) to the whole OSError family (transport
        # taxonomy) — URLError and the builtin TimeoutError are BOTH already OSError subclasses,
        # so this collapses cleanly and additionally catches raw connection-level failures that
        # used to escape unconverted (ConnectionResetError, ConnectionAbortedError, BrokenPipeError
        # mid-read, etc.). `status=None`/`answered=False` (the defaults) — a positive-proof
        # classification already treats "no answer" as the deny-biased default, so this broadening
        # is belt-and-suspenders, not load-bearing.
        raise ErpnextError(f"cannot reach the bench: {exc}") from exc


def _parse_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class ErpnextClient:
    """Shape-pinned REST calls for slice-one, authenticated as the scoped broker user."""

    def __init__(self, base_url, api_key, api_secret, transport=default_transport):
        self._base = base_url.rstrip("/")
        self._auth = f"token {api_key}:{api_secret}"
        self._transport = transport

    # --- plumbing ------------------------------------------------------------------
    def _call(self, method, path, params=None, body=None):
        status, payload = self._transport(
            method, f"{self._base}{path}", {"Authorization": self._auth},
            params=params, body=body,
        )
        if not 200 <= status < 300:
            reason = _extract_server_reason(payload) or "bench refused the call"
            raise ErpnextError(f"HTTP {status}: {reason}", status=status,
                               answered=_answered(status, payload))
        if not isinstance(payload, dict):
            # An in-range status but a non-JSON body — still proxy-shaped ambiguity in spirit
            # (something between the broker and the bench mangled the response), so this stays
            # unanswered too (`_answered` returns False here: payload is never a dict on this
            # branch, and a 2xx status can never be 429/413).
            raise ErpnextError("bench returned a non-JSON response", status=status,
                               answered=_answered(status, payload))
        return payload

    def _data(self, payload):
        if "data" not in payload:
            raise ErpnextError("bench response has no 'data' envelope")
        return payload["data"]

    @staticmethod
    def _doc_path(doctype, name):
        if not isinstance(name, str) or not name.strip():
            raise ErpnextError("a document name is required")
        return (f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
                f"/{urllib.parse.quote(name, safe='')}")

    # --- read tier -----------------------------------------------------------------
    def get_document(self, doctype, name):
        """Read one document of ``doctype`` (permission-scoped item GET). Generalized in the
        Purchase Invoice breadth increment — was ``get_sales_invoice(name)``; every call site
        now passes ``doctype`` explicitly (``SALES_INVOICE``/``PURCHASE_INVOICE``)."""
        return self._data(self._call("GET", self._doc_path(doctype, name)))

    def list_documents(self, doctype, filters=None, limit=20, party_field="customer"):
        """List documents of ``doctype`` (permission-scoped). ``party_field`` selects which field
        carries the counterparty in the returned rows — ``"customer"`` for Sales Invoice,
        ``"supplier"`` for Purchase Invoice, ``"party"`` for Payment Entry, ``None`` for Journal
        Entry, which carries no header-level party at all (:data:`SUPPORTED_DOCTYPES`); the
        caller (tools.py) supplies it, this client has no doctype config of its own. Generalized
        in the Purchase Invoice breadth increment — was ``list_sales_invoices(filters, limit)``."""
        params = {
            "fields": json.dumps(_list_fields(doctype, party_field)),
            "limit_page_length": str(int(limit)),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        path = f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
        return self._data(self._call("GET", path, params=params))

    # --- PLAN ----------------------------------------------------------------------
    def ledger_preview(self, company, doctype, docname):
        """ERPNext's native dry-run: savepoint → make_gl_entries in memory → rollback."""
        payload = self._call("POST", f"/api/method/{PREVIEW_METHOD}",
                             body={"company": company, "doctype": doctype, "docname": docname})
        if "message" not in payload:
            raise ErpnextError("preview response has no 'message' envelope")
        return payload["message"]

    # --- execute -------------------------------------------------------------------
    def submit_document(self, doctype, name, doc=None):
        """One of the two state-changing verbs, for any doctype in :data:`SUPPORTED_DOCTYPES`.

        Branches on the doctype's configured ``submit_via`` (module docstring / dict comment):

        * ``SUBMIT_VIA_RUN_METHOD`` (SI/PI/PE, unchanged, live-proven) — sends
          ``run_method=submit`` and NOTHING else — no adv_adj, no posting_date, no doc payload
          (the draft is submitted as it stands). ``doctype`` travels in the URL path, which is
          what lets ``pacioli_guard`` classify this per-doctype (e.g. ``"Purchase Invoice.submit"``).
        * ``SUBMIT_VIA_CLIENT_RPC`` (Journal Entry, new) — ``POST /api/method/frappe.client.submit``
          with ``{"doc": doc}``. ``doc`` is REQUIRED here (raises if ``None``, never silently falls
          back to the 403ing run_method shape) — frappe's ``frappe.client.submit`` reconstructs
          the document from this body (``frappe.get_doc(doc)``) rather than reading it fresh from
          the DB, so the caller must pass the SAME doc it already validated its plan/closed-books/
          freshness gates against (``tools.py``'s ``_governed_write``). This is body-doctype (the
          doctype lives in ``doc["doctype"]``, not the URL) — safe to enable only because
          ``pacioli_guard.scope.body_scoped_target`` now parses it and enforces the credential's
          per-doctype grant identically to the run_method shape (guard CHANGELOG 0.5.0). Still
          KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (Gate 10, next armed window).
        """
        submit_via = SUPPORTED_DOCTYPES.get(doctype, {}).get("submit_via", SUBMIT_VIA_RUN_METHOD)
        if submit_via == SUBMIT_VIA_CLIENT_RPC:
            if not isinstance(doc, dict) or not doc:
                raise ErpnextError(
                    f"{doctype} submits via frappe.client.submit and requires the already-"
                    "fetched document body — none was supplied")
            payload = self._call("POST", f"/api/method/{_CLIENT_SUBMIT_METHOD}", body={"doc": doc})
            if "message" not in payload:
                raise ErpnextError("frappe.client.submit response has no 'message' envelope")
            return payload["message"]
        payload = self._call("POST", self._doc_path(doctype, name),
                             params={"run_method": "submit"})
        return self._data(payload)

    def cancel_document(self, doctype, name):
        """The UNDO verb.

        * ``SUBMIT_VIA_RUN_METHOD`` (SI/PI/PE, unchanged, live-proven) — the same guard-scopeable
          doc-method shape as submit (classifies per doctype, e.g. ``"Sales Invoice.cancel"``), and
          the same discipline: run_method=cancel and NOTHING else. ERPNext's own cancel-blocks
          (closed period, PCV, freeze date, reconciled links) are relied on downstream and never
          bypassed — the broker refuses first at its own closed-books check anyway.
        * ``SUBMIT_VIA_CLIENT_RPC`` (Journal Entry, new) — ``POST /api/method/frappe.client.cancel``
          with ``{"doctype": doctype, "name": name}``. Unlike submit, NO doc body is needed:
          ``frappe.client.cancel`` loads the document fresh from the DB itself
          (``frappe.get_doc(doctype, name); wrapper.cancel()``) — doctype/name are plain sibling
          params, never nested under a ``doc`` key. Body-doctype, made safe by
          ``pacioli_guard.scope.body_scoped_target`` the same way submit is (see above).
          KNOWLEDGE-PINNED, NOT LIVE-VERIFIED.
        """
        submit_via = SUPPORTED_DOCTYPES.get(doctype, {}).get("submit_via", SUBMIT_VIA_RUN_METHOD)
        if submit_via == SUBMIT_VIA_CLIENT_RPC:
            payload = self._call("POST", f"/api/method/{_CLIENT_CANCEL_METHOD}",
                                 body={"doctype": doctype, "name": name})
            if "message" not in payload:
                raise ErpnextError("frappe.client.cancel response has no 'message' envelope")
            return payload["message"]
        payload = self._call("POST", self._doc_path(doctype, name),
                             params={"run_method": "cancel"})
        return self._data(payload)

    # --- UNDO inputs -----------------------------------------------------------------
    def get_submitted_linked_docs(self, doctype, name):
        """The cancel blast-radius: submitted documents linked to this one (what ERPNext's own
        cancel dialog consults). Returns the ``docs`` list (possibly empty). Raises on an
        unreadable graph — an unverifiable blast radius refuses, it never reads as empty."""
        payload = self._call(
            "POST", "/api/method/frappe.desk.form.linked_with.get_submitted_linked_docs",
            body={"doctype": doctype, "name": name})
        if "message" not in payload:
            raise ErpnextError("linked-docs response has no 'message' envelope")
        message = payload["message"]
        if message is None:
            return []  # frappe's whole-null response — a genuinely empty graph
        # A dict message MUST carry a real `docs` list. A null/absent `docs` (even alongside a
        # non-zero `count`), or a non-list `docs`, is an UNREADABLE graph, never silently a leaf —
        # deny-biased, per the docstring: an unverifiable blast radius refuses.
        if not isinstance(message, dict) or not isinstance(message.get("docs"), list):
            raise ErpnextError("linked-docs 'message' has no readable 'docs' list — "
                               "blast radius unreadable")
        return message["docs"]

    def get_gl_entries(self, voucher_type, voucher_no):
        """The posting's live GL rows — what a cancel unwinds (presented as the plan's projected
        reversal). Reads only uncancelled rows; requires ``GL Entry`` in the credential's
        resource-doctype grant. Filters on BOTH ``voucher_type`` (= the doctype) AND
        ``voucher_no``, not ``voucher_no`` alone — a latent cross-doctype gap this increment
        closes: once Sales Invoice and Purchase Invoice share one GL Entry table, a voucher_no
        collision (however unlikely) would otherwise surface the wrong doctype's rows as this
        cancel's projected reversal. Generalized in the Purchase Invoice breadth increment — was
        ``get_gl_entries(voucher_no)``. The field list carries ``against_voucher_type``/
        ``against_voucher`` (Payment Entry breadth) — a Payment Entry cancel can unwind GL rows
        against N different invoices in one voucher, so the projected reversal needs to say which
        invoice each row is against; both are ordinary GL Entry columns populated for any
        doctype's referencing rows (SI/PI included), so this is a plain field-list addition, not a
        doctype-conditional branch.

        **Row validation (reconciliation-audit residual, 21b7f84 — "2-of-9 field pinning"),
        GL-entries-shaped: same house pattern as :meth:`get_settling_references`/
        :meth:`get_period_locks`.** A non-list body (``"data": null`` and similar) or a non-dict
        row is a structured deny, never handed through to the caller's disclosure loop. Of the 9
        requested fields, ``account``/``debit``/``credit`` are the row's actual accounting content
        — WHICH account, HOW MUCH debited/credited — the substance of the "projected reversal" a
        human consents to (single-op ``plan_cancel``) or a cascade accumulates across nodes
        (``plan_cascade_cancel``); a malformed value there must refuse, never silently reach that
        disclosure (or a future summing consumer) coerced to zero/blank. ``account`` must be a
        non-blank string; ``debit``/``credit`` must be finite non-bool numbers (``math.isfinite``,
        the same NaN-defense :func:`pacioli.reconcile.check_allocation` and
        :mod:`pacioli.consent`/:mod:`pacioli.prove` already apply) — ``0.0`` is the ordinary,
        valid value for a row's unused side, never treated as "missing". The remaining fields
        (``posting_date``, ``against``, ``party_type``, ``party``, ``against_voucher_type``,
        ``against_voucher``) are disclosure-only metadata that are legitimately blank on many real
        rows (a Cash-account row typically carries no party; only a row settling another voucher
        carries an against_voucher at all) — deliberately NOT validated, so a null/absent value
        there is tolerated, not refused."""
        params = {
            "fields": json.dumps(["posting_date", "account", "debit", "credit",
                                  "against", "party_type", "party",
                                  "against_voucher_type", "against_voucher"]),
            "filters": json.dumps([["voucher_type", "=", voucher_type],
                                   ["voucher_no", "=", voucher_no],
                                   ["is_cancelled", "=", 0]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/GL%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "GL Entry list read returned a non-list body; cannot verify the projected "
                "reversal, refusing")
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "GL Entry list row is malformed (not an object); cannot verify the "
                    "projected reversal, refusing")
            account = row.get("account")
            if not isinstance(account, str) or not account.strip():
                raise ErpnextError(
                    f"GL Entry row for {voucher_type} {voucher_no!r} has a malformed/missing "
                    f"account ({account!r}); an unverifiable reversal target refuses, it never "
                    "reads as a valid row")
            for field in ("debit", "credit"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value):
                    raise ErpnextError(
                        f"GL Entry row for {voucher_type} {voucher_no!r}, account {account!r}, "
                        f"has a malformed {field} ({value!r}); an unverifiable reversal amount "
                        "refuses, it never reads as zero")
        return rows

    # --- AMEND (the corrected re-draft) ------------------------------------------------
    def get_doc_for_amend(self, doctype, name):
        """The full source document for :func:`pacioli.amend.amend_payload` — the same
        permission-scoped item GET as the read tier (an item GET returns every field, child
        tables included); named separately so the amend read is explicit in the call trace.
        Generalized in the Purchase Invoice breadth increment — was ``get_doc_for_amend(name)``
        (Sales Invoice only)."""
        return self.get_document(doctype, name)

    def find_amendments(self, doctype, name):
        """Existing amendments of ``name``: documents of ``doctype`` whose ``amended_from``
        points at it, at **any** docstatus — a draft amendment counts, and so do a submitted and
        a cancelled one (deliberately no docstatus filter). Used to refuse a second amend of the
        same cancelled document. Requires no extra grant beyond the doctype's own resource read.
        Generalized in the Purchase Invoice breadth increment — was ``find_amendments(name)``
        (Sales Invoice only)."""
        params = {
            "fields": json.dumps(["name", "docstatus"]),
            "filters": json.dumps([["amended_from", "=", name]]),
            "limit_page_length": "0",
        }
        path = f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
        data = self._data(self._call("GET", path, params=params))
        if not isinstance(data, list):
            # An unreadable amendment search refuses — never reads as "no amendments" (which would
            # let a second amended draft slip past the duplicate-amend refusal). Empty is `[]`.
            raise ErpnextError("amendment search returned no readable list — cannot verify "
                               "existing amendments; refusing")
        return data

    def create_amended_draft(self, doctype, source_doc, seat=None):
        """Insert the amended DRAFT: a resource CREATE (POST to the **collection** URL for
        ``doctype``) carrying exactly the payload :func:`pacioli.amend.amend_payload` builds —
        copy → strip → ``amended_from`` set → insert as docstatus 0, plus the workflow ``seat``
        (the F1 fix — see :func:`pacioli.amend.amend_payload`) when the tool layer computed one.
        The insert is reversible (a draft deletes cleanly), which is why this verb, alone among
        the mutations, carries no consent marker (see :mod:`pacioli.amend`). A payload refusal is
        re-raised as :class:`ErpnextError` so the tool layer's structured-deny guarantee holds
        even without its own gate. Generalized in the Purchase Invoice breadth increment — was
        ``create_amended_draft(source_doc)`` (Sales Invoice's collection URL only)."""
        try:
            body = amend_payload(source_doc, seat=seat)
        except ValueError as exc:
            raise ErpnextError(str(exc)) from exc
        payload = self._call(
            "POST", f"/api/resource/{urllib.parse.quote(doctype, safe='')}", body=body)
        return self._data(payload)

    # --- WORKFLOW (CONSENT's second gate — see pacioli.workflow) ---------------------
    def get_active_workflows(self, doctype):
        """The active Workflow(s) governing ``doctype`` — two-step, because the LIST endpoint
        does not expand child tables: a name-only GET filtered to
        ``[["document_type", "=", doctype], ["is_active", "=", 1]]``, then a full-doc GET per
        name (the pure gate in :mod:`pacioli.workflow` needs the ``states``/``transitions``
        child tables). Returns ``[]`` when nothing is active — never raises for "no workflow".

        Requires a **custom read permission** on the ``Workflow`` DocType for the broker's role:
        by default it is System Manager-only, so the scoped broker credential cannot read it
        until the site's Role Permission Manager grants it — document this as a required
        scoping grant, exactly like the period-lock sources. An unreadable list OR an unreadable
        individual workflow doc both raise :class:`ErpnextError` (deny) — never read as "no
        workflow configured". Knowledge-pinned from frappe source (Workflow DocType JSON), NOT
        live-verified."""
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps([["document_type", "=", doctype], ["is_active", "=", 1]]),
            "limit_page_length": "0",
        }
        path = f"/api/resource/{urllib.parse.quote('Workflow', safe='')}"
        rows = self._data(self._call("GET", path, params=params))
        workflows = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ErpnextError("active-workflow list row is missing a name")
            doc = self._data(self._call("GET", self._doc_path("Workflow", name)))
            # A null/empty/nameless full-doc body must RAISE here, honouring this method's own
            # deny contract — passed downstream, a single malformed body would read as "no
            # workflow" at the gate and silently disable it (the redteam-proven bypass; the pure
            # core's Malformed sentinel is the belt under this suspender).
            if not isinstance(doc, dict) or not doc \
                    or not isinstance(doc.get("name"), str) or not doc["name"].strip():
                raise ErpnextError(f"workflow doc {name!r} unreadable or malformed — refusing")
            workflows.append(doc)
        return workflows

    def get_workflow_state(self, doctype, name, state_field):
        """The document's current workflow-state value, read off ``state_field`` — which comes
        from the governing workflow's own ``workflow_state_field`` (CONFIGURABLE per workflow;
        the caller must never hardcode ``"workflow_state"``). Reuses the same permission-scoped
        item GET the read tier already makes. Raises on an unreadable document (deny); an
        **empty or missing value on a readable document is returned as-is** — frappe lazily
        backfills ``workflow_state`` on next save, so an unset state is a real document shape,
        not a read failure. The pure gate (:func:`pacioli.workflow.check_transition`) is what
        denies on it, not this call."""
        doc = self._data(self._call("GET", self._doc_path(doctype, name)))
        return doc.get(state_field)

    def apply_workflow(self, doctype, name, action):
        """Transport ONLY for a transition :mod:`pacioli.workflow` / ``tools.py`` has already
        classified as non-approving — the classification gate lives there, never here.
        ``POST /api/method/frappe.model.workflow.apply_workflow`` with
        ``{"doc": {"doctype": doctype, "name": name}, "action": action}``. Knowledge-pinned from
        ``frappe/model/workflow.py`` (2026-07-02, NOT live-verified): the server does
        ``frappe.get_doc(parse_json(doc)); doc.load_from_db()``, so only ``doctype``/``name``
        travel in the body — anything else would be misleading, not functional, so we send
        nothing else. Every workflow exception (``WorkflowStateError``/``TransitionError``/
        ``PermissionError``, the plain-``ValidationError`` self-approval block) maps to HTTP 417
        on frappe's side; a bare permission failure can also 403 — both raise
        :class:`ErpnextError` through the usual non-2xx path, never a silent no-op."""
        payload = self._call("POST", "/api/method/frappe.model.workflow.apply_workflow",
                             body={"doc": {"doctype": doctype, "name": name}, "action": action})
        if "message" not in payload:
            raise ErpnextError("apply_workflow response has no 'message' envelope")
        return payload["message"]

    # --- the closed-books inputs -----------------------------------------------------------
    def get_period_locks(self, company, doctype, posting_date):
        """Read the three period-lock boundaries for :func:`pacioli.plan.check_red_line`.
        ``doctype``/``posting_date`` are REQUIRED — no default — so a call site cannot silently
        regress to a doctype-blind read (F-S1; the prior over-refusing shape is retired, not kept
        as a fallback).

        **The frozen-till-date source (v16 spine fix, unchanged by F-S1).** ``Accounts
        Settings.acc_frozen_upto`` was migrated onto ``Company.accounts_frozen_till_date`` in
        ERPNext v16 (confirmed against ``erpnext/patches/v16_0/migrate_account_freezing_settings_
        to_company.py``, which moves the stored value, and against the doctype JSONs: the field is
        absent from ``accounts_settings.json`` on a v16 bench and present on ``company.json``) —
        ``general_ledger.check_freezing_date`` (the real enforcement) reads Company, not Accounts
        Settings. This reads BOTH: the Company doc (the v16 source) and Accounts Settings (the
        legacy v15 field, kept for a bench that hasn't migrated) — if both carry a value, the
        LATER date wins, since either could be the live boundary depending on bench version and
        neither should be silently dropped.

        **The Accounting Period check (F-S1 — doctype- and date-range-aware, the E6 over-refusal
        fixed; F-C1 — the LIST no longer filters ``disabled``, restoring v15 compatibility).**
        Two-scout source read confirmed (``accounting_period.py``, ``general_ledger.py``,
        ``hooks.py``): ERPNext blocks a posting only when its date falls BETWEEN a *specific*
        period's ``start_date``/``end_date`` (both ends inclusive) AND that period is
        ``disabled=0`` AND that period closes the posting's doctype (a ``Closed Document`` child
        row with ``closed=1`` and ``document_type`` equal to the doctype, verbatim). This method
        matches that shape instead of over-refusing every doctype past the latest period's end
        date:

        1. LIST ``Accounting Period`` filtered to ``company`` + a range that CONTAINS
           ``posting_date`` (``start_date <= posting_date <= end_date``) — normally 0 or 1 hits
           (``validate_overlap`` forbids two enabled periods overlapping in one company;
           same-company duplicates are a data-hygiene edge, not a normal shape). **No ``disabled``
           filter here (F-C1)** — ``disabled`` is a v16-only column (``accounting_period.json`` on
           a v15 bench has no such field); filtering a LIST on a column the bench's schema doesn't
           carry is an unknown-column error frappe's filter builder never validates against, which
           would turn this LIST into a hard failure on every v15 bench and refuse every governed
           op via this method's own deny-bias. ``start_date``/``end_date`` exist on both versions
           (confirmed), so the range+company filter is safe on either.
        2. For EACH hit (never just the first — equal-or-stricter than ERPNext's own unordered
           first-row read), a full-document GET (the list endpoint never expands child tables —
           the same two-step :meth:`get_active_workflows` already uses) reads ``disabled`` and
           ``closed_documents``.
        3. If that full document's ``disabled`` is truthy, the period is skipped entirely —
           BEFORE its ``closed_documents`` rows are inspected — a disabled period locks nothing
           (the F-S1/PHASE-T semantics, unchanged, just read from the item GET instead of the LIST
           filter now). If ``disabled`` is **absent** from the full document (the v15 shape — v15
           has no period-disable concept at all) it is treated as enabled, which is the CORRECT
           v15 behavior, not a fallback guess.
        4. For each hit that survives step 3, a row ``closed=1`` (or truthy-``1``) with
           ``document_type`` equal to ``doctype`` (verbatim string) sets
           ``locks["closed_period_until"]`` to THAT period's ``end_date``; otherwise the key is
           simply absent.

        **Deny-bias, unchanged in spirit, extended in surface:** an unreadable LIST or unreadable
        item GET raises (never "assume open"). A malformed period or child row — a non-ISO
        ``start_date``/``end_date`` on a hit, a ``closed_documents`` row missing
        ``document_type``, a ``closed`` value that isn't a clean ``0``/``1``/``bool``, or a
        ``disabled`` value that is PRESENT but isn't a clean ``0``/``1``/``bool``/``None`` —
        raises too: an unverifiable lock must refuse, the same class as unreadable, never silently
        skipped. (Judgment call, flagged for redteam: a malformed-but-present ``disabled`` raises
        rather than being coerced either way — coercing it to falsy/enabled risks the dangerous
        direction, a locked period silently read as open, on nothing more than a guess about what
        the bench meant by that value; raising keeps the same "can't verify, refuse" discipline
        every other field on this row already gets, at the cost of one more refusal case that has
        never been observed on a real bench.) ``posting_date`` itself is validated ISO **before**
        any network call is made (a malformed date refuses immediately, the same discipline
        :func:`pacioli.plan.check_red_line` already applies downstream — this just refuses to
        spend a round-trip building a query on a date that check would refuse anyway).

        **Deliberately NOT modeled: ``exempted_role``.** A per-period role (first matched row)
        that lets a *raw* ERPNext seat holding it bypass the period lock entirely. This broker
        does not inherit that bypass — a seat holding the exempted role could act directly against
        the bench and succeed where this broker refuses. Equal-or-stricter than ERPNext, disclosed
        here rather than silently narrower; there is no ``frappe.flags`` bypass to model, amend
        gets no exemption, and ``from_repost`` is still checked by ERPNext's own path (a real
        ERPNext quirk, not this broker's concern).

        **Cancel parity.** ERPNext blocks CANCELLING into a closed period too, via
        ``general_ledger.make_reverse_gl_entries`` (the GL-level check re-runs on cancel, unlike
        the doc-level ``validate_accounting_period_on_doc_save`` hook which does not fire on
        cancel) — this broker's uniform submit+cancel closed-books gate already matches that,
        calling this same method for both operations.

        Absent locks are simply absent from the dict (never empty strings)."""
        if not _is_iso_date(posting_date):
            raise ErpnextError(
                f"posting_date {posting_date!r} is not a valid ISO (YYYY-MM-DD) date; refusing "
                "to build a period-lock query on an unverifiable date")
        locks = {}
        company_doc = self._data(self._call("GET", self._doc_path("Company", company)))
        frozen_dates = []
        company_frozen = company_doc.get("accounts_frozen_till_date")
        if isinstance(company_frozen, str) and company_frozen.strip():
            frozen_dates.append(company_frozen.strip())

        settings = self._data(self._call(
            "GET", "/api/resource/Accounts%20Settings/Accounts%20Settings"))
        legacy_frozen = settings.get("acc_frozen_upto")
        if isinstance(legacy_frozen, str) and legacy_frozen.strip():
            frozen_dates.append(legacy_frozen.strip())

        if frozen_dates:
            # ISO YYYY-MM-DD strings — lexicographic max is chronological max (the same invariant
            # pacioli.plan's date-range checks already rely on) — honor the LATER of the two
            # sources rather than preferring one over the other.
            locks["frozen_until"] = max(frozen_dates)

        pcv = self._data(self._call(
            "GET", "/api/resource/Period%20Closing%20Voucher",
            params={"fields": json.dumps(["period_end_date"]),
                    "filters": json.dumps([["company", "=", company], ["docstatus", "=", 1]]),
                    "order_by": "period_end_date desc", "limit_page_length": "1"}))
        if pcv and pcv[0].get("period_end_date"):
            locks["pcv_until"] = str(pcv[0]["period_end_date"])

        # F-S1: doctype- and date-range-aware Accounting Period check (see docstring above). The
        # LIST filter does the coarse work (company, containing range) — a bench that filters
        # correctly can never hand back a different company's period or one that doesn't even
        # contain posting_date, so nothing further needs re-checking client-side for those two
        # dimensions. What the LIST endpoint can NOT hand back is `disabled` scoped correctly
        # across versions or the closed_documents child table (frappe never expands child tables
        # on a list read), hence the per-hit item GET below reads both.
        #
        # F-C1: deliberately NO `disabled` filter here. `disabled` is v16-only on `Accounting
        # Period` (absent from a v15 bench's schema) and frappe's filter builder has no
        # meta-validation/sanitizer (`frappe/model/db_query.py::build_filter_conditions` ->
        # `prepare_filter_condition`) — filtering on a column that doesn't exist is an
        # unknown-column failure, not "no match", which would turn this LIST into a hard error on
        # every v15 bench and (via this method's own deny-bias) refuse every governed op there.
        # `company`/`start_date`/`end_date` are confirmed present on both v15 and v16, so they stay.
        periods = self._data(self._call(
            "GET", "/api/resource/Accounting%20Period",
            params={"fields": json.dumps(["name", "start_date", "end_date"]),
                    "filters": json.dumps([["company", "=", company],
                                           ["start_date", "<=", posting_date],
                                           ["end_date", ">=", posting_date]]),
                    "limit_page_length": "0"}))
        if not isinstance(periods, list):
            # A LIST body whose "data" is present but not a list (e.g. null) is as unverifiable
            # as an unreadable one — the structured deny, not a bare TypeError out of the loop.
            raise ErpnextError(
                "Accounting Period list read returned a non-list body; "
                "cannot verify the closed-period lock, refusing")
        matched_end_date = None
        for row in periods:
            if not isinstance(row, dict):
                raise ErpnextError("Accounting Period list row is malformed (not an object)")
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ErpnextError("Accounting Period list row is missing a name")
            start_date, end_date = row.get("start_date"), row.get("end_date")
            if not _is_iso_date(start_date) or not _is_iso_date(end_date):
                raise ErpnextError(
                    f"Accounting Period {name!r} has a malformed start_date/end_date "
                    f"({start_date!r}, {end_date!r}); refusing rather than trust an unverifiable "
                    "period boundary")
            # Full-doc GET, never re-derivable from the list row — the ONLY way to read
            # closed_documents (module docstring / get_active_workflows precedent).
            full = self._data(self._call("GET", self._doc_path("Accounting Period", name)))
            if not isinstance(full, dict) or not full:
                raise ErpnextError(f"Accounting Period {name!r} unreadable or malformed — refusing")
            # F-C1: `disabled` is read here, off the full document, not off the LIST filter (see
            # the LIST comment above). Absent (v15 — the column doesn't exist there) is treated as
            # enabled, which is the correct v15 behavior, not a guess. Present-but-not-a-clean
            # 0/1/bool is a malformed value — raise rather than coerce it either direction (a
            # judgment call: coercing toward "enabled" is safe, but coercing toward "disabled"
            # risks silently unlocking a period that closed_documents would otherwise have
            # refused, so an unparseable value gets the same "can't verify, refuse" treatment
            # every other field on this row already gets).
            disabled_raw = full.get("disabled")
            if disabled_raw not in (0, 1, True, False, None):
                raise ErpnextError(
                    f"Accounting Period {name!r} has an unparseable disabled value "
                    f"{disabled_raw!r}; refusing rather than guess whether the period is enabled")
            if disabled_raw:
                # Validated above to be one of 0/1/True/False/None — truthy here means 1 or True.
                # A disabled period locks nothing (F-S1/PHASE-T semantics) — skip it before ever
                # inspecting closed_documents; what it would otherwise close is irrelevant.
                continue
            closed_rows = full.get("closed_documents")
            if closed_rows is None:
                closed_rows = []
            if not isinstance(closed_rows, list):
                raise ErpnextError(
                    f"Accounting Period {name!r} has a malformed closed_documents child table; "
                    "refusing")
            for child in closed_rows:
                if not isinstance(child, dict) or "document_type" not in child:
                    raise ErpnextError(
                        f"Accounting Period {name!r} has a closed_documents row missing "
                        "document_type; refusing")
                closed_raw = child.get("closed")
                if closed_raw not in (0, 1, True, False):
                    raise ErpnextError(
                        f"Accounting Period {name!r} has a closed_documents row with an "
                        f"unparseable closed value {closed_raw!r}; refusing")
                if closed_raw and child.get("document_type") == doctype and matched_end_date is None:
                    # First match wins (same-company overlapping-and-both-closing-this-doctype is
                    # the data-hygiene edge the pin sheet names, not a shape this client need
                    # arbitrate further) — but keep validating every remaining row/period below;
                    # a match found here must never short-circuit validation of the rest.
                    matched_end_date = str(end_date)
        if matched_end_date is not None:
            locks["closed_period_until"] = matched_end_date
        return locks

    def get_accounts_settings(self, fields):
        """Read named ``fields`` off the site's single ``Accounts Settings`` doctype — a small,
        doctype-blind primitive (names no ERPNext DocType beyond Accounts Settings itself), added
        for the Journal Entry breadth increment's ``plan_cancel`` disclosure (``tools.py``): whether
        ``unlink_payment_on_cancellation_of_invoice`` is on changes a JE cancel's blast radius from
        "refused by the generic backlink check" to "a silent raw-SQL unlink of other submitted
        Journal Entries/Payment Entries that reference this one" (scout-je.md §2, §5). Reusable by
        any future disclosure that needs another Accounts Settings field — it takes the field list
        as a parameter rather than hardcoding one.

        Raises on an unreadable response (the same as every other read in this client) — whether
        that should refuse the caller's plan or be read as "flag unknown" is the CALLER's decision,
        made at the tool layer, not here."""
        payload = self._call(
            "GET", "/api/resource/Accounts%20Settings/Accounts%20Settings",
            params={"fields": json.dumps(list(fields))})
        return self._data(payload)

    # --- RECONCILE (F-R2: govern Payment Reconciliation) -----------------------------
    def reconcile(self, company, party_type, party, receivable_payable_account, allocations):
        """The single governed reconcile call — settles a pinned allocation set of
        payments/Journal Entries against invoices. ``docs/plans/2026-07-09-fr2-govern-
        reconciliation.md``: ``POST /api/method/run_doc_method`` with a client-constructed
        ``Payment Reconciliation`` doc body and ``method=reconcile``
        (``frappe/handler.py:272-311``; ``payment_reconciliation.js:386-394``) — the doctype's
        ``load_from_db`` stub is NOT in the write path, so the FULL doc travels in the request,
        never re-fetched server-side.

        ``allocations`` is the caller-facing row shape (mirroring ``pacioli.reconcile``'s pinned
        graph/``rows`` SEMANTIC keys: ``payment_type``/``payment_no``/``invoice_type``/
        ``invoice_no``/``allocated_amount``/``payment_unallocated``/``invoice_outstanding`` per
        row) — this method is the ONLY place those get translated into ERPNext's own wire field
        names, so a semantics swap cannot happen anywhere else.

        WIRE SHAPE — LIVE-VERIFIED (P7, 2026-07-09, real Frappe v16 sealed-lab bench; both former
        BENCH-PENDING questions answered by reproduction):

          1. The ``invoices[]`` pool IS REQUIRED. ``validate_allocation`` builds its per-invoice
             outstanding map from ``self.get("invoices")`` — with the pool absent,
             ``invoice_outstanding`` is None and the ceiling check TypeErrors (HTTP 500, the exact
             refusal 0.13.0's allocation-only shape got live). One pool row per UNIQUE invoice:
             ``{invoice_type, invoice_number, outstanding_amount}``. A ``payments[]`` pool is NOT
             read on this path and is deliberately not sent (nothing untested rides the wire).
          2. The allocation row's ``amount`` AND ``unreconciled_amount`` are BOTH the PAYMENT's
             unallocated. ``validate_allocation`` reads ``row.amount`` (the payment's available;
             unset -> 0 -> throws on row 1) and ``check_if_advance_entry_modified`` compares
             ``row.unreconciled_amount`` to the PE's LIVE ``unallocated_amount`` (the
             no-``voucher_detail_no`` branch, ``utils.py:645-647``) — 0.13.0 sent the invoice's
             outstanding there and the live bench refused: "Payment Entry has been modified after
             you pulled it". Entries are processed GROUPED per voucher with every check before the
             group's single ``save()`` (``reconcile_against_document``), so every row carries the
             plain pre-write value — no running decrement for multi-row-same-payment sets.

        The caller (``tools.py``'s ``_tool_reconcile``) is what guarantees ``allocations`` here is
        built from the PINNED plan graph alone, never forwarded from any other argument — this
        method has no opinion on where its argument came from, it only shapes the wire call.
        Duck-typed return (``message`` envelope if present, else the raw payload): unlike
        ``apply_workflow``, this does NOT raise on a missing ``message`` key — the response
        envelope shape is itself BENCH-PENDING (see above), so asserting one here would pin an
        unverified assumption. ``_call`` already raises :class:`ErpnextError`
        (``answered=...``, the transport taxonomy) on any non-2xx response; the result this
        returns is never trusted as proof of effect by the caller either way — the caller's own
        readback is (``pacioli.reconcile``'s module docstring)."""
        invoices_pool = {}
        for a in allocations:
            key = (a["invoice_type"], a["invoice_no"])
            invoices_pool.setdefault(key, a["invoice_outstanding"])
        pr = {"doctype": "Payment Reconciliation", "company": company, "party_type": party_type,
             "party": party, "receivable_payable_account": receivable_payable_account,
             "invoices": [
                 {"invoice_type": dt, "invoice_number": no, "outstanding_amount": out}
                 for (dt, no), out in invoices_pool.items()
             ],
             "allocation": [
                 {"invoice_type": a["invoice_type"], "invoice_number": a["invoice_no"],
                  "reference_type": a["payment_type"], "reference_name": a["payment_no"],
                  "allocated_amount": a["allocated_amount"], "amount": a["payment_unallocated"],
                  "unreconciled_amount": a["payment_unallocated"]}
                 for a in allocations
             ]}
        payload = self._call("POST", "/api/method/run_doc_method",
                             body={"docs": json.dumps(pr), "method": "reconcile"})
        return payload.get("message", payload) if isinstance(payload, dict) else payload

    def get_settling_references(self, doctype, name):
        """F-R1 — the settling-PE disclosure read. **Payment Ledger Entry** is the settlement
        ledger since ERPNext v14 (``update_voucher_outstanding`` reads it), and it is doctype-blind
        by construction: filtering on ``against_voucher_type``/``against_voucher_no`` surfaces
        whatever settles the target document, not just a Payment Entry — honest against
        ``auto_cancel_exempted_doctypes`` hook extensions (currently just Payment Entry in stock
        ERPNext, but the union is per-installed-app and this read never assumes the list stays
        that short).

        GL-entries-shaped (:meth:`get_gl_entries` is the template): explicit ``fields``
        (``voucher_type``/``voucher_no``/``amount``/``account_currency``), explicit ``filters``
        (``against_voucher_type``, ``against_voucher_no``, ``delinked=0`` — a delinked row is
        already severed, nothing left to disclose — and ``voucher_no != name``, which excludes the
        target document's own self-referencing rows once it is itself unlinked/unallocated),
        ``limit_page_length: "0"`` (F-V1 law: never a silent partial page).

        **Structured deny on a non-list body** — the same house pattern
        :meth:`get_period_locks` already applies to its Accounting Period LIST read: a
        ``"data": null`` (or otherwise non-list) body is valid JSON the transport layer accepts,
        but is as unverifiable as an unreadable response — raising here, rather than handing a
        non-list through to the caller's per-row disclosure loop, keeps this a structured deny
        instead of a bare ``TypeError``/silent empty read.

        Raises on an unreadable response too (the same as every other read in this client) — the
        CALLER (``tools.py``) decides this refuses the whole plan (deny-biased, pin sheet), not
        this client."""
        params = {
            "fields": json.dumps(["voucher_type", "voucher_no", "amount", "account_currency"]),
            "filters": json.dumps([["against_voucher_type", "=", doctype],
                                   ["against_voucher_no", "=", name],
                                   ["delinked", "=", 0],
                                   ["voucher_no", "!=", name]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/Payment%20Ledger%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "Payment Ledger Entry list read returned a non-list body; cannot verify settling "
                "references, refusing")
        for row in rows:
            # Row-shape guard, same as get_period_locks' per-row validation (redteam catch): a
            # malformed row must be a structured deny, never an AttributeError out of the
            # disclosure loop.
            if not isinstance(row, dict):
                raise ErpnextError(
                    "Payment Ledger Entry list row is malformed (not an object); cannot verify "
                    "settling references, refusing")
        return rows

    # --- THE CLOSE, HALF 2 (the Reconciliation) — period-sweep reads ------------------
    def sweep_gl_entries(self, company, since, until):
        """Fork I — the CREATION-window movement sweep.

        Unlike :meth:`get_gl_entries` (voucher-scoped: filtered on ``voucher_type``/``voucher_no``
        for one document's projected reversal), this reads EVERY GL Entry row **written** for
        ``company`` inside ``[since, until]`` — axis ``creation``, deliberately NOT
        ``posting_date``. A row can carry any ``posting_date`` (backdated, corrected, whatever the
        business needs) while ``creation`` pins exactly when it actually landed on the bench; a
        reconciliation sweep organized around when work happened, not what date it claims, has to
        read the axis that can't be backdated. ``since``/``until`` are ERPNext-clock frappe-format
        datetime strings (``YYYY-MM-DD HH:MM:SS[.ffffff]``) supplied by the caller — this method
        invents, defaults, or reformats no clock; that is the glue layer's job, not this client's.

        GL-entries-shaped (:meth:`get_gl_entries` is the template): explicit ``fields``/
        ``filters`` JSON, ``limit_page_length: "0"`` (F-V1 law — a gate-feeding LIST read must pin
        the full page, never rely on frappe's default-20 truncation), structured-deny-on-non-list-
        body before any caller loop ever sees a row.

        **Row validation — wider than get_gl_entries' because this feeds classification, not just
        disclosure:**

        * ``account`` — non-blank str (else raise): the accounting content, same as get_gl_entries.
        * ``debit``/``credit`` — finite non-bool number (``math.isfinite``, the same NaN-defense
          get_gl_entries/check_allocation/consent/prove already apply): a malformed amount must
          refuse, never read as zero.
        * ``is_cancelled`` — a clean ``int`` (``0``/``1``), never a bool, never absent (else
          raise): the governed/cancel classification downstream keys off this value directly — a
          malformed one silently read as ``0`` would misclassify a cancelled row as live.
        * ``voucher_type``/``voucher_no`` — non-blank str (else raise): the grouping key the sweep
          is organized around; a blank one is an unusable movement record.
        * ``creation``/``owner`` — non-blank str (else raise): the join anchors — back to the
          sweep window, and to who wrote the row; blank is unverifiable.
        * ``posting_date``/``modified``/``modified_by``/``party_type``/``party`` —
          disclosure-only, legitimately blank on real rows (a Cash-account row typically carries
          no party) — deliberately NOT validated; a null/absent value here is tolerated.

        Raises on an unreadable response, same as every read in this client — the CALLER decides
        what an unreadable sweep means for the Close, not this method.

        KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (this module's standing residual, SPEC §7): the
        ``creation`` filter axis and full field list are confirmed against GL Entry's own
        universal fields (``creation``/``owner``/``modified``/``modified_by`` on every doctype;
        ``is_cancelled``/``party_type``/``party`` already proven live via get_gl_entries/
        get_settling_references' siblings) — not against a live creation-window sweep response."""
        params = {
            "fields": json.dumps(["voucher_type", "voucher_no", "account", "debit", "credit",
                                  "posting_date", "creation", "owner", "modified", "modified_by",
                                  "is_cancelled", "party_type", "party"]),
            "filters": json.dumps([["company", "=", company],
                                   ["creation", ">=", since],
                                   ["creation", "<=", until]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/GL%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "GL Entry creation-window sweep returned a non-list body; cannot verify the "
                "sweep, refusing")
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "GL Entry sweep row is malformed (not an object); cannot verify the sweep, "
                    "refusing")
            account = row.get("account")
            if not isinstance(account, str) or not account.strip():
                raise ErpnextError(
                    f"GL Entry sweep row has a malformed/missing account ({account!r}); an "
                    "unverifiable row refuses, it never reads as valid")
            for field in ("debit", "credit"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value):
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed {field} "
                        f"({value!r}); an unverifiable amount refuses, it never reads as zero")
            is_cancelled = row.get("is_cancelled")
            if not isinstance(is_cancelled, int) or isinstance(is_cancelled, bool) \
                    or is_cancelled not in (0, 1):
                raise ErpnextError(
                    f"GL Entry sweep row, account {account!r}, has a malformed is_cancelled "
                    f"({is_cancelled!r}); a malformed value must never read as 0 (live), refusing")
            for field in ("voucher_type", "voucher_no"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed/missing "
                        f"{field} ({value!r}); the grouping key must be usable, refusing")
            for field in ("creation", "owner"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed/missing "
                        f"{field} ({value!r}); the join anchor must be verifiable, refusing")
        return rows

    def get_reposts(self, company, since, until):
        """Fork II — the Repost Accounting Ledger read. Explains a Fork-IV second generation: a
        repost re-derives a document's GL rows in place (same ``voucher_no``, freshly-written GL
        rows) — this is how the sweep's glue tells "the bench silently regenerated this voucher's
        ledger" apart from "a brand-new voucher landed in the window".

        Two-step, LIST then per-doc GET — the same shape :meth:`get_period_locks`/
        :meth:`get_active_workflows` already use, because the LIST endpoint never expands child
        tables:

        1. LIST ``/api/resource/Repost%20Accounting%20Ledger`` filtered to ``company`` + the
           creation window (``since``/``until`` — the same ERPNext-clock frappe-format datetime
           strings as :meth:`sweep_gl_entries`; this method invents no clock either), fields
           ``["name", "owner", "creation", "docstatus"]``, ``limit_page_length: "0"`` (F-V1 law),
           structured-deny-on-non-list-body, per-row validation (``name``/``owner``/``creation``
           non-blank str, ``docstatus`` a clean int — else raise) before any per-doc GET is issued.
        2. For each surviving hit, a full-document GET (:meth:`get_document`, reused rather than
           reimplemented) reads the ``vouchers`` child table: each child's ``voucher_type``/
           ``voucher_no`` non-blank str, else raise. A repost doc with no ``vouchers`` key or an
           empty list simply names no vouchers — ``[]``, not an error (a repost that touched
           nothing is a real, valid shape).

        **This read may be permission-locked** (Repost Accounting Ledger can be SysMgr-gated on a
        real bench) — this method itself just raises honestly on an unreadable/403 response, like
        every read in this client. Whether an unreadable repost read should be a non-fatal FLAG
        (the reconciliation still runs, just without repost attribution) or a whole-plan refusal
        is the CALLER's decision, made at the glue layer — never here.

        Returns ``[{"name", "owner", "creation", "docstatus",
        "vouchers": [{"voucher_type", "voucher_no"}, ...]}, ...]`` — each voucher dict carries
        only those two keys, never the raw child row's frappe bookkeeping fields (``idx``/
        ``parent``/``parenttype``/``parentfield``/the child row's own ``name``), which are not
        this read's concern.

        KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (this module's standing residual, SPEC §7): Repost
        Accounting Ledger's ``vouchers`` child table shape (``voucher_type``/``voucher_no`` per
        row) is confirmed against ERPNext source, not against a live bench response."""
        params = {
            "fields": json.dumps(["name", "owner", "creation", "docstatus"]),
            "filters": json.dumps([["company", "=", company],
                                   ["creation", ">=", since],
                                   ["creation", "<=", until]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call(
            "GET", "/api/resource/Repost%20Accounting%20Ledger", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "Repost Accounting Ledger list read returned a non-list body; cannot verify "
                "reposts, refusing")
        reposts = []
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "Repost Accounting Ledger list row is malformed (not an object); cannot "
                    "verify reposts, refusing")
            for field in ("name", "owner", "creation"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger list row has a malformed/missing {field} "
                        f"({value!r}); refusing")
            docstatus = row.get("docstatus")
            if not isinstance(docstatus, int) or isinstance(docstatus, bool):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} has a malformed docstatus "
                    f"({docstatus!r}); refusing")
            full = self.get_document("Repost Accounting Ledger", row["name"])
            if not isinstance(full, dict):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} unreadable or malformed — "
                    "refusing")
            voucher_rows = full.get("vouchers")
            if voucher_rows is None:
                voucher_rows = []
            if not isinstance(voucher_rows, list):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} has a malformed vouchers child "
                    "table; refusing")
            vouchers = []
            for child in voucher_rows:
                if not isinstance(child, dict):
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a malformed vouchers "
                        "child row (not an object); refusing")
                voucher_type = child.get("voucher_type")
                voucher_no = child.get("voucher_no")
                if not isinstance(voucher_type, str) or not voucher_type.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a vouchers child row "
                        f"with a malformed/missing voucher_type ({voucher_type!r}); refusing")
                if not isinstance(voucher_no, str) or not voucher_no.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a vouchers child row "
                        f"with a malformed/missing voucher_no ({voucher_no!r}); refusing")
                vouchers.append({"voucher_type": voucher_type, "voucher_no": voucher_no})
            reposts.append({"name": row["name"], "owner": row["owner"],
                            "creation": row["creation"], "docstatus": docstatus,
                            "vouchers": vouchers})
        return reposts
