# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — DOCTOR: the inventory (read-only config & readiness checks; glue, stdlib-only).

Pacioli starts the merchant with a complete inventory before a single entry — survey what you
hold before the books open. ``pacioli doctor`` answers one question before the first real call: *is this install actually
ready, and ready as the right principal?* It exists because credential resolution is deferred to
call time (deliberately — nothing secret is cached), which means the purely-local commands
(``verify``/``orphans``/``mint``) succeed on a half-configured install and the first bench call is
where a missing secret would otherwise surface.

Rules of the house apply throughout:

* **Read-only, no side effects.** Doctor never mints a seal key, never creates a state db, never
  writes a receipt. Where first use *would* create something, it says so instead.
* **Never echoes a secret.** Credential checks report *that* a reference resolves, never what it
  resolves to.
* **Deny-biased.** Anything doctor cannot verify is reported as a failure, not assumed fine. The
  one deliberate inversion: a **403 on the probe is a PASS** — it proves the bench answered and the
  credential authenticated but is scoped tighter than the probe method, which is exactly the
  posture this broker prescribes.
* **Administrator is a FAILURE, not a warning.** A broker credential that authenticates as
  Administrator (or any bench-superuser) voids the whole trust spine (SPEC §2) — doctor refuses to
  call that install ready.

Human-side only, on purpose: doctor is a CLI command, not an MCP tool. The agent surface stays
the six slice-one tools; an agent has no business enumerating the operator's config health.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from pacioli.erpnext import SUPPORTED_DOCTYPES, default_transport
from pacioli.registry import RegistryError, _resolve_ref, load_registry
from pacioli.runtime import _SEAL_KEY_BYTES, _seal_key_path, state_db_path

# COUPLED to pacioli-guard's SAFE_METHODS (guard/pacioli_guard/scope.py) — as of guard 0.6.0
# deny-unknown, a bare /api/method/<name> call fires a `methods` grant ONLY if the exact name is in
# that frozenset. This probe is a bare call, and `probe_bench` reads BOTH outcomes as signal:
# 403 = PASS (scoped tighter than the probe), 200 = authenticated-as-<user> (+ a WARN that the
# probe is in scope). The 200 leg is only reachable because this exact name IS in SAFE_METHODS;
# swap this method OR drop it from SAFE_METHODS and a credential that genuinely grants it would
# 403 anyway — doctor would then misreport "scoped tighter" for a credential that isn't. Change
# the two together or not at all.
PROBE_METHOD = "frappe.auth.get_logged_user"

# probe_roles (the tight-role seat): the seat reads its OWN role list via frappe's v2
# doctype-resolved method route. A seat holding a SPINE-VOIDING role can administer the very
# governance it operates under — over frappe's OWN REST surface a System Manager can write Custom
# DocPerm rows (grant itself any permission), mint API keys (frappe.core...user.generate_keys,
# only_for "System Manager"), and — if server scripts are enabled — execute arbitrary server-side
# code via the System Console (execute_code, only_for System Manager/Administrator). So doctor
# refuses it, the same doctrine as "Administrator is a failure" (module docstring) extended from
# the literal username to the administrative *role*. Frappe's own permission evaluator hardcodes a
# blanket bypass ONLY for the username "Administrator", never for the "System Manager" role
# (frappe/permissions.py) — so this is a least-privilege / blast-radius refusal, not a
# permission-bypass one. OVER-BROAD roles (Accounts Manager) grant more than the broker uses (it
# never deletes, never closes a period) but cannot administer the governance → a WARN, not a
# refusal. The frappe-auto roles are appended to every authenticated user and carry no privilege →
# ignored. The v2 route is deliberate: the bare dotted `frappe.core.doctype.user.user.get_roles`
# is guard-blocked (not a SAFE_METHOD), but `/api/v2/method/User/get_roles` is doctype-resolved, so
# a plain `User.get_roles` methods-grant admits it with NO guard code change (guard scope.py).
#
# KNOWN RESIDUALS (documented, not silently swept — house style):
#  * name-based, not permission-based. The refusal matches role NAMES (_SPINE_VOIDING_ROLES); a
#    custom role cloned from System Manager's DocPerm rows under a different name ("Ops Admin")
#    carries equal blast-radius and would read as clean. Creating such a role itself requires
#    pre-existing System-Manager access (chicken-and-egg — unreachable from the tight seat), so
#    this is config-drift risk, not a seat-reachable bypass. A permission-based check (read each
#    role's DocPerm rows) is a larger read surface + grant — its own increment.
#  * the `User.get_roles` grant is broader than "read own roles": frappe's get_roles honors a
#    `?uid=<user>` param with no permission check, so the grant also lets the credential enumerate
#    ANY user's roles (read-only, no mutation — recon, not escalation). doctor never sends `uid`.
#    Ignoring `uid` bench-side is a guard code change — a separate hardening increment.
ROLES_PROBE_PATH = "/api/v2/method/User/get_roles"
_SPINE_VOIDING_ROLES = ("System Manager", "Administrator")
_OVER_BROAD_ROLES = ("Accounts Manager",)
_FRAPPE_AUTO_ROLES = frozenset({"All", "Guest", "Desk User"})

OK, WARN, FAIL = "ok", "warn", "fail"
_PREFIX = {OK: "  ok ", WARN: "  !! ", FAIL: "  XX "}


def _finding(level, message):
    return (level, message)


def check_registry(env):
    """Load and validate the registry. Returns ``(findings, registry_or_None)``."""
    reg_path = (env or {}).get("PACIOLI_REGISTRY")
    if not reg_path:
        return [_finding(FAIL, "PACIOLI_REGISTRY is not set")], None
    if not Path(reg_path).exists():
        return [_finding(FAIL, f"registry file not found: {reg_path}")], None
    try:
        registry = load_registry(path=reg_path)
    except RegistryError as exc:
        return [_finding(FAIL, f"registry {reg_path}: {exc}")], None
    names = registry.names()
    try:
        default = registry.get(None).name
    except RegistryError:
        default = None
    labelled = ", ".join(f"{n}*" if n == default else n for n in names)
    findings = [_finding(OK, f"registry: {reg_path} — {len(names)} target(s): {labelled}")]
    if default is None and len(names) > 1:
        findings.append(_finding(WARN, "no unambiguous default target — every call must pass "
                                       "pacioli_target= explicitly"))
    return findings, registry


def check_credentials(target, env, read_file):
    """Report whether the target's credential references resolve. Values are never shown."""
    findings = []
    for what, ref in (("api_key", target.api_key), ("api_secret", target.api_secret)):
        try:
            _resolve_ref(ref, f"target {target.name!r} {what}", env, read_file)
        except RegistryError as exc:
            findings.append(_finding(FAIL, str(exc)))
        else:
            how = ref.split(":", 1)[0] if ref.startswith(("env:", "file:")) else "inline"
            findings.append(_finding(OK, f"{what}: {how} — resolves (not shown)"))
    return findings


def check_state(env, target_name):
    """Report on the state dir, the target's db, and the seal key — creating NONE of them."""
    findings = []
    state_dir = (env or {}).get("PACIOLI_STATE_DIR")
    if not state_dir:
        return [_finding(FAIL, "PACIOLI_STATE_DIR is not set")]
    if not Path(state_dir).is_dir():
        findings.append(_finding(WARN, f"state dir {state_dir} does not exist yet — "
                                       "created on first use"))
    db = state_db_path(state_dir, target_name)
    findings.append(_finding(OK, f"state db: {db} — exists")
                    if db.exists() else
                    _finding(OK, f"state db: {db} — will be created on first use"))
    key_path = _seal_key_path(env)
    if not key_path.exists():
        findings.append(_finding(OK, f"seal key: {key_path} — will be minted 0600 on first use"))
        return findings
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        findings.append(_finding(FAIL, f"seal key {key_path} has permissions {oct(mode)}; must be "
                                       f"0600 — run: chmod 600 {key_path}"))
    elif key_path.stat().st_size < _SEAL_KEY_BYTES:
        findings.append(_finding(FAIL, f"seal key {key_path} is shorter than {_SEAL_KEY_BYTES} "
                                       "bytes — investigate, do not just regenerate"))
    else:
        findings.append(_finding(OK, f"seal key: {key_path} — 0600, present"))
    return findings


def probe_bench(target, env, read_file, transport):
    """One read-only GET as the target's credential; classify what came back.

    * 200 + a username → authenticated; **Administrator/Guest are failures** (superuser voids the
      spine; Guest means the credential did not actually authenticate).
    * 403 → PASS: reachable, authenticated, and scoped tighter than the probe — the prescribed
      posture (the probe method simply isn't in the allowlist).
    * 401 → the bench rejected the credential.
    * anything else / unreachable → failure, stated plainly.
    """
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "bench probe skipped — credential does not resolve (above)")]
    url = f"{target.base_url}/api/method/{PROBE_METHOD}"
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"})
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"bench unreachable: {exc}")]
    if status == 403:
        return [_finding(OK, "bench: reachable — credential authenticated and is scoped tighter "
                             "than the probe method (the prescribed posture)")]
    if status == 401:
        return [_finding(FAIL, "bench: reachable but the credential was rejected (401)")]
    if status == 200 and isinstance(payload, dict):
        user = str(payload.get("message") or "")
        if user == "Guest":
            return [_finding(FAIL, "bench: reachable but the credential did not authenticate "
                                   "(logged in as Guest)")]
        if user == "Administrator":
            return [_finding(FAIL, "bench: authenticated as Administrator — a superuser broker "
                                   "credential voids the trust spine (SPEC §2); use a scoped "
                                   "non-Administrator user")]
        if user:
            return [_finding(OK, f"bench: reachable — authenticated as {user}"),
                    _finding(WARN, f"{PROBE_METHOD} is in this credential's scope; harmless, but "
                                   "slice-one does not need it")]
    return [_finding(FAIL, f"bench: unexpected response to the probe (HTTP {status})")]


def probe_company_read(target, env, read_file, transport):
    """One read-only GET of the Company DocType — required since ``get_period_locks`` now reads
    ``Company.accounts_frozen_till_date`` as the v16 source for the frozen-books boundary of the closed books
    (``pacioli/erpnext.py``'s "Spine fix" — ERPNext v16 migrated the field off Accounts Settings).

    **The same deliberate inversion as** :func:`probe_workflow_read`, **not** :func:`probe_bench`:
    a **403 here is a FAIL**, because the closed-books check REQUIRES this read — an unreadable
    Company doc makes ``get_period_locks`` raise, which denies every plan/submit/cancel on this
    target, workflow-governed or not. Readable-but-empty still PASSES (a credential can legally
    see zero Company rows under row-level permission restrictions and still have the DocType-level
    read grant the lock needs). Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "Company read probe skipped — credential does not resolve (above)")]
    url = f"{target.base_url}/api/resource/Company"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"Company read probe: bench unreachable: {exc}")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [_finding(OK, "Company read: readable — required for the v16 frozen-books period "
                             "lock (Company.accounts_frozen_till_date)")]
    if status == 403:
        return [_finding(FAIL, "Company read (403): the credential cannot read the Company "
                              "DocType — grant read permission on Company (Role Permission "
                              "Manager); required to reconstruct the v16 frozen-books boundary "
                              "of the closed books (Company.accounts_frozen_till_date); without "
                              "it get_period_locks "
                              "raises and every plan/submit/cancel on this target is refused")]
    return [_finding(FAIL, f"Company read: unexpected response (HTTP {status}) — the closed-books "
                          "check will refuse on an unreadable source")]


def probe_pcv_read(target, env, read_file, transport):
    """One read-only GET of the Period Closing Voucher DocType — required since
    ``get_period_locks`` reads the latest submitted Period Closing Voucher for the PCV boundary of
    the closed books (``pacioli/erpnext.py``), and an unreadable source **RAISES**, refusing the
    whole plan/submit/cancel — the same deny-on-unreadable class as Company/Workflow/Accounts
    Settings above, and this source had NO probe until now.

    **The same required-read inversion as** :func:`probe_company_read`: a **403 here is a FAIL**.
    Readable-but-empty still PASSES (a credential can legally see zero PCV rows under row-level
    permission restrictions and still have the DocType-level read grant the lock needs). The
    doctype name has a space — the path segment MUST be percent-encoded (a raw space makes
    urllib reject the URL; this exact bug was just found live on the Accounts Settings probe,
    0.9.7 — see :func:`probe_accounts_settings`). Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "Period Closing Voucher read probe skipped — credential does not "
                              "resolve (above)")]
    seg = urllib.parse.quote("Period Closing Voucher", safe="")
    url = f"{target.base_url}/api/resource/{seg}"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"Period Closing Voucher read probe: bench unreachable: {exc}")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [_finding(OK, "Period Closing Voucher read: readable — required for the PCV "
                             "boundary of the closed books (get_period_locks)")]
    if status == 403:
        return [_finding(FAIL, "Period Closing Voucher read (403): the credential cannot read "
                              "the Period Closing Voucher DocType — grant read permission on "
                              "Period Closing Voucher (Role Permission Manager); required by "
                              "get_period_locks for the PCV boundary of the closed books — "
                              "without it get_period_locks raises and every plan/submit/cancel "
                              "on this target is refused")]
    return [_finding(FAIL, f"Period Closing Voucher read: unexpected response (HTTP {status}) — "
                          "the closed-books check will refuse on an unreadable source")]


def probe_accounting_period_read(target, env, read_file, transport):
    """Read-only GET(s) of the Accounting Period DocType — required since ``get_period_locks``
    (F-S1) LISTs the periods containing a posting date for the company, then does a full-document
    **item GET per hit** to read ``closed_documents`` (the list endpoint never expands child
    tables — the same two-step :func:`pacioli.erpnext.ErpnextClient.get_active_workflows` already
    uses). An unreadable LIST *or* item GET **RAISES** inside ``get_period_locks``, refusing the
    whole plan/submit/cancel exactly like PCV/Company/Workflow above.

    Same required-read inversion as those: a **403 on the LIST is a FAIL**. Readable-but-empty
    (zero periods) still PASSES — the readability grant is what's being probed, not the presence
    of any period — and in that case there is nothing to item-GET, so this returns exactly one
    finding. When the LIST returns at least one row, this probe ALSO exercises the item-GET leg
    against the first hit (the exact read ``get_period_locks`` would make) and returns a SECOND
    finding for it — readability of the row the lock actually reads, not just the list. The
    doctype name has a space — percent-encoded, same discipline as :func:`probe_pcv_read`/
    :func:`probe_accounts_settings`."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "Accounting Period read probe skipped — credential does not "
                              "resolve (above)")]
    seg = urllib.parse.quote("Accounting Period", safe="")
    url = f"{target.base_url}/api/resource/{seg}"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"Accounting Period read probe: bench unreachable: {exc}")]
    if status == 403:
        return [_finding(FAIL, "Accounting Period read (403): the credential cannot read the "
                              "Accounting Period DocType — grant read permission on Accounting "
                              "Period (Role Permission Manager); required by get_period_locks "
                              "for the closed-period boundary of the closed books — without it "
                              "get_period_locks raises and every plan/submit/cancel on this "
                              "target is refused")]
    if not (status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list)):
        return [_finding(FAIL, f"Accounting Period read: unexpected response (HTTP {status}) — "
                              "the closed-books check will refuse on an unreadable source")]
    rows = payload["data"]
    findings = [_finding(OK, "Accounting Period read: readable — required for the closed-period "
                             "boundary of the closed books (get_period_locks)")]
    if not rows:
        return findings
    # At least one period exists — exercise the SAME item-GET leg get_period_locks makes (the
    # list endpoint never expands closed_documents; the lock is read off the full document).
    name = rows[0].get("name") if isinstance(rows[0], dict) else None
    if not isinstance(name, str) or not name.strip():
        findings.append(_finding(FAIL, "Accounting Period list row is missing a name — cannot "
                                      "exercise the item-GET leg get_period_locks depends on"))
        return findings
    item_url = f"{url}/{urllib.parse.quote(name, safe='')}"
    try:
        item_status, item_payload = transport(
            "GET", item_url, {"Authorization": f"token {key}:{secret}"})
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        findings.append(_finding(FAIL, f"Accounting Period item read ({name}): bench unreachable: "
                                      f"{exc}"))
        return findings
    if item_status == 403:
        findings.append(_finding(FAIL, f"Accounting Period item read ({name}, 403): the "
                                      "credential can list Accounting Period but not read a full "
                                      "document — grant read permission on Accounting Period "
                                      "(Role Permission Manager); required by get_period_locks to "
                                      "read closed_documents — without it get_period_locks raises "
                                      "and every plan/submit/cancel on this target is refused"))
    elif item_status == 200 and isinstance(item_payload, dict) \
            and isinstance(item_payload.get("data"), dict):
        findings.append(_finding(OK, f"Accounting Period item read ({name}): readable — "
                                     "required for get_period_locks to read closed_documents "
                                     "(the doctype-aware period-close check, F-S1)"))
    else:
        findings.append(_finding(FAIL, f"Accounting Period item read ({name}): unexpected "
                                      f"response (HTTP {item_status}) — the closed-books check "
                                      "will refuse on an unreadable source"))
    return findings


def probe_accounts_settings(target, env, read_file, transport):
    """One read-only GET of the Accounts Settings single — required since EVERY supported
    doctype's ``plan_cancel``/``plan_cascade_cancel`` now reads
    ``unlink_payment_on_cancellation_of_invoice`` (F-R1's settling-PE disclosure,
    :func:`pacioli.tools._settling_reference_risk_flags`, widened beyond the prior JE-only gate —
    Journal Entry's OWN standing EG-note/unlink flag,
    :func:`pacioli.tools._journal_entry_cancel_flags_for_settings`, rides the same read), and an
    unreadable settings doc **RAISES**, refusing the whole plan.

    **The same inversion as** :func:`probe_company_read`/:func:`probe_workflow_read`, **not**
    :func:`probe_bench`: a **403 here is a FAIL**, because every doctype's cancel plan REQUIRES
    this read since F-R1 — narrower than Company/Workflow (only *cancels*, not every governed
    write), but no longer narrower by *doctype* the way it was before F-R1 (Journal-Entry-only);
    the remedy names this scope honestly. Readable is a PASS (the value itself is disclosure, not
    a gate). Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "Accounts Settings read probe skipped — credential does not "
                              "resolve (above)")]
    # "Accounts Settings" has a space — the path segment MUST be percent-encoded (the erpnext
    # client quotes doc names the same way; Company/Workflow never hit this, having no space).
    seg = urllib.parse.quote("Accounts Settings", safe="")
    url = f"{target.base_url}/api/resource/{seg}/{seg}"
    params = {"fields": json.dumps(["unlink_payment_on_cancellation_of_invoice"])}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"Accounts Settings read probe: bench unreachable: {exc}")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        unlink = payload["data"].get("unlink_payment_on_cancellation_of_invoice")
        state = "ON" if unlink else "OFF"
        return [_finding(OK, "Accounts Settings read: readable — required for every supported "
                             f"doctype's cancel blast-radius disclosure (F-R1; "
                             f"unlink_payment_on_cancellation_of_invoice is {state} on this "
                             "target)")]
    if status == 403:
        return [_finding(FAIL, "Accounts Settings read (403): the credential cannot read the "
                              "Accounts Settings single — grant read permission on Accounts "
                              "Settings (Role Permission Manager); required for every supported "
                              "doctype's cancel plan's blast-radius disclosure (F-R1, widened "
                              "beyond the prior Journal-Entry-only gate) — without it every "
                              "plan_cancel/plan_cascade_cancel on this target refuses")]
    return [_finding(FAIL, f"Accounts Settings read: unexpected response (HTTP {status}) — a "
                          "cancel plan will refuse on an unreadable source")]


def probe_payment_ledger_read(target, env, read_file, transport):
    """F-R1: one read-only GET (LIST) of the Payment Ledger Entry DocType — required since
    ``get_settling_references`` (``pacioli/erpnext.py``) reads it to disclose a cancel's
    settling-PE (or other ``auto_cancel_exempted_doctypes`` voucher) blast radius in
    ``plan_cancel``/``plan_cascade_cancel``, for EVERY supported doctype (not just Journal Entry —
    the widened F-R1 gate), and an unreadable read **RAISES**, refusing the whole plan.

    **The same required-read inversion as** :func:`probe_company_read`/:func:`probe_pcv_read`/
    :func:`probe_accounting_period_read`, **not** :func:`probe_bench`: a **403 here is a FAIL** —
    the disclosure REQUIRES this read, on every supported doctype's cancel plan, not just one.
    Readable-but-empty still PASSES (a credential can legally see zero rows under row-level
    permission restrictions and still have the DocType-level read grant the disclosure needs).
    The doctype name has a space — percent-encoded, same discipline as
    :func:`probe_pcv_read`/:func:`probe_accounts_settings`/:func:`probe_accounting_period_read`.
    Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "Payment Ledger Entry read probe skipped — credential does not "
                              "resolve (above)")]
    seg = urllib.parse.quote("Payment Ledger Entry", safe="")
    url = f"{target.base_url}/api/resource/{seg}"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"Payment Ledger Entry read probe: bench unreachable: {exc}")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [_finding(OK, "Payment Ledger Entry read: readable — required for the settling-PE "
                             "cancel disclosure on every supported doctype "
                             "(get_settling_references, F-R1)")]
    if status == 403:
        return [_finding(FAIL, "Payment Ledger Entry read (403): the credential cannot read the "
                              "Payment Ledger Entry DocType — grant read permission on Payment "
                              "Ledger Entry (Role Permission Manager); required for the "
                              "settling-PE cancel disclosure on EVERY supported doctype — without "
                              "it every plan_cancel/plan_cascade_cancel on this target refuses")]
    return [_finding(FAIL, f"Payment Ledger Entry read: unexpected response (HTTP {status}) — a "
                          "cancel plan will refuse on an unreadable source")]


def probe_gl_entry_read(target, env, read_file, transport):
    """The Close, Half 2 (the Reconciliation) — closes the "GL Entry has no probe" asymmetry:
    ``sweep_gl_entries`` (``pacioli/erpnext.py``) reads the GL Entry DocType's creation-window
    movement for ``close --reconcile``, and an unreadable read **RAISES**, refusing the whole
    reconciliation (the audit source itself is unreadable — deny-biased, the Half-2 design pin).

    **The same required-read inversion as** :func:`probe_payment_ledger_read`/
    :func:`probe_company_read`/:func:`probe_pcv_read`/:func:`probe_accounting_period_read`, **not**
    :func:`probe_bench`: a **403 here is a FAIL** — the GL sweep REQUIRES this read for every
    ``close --reconcile`` call, not an optional corroboration. Readable-but-empty still PASSES (a
    credential can legally see zero rows under row-level permission restrictions and still have
    the DocType-level read grant the sweep needs). Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "GL Entry read probe skipped — credential does not resolve "
                              "(above)")]
    seg = urllib.parse.quote("GL Entry", safe="")
    url = f"{target.base_url}/api/resource/{seg}"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"GL Entry read probe: bench unreachable: {exc}")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [_finding(OK, "GL Entry read: readable — required for the Half-2 reconciliation "
                             "GL sweep (sweep_gl_entries, close --reconcile)")]
    if status == 403:
        return [_finding(FAIL, "GL Entry read (403): the credential cannot read the GL Entry "
                              "DocType — grant read permission on GL Entry (Role Permission "
                              "Manager); required for the Half-2 reconciliation GL sweep — "
                              "without it every `close --reconcile` on this target refuses")]
    return [_finding(FAIL, f"GL Entry read: unexpected response (HTTP {status}) — "
                          "`close --reconcile` will refuse on an unreadable source")]


def probe_repost_read(target, env, read_file, transport):
    """The Close, Half 2 — the Repost Accounting Ledger read (``get_reposts``,
    ``pacioli/erpnext.py``), which explains a second-generation GL row (a repost re-derived a
    voucher's ledger in place) to ``close --reconcile``.

    **The ONE deliberate WARN-on-403 probe in this module, and here is why it differs from every
    other required-read probe above:** an unreadable Repost Accounting Ledger read is, by the
    glue's own design (``cmd_close``'s reconciliation wiring), a **NON-FATAL** flag — the
    reconciliation still runs and still completes, it just cannot attribute a second-generation
    row to the repost that caused it. Contrast :func:`probe_gl_entry_read`/
    :func:`probe_payment_ledger_read`/:func:`probe_company_read` etc., where a 403 FAILS doctor
    because the read those probes cover is REQUIRED — the whole plan/reconciliation refuses
    without it. This read is corroboration, not the audit source, so doctor informs (WARN) rather
    than refuses (FAIL) on both a 403 and any other unreadable response — deny-biased toward
    telling the operator, never toward blocking readiness over an optional grant. 200 + a list
    still PASSES (OK). Returns exactly one finding."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(WARN, "Repost Accounting Ledger read probe skipped — credential does "
                              "not resolve (above)")]
    seg = urllib.parse.quote("Repost Accounting Ledger", safe="")
    url = f"{target.base_url}/api/resource/{seg}"
    params = {"fields": json.dumps(["name"]), "limit_page_length": "1"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(WARN, f"Repost Accounting Ledger read probe: bench unreachable: {exc} "
                              "— non-fatal, the reconciliation runs without repost attribution")]
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [_finding(OK, "Repost Accounting Ledger read: readable — enables second-"
                             "generation repost attribution on close --reconcile")]
    if status == 403:
        return [_finding(WARN, "Repost Accounting Ledger unreadable — the reconciliation runs "
                              "but cannot attribute a second generation of GL rows to a repost; "
                              "grant read on Repost Accounting Ledger to enable it (HTTP 403, "
                              "non-fatal — this is corroboration, not close --reconcile's audit "
                              "source)")]
    return [_finding(WARN, f"Repost Accounting Ledger read: unexpected response (HTTP {status}) "
                          "— non-fatal, but close --reconcile cannot attribute second-generation "
                          "rows to a repost without a readable source; grant read on Repost "
                          "Accounting Ledger to enable it")]


def probe_roles(target, env, read_file, transport):
    """The tight-role seat: read the seat's OWN roles and refuse a spine-voiding one.

    Reads the seat's role list via the v2 doctype-resolved method route
    (:data:`ROLES_PROBE_PATH`) — which routes to frappe's whitelisted
    ``frappe.core.doctype.user.user.get_roles`` (raw "Has Role" query, no ``has_permission``
    check, so a tight non-superuser seat can read its own roles). A seat carrying a
    **spine-voiding** role (:data:`_SPINE_VOIDING_ROLES` — System Manager or the literal
    Administrator role) is a **FAIL**: over frappe's own REST surface that role can write Custom
    DocPerm rows (grant itself any permission), mint API keys, and — with server scripts enabled —
    run arbitrary server-side code via the System Console; it can dismantle the governance it
    operates under, exactly the "voids the whole trust spine" bar the module docstring sets for a
    superuser, here applied to the *role* rather than the username.

    **Deny-biased, the required-read inversion** (:func:`probe_payment_ledger_read`, NOT
    :func:`probe_bench`): a 403, an unparseable body, or an empty role list is a **FAIL** — a seat
    whose roles cannot be audited cannot be certified least-privilege. A **403 specifically** means
    the credential lacks the ``User.get_roles`` methods-grant (the one new, config-only grant this
    probe needs). A clean seat passes with the role list echoed for eyeball review; an
    **over-broad** role (:data:`_OVER_BROAD_ROLES` — e.g. Accounts Manager, which also grants
    delete and period-closing the broker never uses) adds a **WARN** but is not a refusal. Returns
    one finding (the verdict), plus a second WARN when a clean seat is nonetheless over-broad."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "roles probe skipped — credential does not resolve (above)")]
    url = f"{target.base_url}{ROLES_PROBE_PATH}"
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"})
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"roles probe: bench unreachable: {exc}")]
    if status == 403:
        return [_finding(FAIL, "roles probe (403): the credential cannot read its own roles — "
                              "grant the method 'User.get_roles' (API Key Scope → methods); "
                              "doctor cannot certify this seat is least-privilege without it")]
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [_finding(FAIL, f"roles probe: unexpected response (HTTP {status}) — a seat whose "
                              "roles cannot be read cannot be certified least-privilege "
                              "(deny-biased)")]
    # strip + drop blanks (defense-in-depth, matching the guard's own .strip() discipline): a
    # whitespace-padded "System Manager " cannot evade the exact-name match below, and a blank-only
    # list falls through to the deny-biased empty-list FAIL. Case is deliberately NOT folded — a
    # differently-cased role is a DIFFERENT frappe role that does not carry the powers, so folding
    # would false-positive on a harmless custom role.
    roles = [s for s in (r.strip() for r in payload["data"] if isinstance(r, str)) if s]
    if not roles:
        return [_finding(FAIL, "roles probe: the seat reports no roles — anomalous (an "
                              "authenticated seat always carries at least All/Guest); cannot "
                              "certify least-privilege")]
    voiding = [r for r in _SPINE_VOIDING_ROLES if r in roles]
    if voiding:
        return [_finding(FAIL, f"roles probe: the seat carries {', '.join(voiding)} — a "
                              "bench-superuser role that can write Custom DocPerm rows, mint API "
                              "keys, and (if server scripts are enabled) run arbitrary code via the "
                              "System Console, i.e. dismantle the governance it operates under; run "
                              "the broker under a least-privilege seat (Accounts User + a custom "
                              "read-only role — see the broker README)")]
    shown = ", ".join(r for r in roles if r not in _FRAPPE_AUTO_ROLES) or "(none beyond All/Guest)"
    findings = [_finding(OK, f"roles: seat carries {shown} — no spine-voiding role")]
    over_broad = [r for r in _OVER_BROAD_ROLES if r in roles]
    if over_broad:
        findings.append(_finding(WARN, f"the seat carries {', '.join(over_broad)} — broader than "
                                       "the broker needs (it never deletes and never closes a "
                                       "period); the minimal seat is Accounts User + a custom "
                                       "read-only role (Sales Invoice cancel + Accounts "
                                       "Settings/Period Closing Voucher/Workflow read)"))
    return findings


def probe_belt_exemptions(target, env, read_file, transport):
    """The belt-exemption drift watch (docs/plans/2026-07-09-probe-belt-exemptions.md).

    ERPNext's frozen-books and closed-period belts each carry a **role escape hatch** — a single
    config field that, once non-blank, silently disables that belt for every seat holding the
    named role: ``Accounting Period.exempted_role`` (which additionally has **no
    anti-Administrator carve-out** — the moment it is set, Administrator bypasses that belt too),
    ``Company.role_allowed_for_frozen_entries`` (v16), and legacy
    ``Accounts Settings.frozen_accounts_modifier`` (≤v15). PHASE X (T-P5) verified these blank on
    the proof bench; this probe is what keeps that TRUE over time — drift detection, not
    prevention (a write after doctor ran is invisible until the next run; recorded residual).

    Reads ride EXISTING grants only (Company read · Accounts Settings read · Accounting Period
    read · ``User.get_roles``): the seat's own roles (same v2 route as :func:`probe_roles`), the
    pinned company's full doc (or every company on an unpinned target), the Accounts Settings
    Single, and every Accounting Period's full doc (the same LIST → item-GET two-step
    ``get_period_locks`` uses). **Version-safe by construction (the F-C1 lesson):** every field is
    read off a FULL document with ``.get()`` — absent (the other major-version's shape) is blank,
    never an unknown-column error the way a LIST ``fields``/``filters`` selection would be. Every
    LIST carries ``limit_page_length: "0"`` (the F-V1 lesson — a period past row 20 must not be
    invisible).

    Verdict, deny-biased: exemption role **held by this seat → FAIL** (that belt does not fire for
    this seat's postings — the broker's own closed-books refusal becomes the only gate). The
    cross-ref set deliberately KEEPS the frappe-auto roles — ``exempted_role = "All"`` exempts
    every authenticated seat and must fail loudly. Exemption set but **not held → WARN** (off for
    someone; and — Accounting Period case — Administrator now bypasses it too). Nothing set → OK.
    Any unreadable source / unexpected shape → **FAIL** naming the source, the same required-read
    inversion as every gate-feeding probe."""
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return [_finding(FAIL, "belt-exemptions probe skipped — credential does not resolve "
                              "(above)")]
    auth = {"Authorization": f"token {key}:{secret}"}

    # 1. The seat's own roles (the cross-ref side). Same call shape as probe_roles; kept
    # self-contained so this probe stands alone in any future probe reordering.
    try:
        status, payload = transport("GET", f"{target.base_url}{ROLES_PROBE_PATH}", auth)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return [_finding(FAIL, f"belt-exemptions probe: bench unreachable: {exc}")]
    if status == 403:
        return [_finding(FAIL, "belt-exemptions probe (403): the credential cannot read its own "
                              "roles — grant the method 'User.get_roles' (API Key Scope → "
                              "methods); the exemption cross-reference needs the seat's role "
                              "list")]
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [_finding(FAIL, f"belt-exemptions probe: unexpected roles response (HTTP {status}) "
                              "— cannot cross-reference exemptions against an unreadable seat "
                              "(deny-biased)")]
    seat_roles = {s for s in (r.strip() for r in payload["data"] if isinstance(r, str)) if s}

    def _get(url, params=None):
        """(status, payload) — a raising transport degrades to (None, exc); ``_http`` renders
        either shape into the deny message so the cause is never swallowed."""
        try:
            return transport("GET", url, auth, params=params)
        except Exception as exc:  # noqa: BLE001 — degrade to a deny-shaped tuple, never traceback
            return None, exc

    def _http(status, payload):
        return f"bench unreachable: {payload}" if status is None else f"HTTP {status}"

    def _field(doc, field):
        """(role_or_None, error_or_None) — absent/None/blank-after-strip is 'not set' (the other
        major-version's doc shape reads as blank, F-C1); a non-string value is an error."""
        value = doc.get(field)
        if value is None:
            return None, None
        if not isinstance(value, str):
            return None, f"unexpected {field} value {value!r}"
        return (value.strip() or None), None

    exemptions = []  # (source description, role)

    # 2. Company.role_allowed_for_frozen_entries — full-doc item GET(s), never a LIST fields
    # selection of the (v16-only) column.
    if target.company:
        company_names = [target.company]
    else:
        status, payload = _get(f"{target.base_url}/api/resource/Company",
                               params={"fields": json.dumps(["name"]), "limit_page_length": "0"})
        if status != 200 or not isinstance(payload, dict) \
                or not isinstance(payload.get("data"), list):
            return [_finding(FAIL, f"belt-exemptions probe: Company list unreadable "
                                  f"({_http(status, payload)}) — cannot audit the frozen-books exemption "
                                  "(deny-biased)")]
        company_names = []
        for row in payload["data"]:
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.strip():
                # Deny-bias symmetry with the Accounting Period loop below: a nameless row must
                # FAIL, never silently drop that company from the audit (its exemption would go
                # unwatched while doctor reports ok).
                return [_finding(FAIL, "belt-exemptions probe: Company list row is missing a "
                                      "name — cannot audit the frozen-books exemption "
                                      "(deny-biased)")]
            company_names.append(name)
    for name in company_names:
        seg = urllib.parse.quote(name, safe="")
        status, payload = _get(f"{target.base_url}/api/resource/Company/{seg}")
        if status != 200 or not isinstance(payload, dict) \
                or not isinstance(payload.get("data"), dict):
            return [_finding(FAIL, f"belt-exemptions probe: Company {name!r} unreadable "
                                  f"({_http(status, payload)}) — cannot audit the frozen-books exemption "
                                  "(deny-biased)")]
        role, err = _field(payload["data"], "role_allowed_for_frozen_entries")
        if err:
            return [_finding(FAIL, f"belt-exemptions probe: Company {name!r}: {err} "
                                  "(deny-biased)")]
        if role:
            exemptions.append((f"Company {name!r} role_allowed_for_frozen_entries", role))

    # 3. Legacy Accounts Settings.frozen_accounts_modifier (≤v15; absent on v16 → blank).
    seg = urllib.parse.quote("Accounts Settings", safe="")
    status, payload = _get(f"{target.base_url}/api/resource/{seg}/{seg}")
    if status != 200 or not isinstance(payload, dict) \
            or not isinstance(payload.get("data"), dict):
        return [_finding(FAIL, f"belt-exemptions probe: Accounts Settings unreadable "
                              f"({_http(status, payload)}) — cannot audit the legacy frozen-books exemption "
                              "(deny-biased)")]
    role, err = _field(payload["data"], "frozen_accounts_modifier")
    if err:
        return [_finding(FAIL, f"belt-exemptions probe: Accounts Settings: {err} (deny-biased)")]
    if role:
        exemptions.append(("Accounts Settings frozen_accounts_modifier", role))

    # 4. Every Accounting Period's exempted_role — LIST names (page-pinned, company-filtered when
    # pinned; the company column is version-stable, unlike F-C1's disabled), then the same
    # item-GET leg get_period_locks makes.
    seg = urllib.parse.quote("Accounting Period", safe="")
    params = {"fields": json.dumps(["name"]), "limit_page_length": "0"}
    if target.company:
        params["filters"] = json.dumps([["company", "=", target.company]])
    status, payload = _get(f"{target.base_url}/api/resource/{seg}", params=params)
    if status != 200 or not isinstance(payload, dict) \
            or not isinstance(payload.get("data"), list):
        return [_finding(FAIL, f"belt-exemptions probe: Accounting Period list unreadable "
                              f"({_http(status, payload)}) — cannot audit the closed-period exemption "
                              "(deny-biased)")]
    for row in payload["data"]:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip():
            return [_finding(FAIL, "belt-exemptions probe: Accounting Period list row is missing "
                                  "a name — cannot audit the closed-period exemption "
                                  "(deny-biased)")]
        status, payload_item = _get(
            f"{target.base_url}/api/resource/{seg}/{urllib.parse.quote(name, safe='')}")
        if status != 200 or not isinstance(payload_item, dict) \
                or not isinstance(payload_item.get("data"), dict):
            return [_finding(FAIL, f"belt-exemptions probe: Accounting Period {name!r} unreadable "
                                  f"({_http(status, payload_item)}) — cannot audit the closed-period exemption "
                                  "(deny-biased)")]
        role, err = _field(payload_item["data"], "exempted_role")
        if err:
            return [_finding(FAIL, f"belt-exemptions probe: Accounting Period {name!r}: {err} "
                                  "(deny-biased)")]
        if role:
            exemptions.append((f"Accounting Period {name!r} exempted_role", role))

    # 5. The verdict.
    if not exemptions:
        return [_finding(OK, "belt exemptions: none set — the frozen/closed-period belts have "
                             "no role escape hatch on this target")]
    held = [(src, role) for src, role in exemptions if role in seat_roles]
    unheld = [(src, role) for src, role in exemptions if role not in seat_roles]
    findings = []
    if held:
        shown = "; ".join(f"{src} exempts {role!r}" for src, role in held)
        findings.append(_finding(FAIL, f"belt exemptions: {shown} — a role THIS SEAT carries, so "
                                       "that belt does not fire for this seat's postings; the "
                                       "broker's own closed-books refusal is the only remaining "
                                       "gate — clear the field or change the seat"))
    if unheld:
        shown = "; ".join(f"{src} exempts {role!r}" for src, role in unheld)
        findings.append(_finding(WARN, f"belt exemptions: {shown} — not carried by this seat, but "
                                       "the belt is disabled for any seat holding it (and "
                                       "Accounting Period.exempted_role has no "
                                       "anti-Administrator carve-out — once non-blank, "
                                       "Administrator bypasses that belt too); confirm this is "
                                       "intended"))
    return findings


def _probe_workflow_read_one(doctype, target, env, read_file, transport):
    """One read-only GET of the active-workflow list for a single ``doctype`` — the workflow-SoD
    gate's source (``tools.py`` reads it before EVERY governed submit/cancel of that doctype).

    **The deliberate asymmetry with :func:`probe_bench`, made explicit:** there, a 403 is a
    PASS — that probe checks a *whitelisted method* the broker doesn't need, so "scoped tighter
    than the probe" is the prescribed posture. HERE a **403 is a FAIL**, because the gate
    *requires* readability: the ``Workflow`` DocType is System Manager-read-only by frappe
    default, an unreadable gate source refuses (deny-biased, the house rule), and therefore a
    credential without the custom read grant has **every governed submit/cancel of this doctype
    denied** — workflow configured or not. Readable-but-empty is a PASS (no workflow configured
    is a legal state; the gate passes through to the marker). Anything else fails with the
    reason. Returns exactly one finding.
    """
    try:
        key = _resolve_ref(target.api_key, "api_key", env, read_file)
        secret = _resolve_ref(target.api_secret, "api_secret", env, read_file)
    except RegistryError:
        return _finding(FAIL, f"workflow-read probe ({doctype}) skipped — credential does not "
                              "resolve (above)")
    url = f"{target.base_url}/api/resource/Workflow"
    params = {"fields": json.dumps(["name"]),
              "filters": json.dumps([["document_type", "=", doctype],
                                     ["is_active", "=", 1]]),
              "limit_page_length": "0"}
    try:
        status, payload = transport("GET", url, {"Authorization": f"token {key}:{secret}"},
                                    params=params)
    except Exception as exc:  # noqa: BLE001 — a probe failure must never traceback out of doctor
        return _finding(FAIL, f"workflow-read probe ({doctype}): bench unreachable: {exc}")
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        n = len(payload["data"])
        detail = f"{n} active workflow(s) on {doctype}" if n else \
                 f"none active on {doctype} (a legal state — the gate passes through)"
        return _finding(OK, f"workflow read: {doctype} is readable — {detail}")
    if status == 403:
        return _finding(FAIL, f"workflow read ({doctype}): the credential cannot read the "
                              "Workflow DocType (403) — grant custom read permission on "
                              "Workflow for the broker's role (Role Permission Manager); "
                              f"required by the workflow-SoD gate for {doctype}; without it "
                              "every submit/cancel denies. (Unlike the method probe above, 403 "
                              "here is a failure: the gate requires readability.)")
    return _finding(FAIL, f"workflow read ({doctype}): unexpected response (HTTP {status}) — "
                          "the gate will refuse on an unreadable source")


def probe_workflow_read(target, env, read_file, transport):
    """Loop :func:`_probe_workflow_read_one` over every doctype this broker supports
    (:data:`pacioli.erpnext.SUPPORTED_DOCTYPES` — Sales Invoice, Purchase Invoice as of the
    breadth increment): one finding per doctype, readable-for-each is a pass, a 403 on ANY of
    them is the same FAIL+remedy for that doctype specifically — a failure on one doctype never
    masks or skips the check for the others, since each is an independent grant on a live bench
    (a company can configure Role Permission Manager per-doctype read access differently)."""
    return [_probe_workflow_read_one(doctype, target, env, read_file, transport)
           for doctype in SUPPORTED_DOCTYPES]


def run_doctor(env, *, target_name=None, offline=False,
               read_file=None, transport=default_transport):
    """Run every check; return ``(exit_code, lines)``. Exit is 0 only with zero failures."""
    if read_file is None:
        read_file = lambda p: Path(p).read_text(encoding="utf-8")  # noqa: E731
    lines, failed = ["pacioli doctor — config & readiness (read-only)"], False

    reg_findings, registry = check_registry(env)
    for level, msg in reg_findings:
        failed |= level == FAIL
        lines.append(_PREFIX[level] + msg)
    if registry is None:
        return 1, lines

    try:
        targets = [registry.get(target_name)] if target_name else \
                  [registry.get(n) for n in registry.names()]
    except RegistryError as exc:
        return 1, lines + [_PREFIX[FAIL] + str(exc)]

    for target in targets:
        lines.append(f"[target {target.name}]")
        lines.append(_PREFIX[OK] + f"base_url: {target.base_url}"
                     + (f" (company-pinned: {target.company})" if target.company else ""))
        findings = check_credentials(target, env, read_file)
        findings += check_state(env, target.name)
        if offline:
            findings.append(_finding(WARN, "bench probe, Company-read probe, workflow-read probe, "
                                           "Accounts-Settings-read probe, PCV-read probe, "
                                           "Accounting-Period-read probe, "
                                           "Payment-Ledger-Entry-read probe, GL-Entry-read probe, "
                                           "Repost-read probe, roles probe, and "
                                           "belt-exemptions probe skipped (--offline)"))
        else:
            findings += probe_bench(target, env, read_file, transport)
            findings += probe_company_read(target, env, read_file, transport)
            findings += probe_workflow_read(target, env, read_file, transport)
            findings += probe_accounts_settings(target, env, read_file, transport)
            findings += probe_pcv_read(target, env, read_file, transport)
            findings += probe_accounting_period_read(target, env, read_file, transport)
            findings += probe_payment_ledger_read(target, env, read_file, transport)
            findings += probe_gl_entry_read(target, env, read_file, transport)
            findings += probe_repost_read(target, env, read_file, transport)
            findings += probe_roles(target, env, read_file, transport)
            findings += probe_belt_exemptions(target, env, read_file, transport)
        for level, msg in findings:
            failed |= level == FAIL
            lines.append(_PREFIX[level] + msg)

    lines.append("NOT ready — fix the XX lines above." if failed else "ready.")
    return (1 if failed else 0), lines
