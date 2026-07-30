# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The human mint route — create a `Pacioli Consent Marker` from the books side.

**Why this exists.** The floor has refused unconsented writes since 0.7.0, but this package shipped
no way for a human to create the marker it demands. The only route was an ad-hoc script run as
Administrator inside the container: proof that the mechanism worked, not something an operator could
be told to do. That absence is why every refusal in this package had to be rewritten to promise no
route, and it bites hardest where it is least visible — a site with `require_consent` on and no
marker ever minted cannot complete its first governed write.
`docs/plans/2026-07-26-consent-ceremony-decision.md` records this as Option B's outstanding cost.

**DELIBERATELY NOT WHITELISTED, and that is the security property of this module.** A whitelisted
mint endpoint would be reachable by exactly the api-key credentials the floor exists to constrain,
and a credential that can mint its own consent is signing its own permission slip. `consent_verdict`
refuses a self-minted marker at spend time, but that backstop is not a reason to open the door. This
runs from the books side only::

    bench --site <site> execute pacioli_guard.mint.mint_consent_marker \\
        --kwargs '{"ref_doctype": "Sales Invoice", "ref_docname": "ACC-SINV-2026-00004",
                   "ref_action": "submit", "ttl_seconds": 900}'

Whoever runs it is recorded as the minter, because `PacioliConsentMarker.before_insert` binds
`minted_by` from the session and overwrites anything a caller sends. Run it as the operator, never as
the agent's seat: the floor refuses a marker whose minter is the credential it authorises.

**The token is printed once and never stored.** Only its SHA-256 lands in the books, so reading every
marker row yields nothing spendable. Hand the token over out of band — the agent must not be able to
read the place it came from, or two hands collapse into one.

**The clock is the subtle part, and getting it wrong has bitten this project twice.** `expires_at` is
a frappe Datetime, stored naive, and `enforce._epoch` resolves a naive value through the **SITE's**
timezone. 0.10.0 fixed the reader after it had been resolving through the *process* zone, which made
every marker on a non-UTC site born expired. The old bench script was then left writing
`datetime.now()` — the process clock — so the two ends disagreed in the opposite direction: measured
on a live bench (site America/Chicago, container UTC) a 900-second marker read as 17,980 seconds of
remaining life. Fail-OPEN on lifetime, never an escape, but "short-lived by design" was off twentyfold.
This module writes `frappe.utils.now_datetime()`, frappe's own site-zone naive clock, which is the
value `_epoch` assumes. Do not substitute `datetime.now()`.
"""
import datetime
import secrets
import time
import zoneinfo

import frappe

from pacioli_guard.scope import plan_consent_marker

MARKER_DOCTYPE = "Pacioli Consent Marker"

# 24 bytes urlsafe-base64 -> 32 characters, comfortably above the pure planner's floor. The same
# width `pacioli mint` uses for the broker's own marker, for the same reason: the token is compared
# by hash, so it must be infeasible to guess offline from a leaked row.
_TOKEN_BYTES = 24


def _coerce_ttl(value):
    """`bench execute --kwargs` hands values in as strings. The glue absorbs that; the pure planner
    stays strict and refuses a string, so the coercion has to happen here or nowhere."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return value
    return value


def _document_exists(doctype, docname):
    """Does this exact document exist? Deliberately NOT `frappe.db.exists`.

    `Database.exists` carries a shortcut for Single doctypes that it applies unconditionally
    (`frappe/database/database.py`)::

        if dt != "DocType" and dt == dn:
            # single always exists (!)
            return dn

    So `exists("Sales Invoice", "Sales Invoice")` is TRUTHY with no such document present, and this
    module then minted a marker for a document that does not exist — breaking its own stated
    invariant, on any typo where the docname happens to equal the doctype. Found by an independent
    review, 2026-07-29, reading the real frappe source rather than our comment about it.

    A `name` lookup answers the question that was actually being asked. A genuine Single is still
    handled correctly: its row's `name` IS the doctype name, so the lookup finds it.
    """
    return bool(frappe.db.get_value(doctype, docname, "name"))


def mint_consent_marker(ref_doctype, ref_docname, ref_action="submit", ttl_seconds=900):
    """Create one marker for one act on one document. Prints the token; returns everything but it.

    ``{"ok": True, "name": <marker name>, "expires_at": <site-zone naive>}`` or
    ``{"ok": False, "reason": <why>}``. Nothing is inserted or committed on a refusal.

    **The raw token is deliberately NOT in the return value.** `bench execute` echoes whatever a
    function returns (`frappe/commands/utils.py`: ``if ret: print(json.dumps(ret, ...))``), so
    returning it printed the secret a SECOND time, in an unlabelled JSON blob that a "redact lines
    containing `token:`" filter would sail past. Observed for real on this module's first live use: the
    token landed twice in a captured session transcript. The operator gets it from the explicit print
    below, which is the one place it should ever appear.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    ttl = _coerce_ttl(ttl_seconds)
    # VALIDATE BEFORE TOUCHING THE DATABASE (independent review, 2026-07-29). This used to call
    # `frappe.db.exists` first, on entirely unvalidated input — and `Database.exists(dt, dn)` accepts
    # a FILTER DICT as its second argument, so a dict `ref_docname` ran a "does ANY row match" query
    # instead of checking one named document. The pure planner refused it a moment later so nothing
    # was inserted, but the query had already gone to the DB. Types are established here first now.
    #
    # The planner VALIDATES, hashes the token, and computes the AUTHORITATIVE expiry as an epoch
    # instant — the domain `consent_verdict` compares in, and the one that has no timezone to
    # disagree about. It is handed the true clock (`time.time()`), so `row["expires_at"]` is now
    # load-bearing rather than the dead output an earlier draft of this function left it as.
    ok, reason, row = plan_consent_marker(
        ref_doctype=ref_doctype, ref_docname=ref_docname, ref_action=ref_action,
        token=token, ttl_seconds=ttl, now=time.time())
    if not ok:
        print(f"REFUSED: {reason}")
        return {"ok": False, "reason": reason}

    if not _document_exists(row["ref_doctype"], row["ref_docname"]):
        # A marker for a document that is not there is a row nothing can ever spend, and a typo here
        # means the operator believes they authorised something they did not.
        reason = (f"{ref_doctype} {ref_docname!r} does not exist on this site — refusing to mint a "
                  f"marker nothing could spend")
        print(f"REFUSED: {reason}")
        return {"ok": False, "reason": reason}

    # The HUMAN-READABLE rendering of that same moment, for the desk list view. Site-zone naive,
    # which is frappe's convention for a Datetime field and what `_epoch` resolves a naive value
    # through. This must not become `datetime.datetime.now()` (the process clock) — see the module
    # docstring for the 900s-read-as-17,980s defect that caused.
    #
    # ⚠️ It is a RENDERING, not the source of truth, and the difference matters: naive wall-clock
    # arithmetic is off by exactly the DST shift across a transition, so on those two days a year
    # this value and `expires_at_epoch` genuinely disagree. The epoch one is right, and the gate
    # reads it in preference. Deriving this from the epoch keeps them the same moment everywhere else.
    expires_at = datetime.datetime.fromtimestamp(
        row["expires_at"], zoneinfo.ZoneInfo(frappe.utils.get_system_timezone())
    ).replace(tzinfo=None)
    doc = frappe.get_doc({
        "doctype": MARKER_DOCTYPE,
        "ref_doctype": row["ref_doctype"],
        "ref_docname": row["ref_docname"],
        "ref_action": row["ref_action"],
        "token_hash": row["token_hash"],
        "expires_at": expires_at,
        "expires_at_epoch": row["expires_at"],
        "burned": row["burned"],
        # `minted_by` is deliberately absent — `before_insert` binds it from the session and
        # overwrites a caller-supplied value. Sending one would imply this call establishes the
        # separation property. It does not; the server does (floor audit F3).
    })
    # NO `ignore_permissions` (independent review, 2026-07-29). It was redundant — the only reachable
    # caller is `bench execute`, where `frappe.connect()` seats the session as Administrator, who
    # bypasses permission checks anyway. But setting it pre-emptively disabled the DocType's own
    # System-Manager-only `create` permission, which is the one OTHER layer that would catch this
    # function being reached from a lower-privileged context by some future refactor or whitelist
    # mistake. The `minted_by` binding in `before_insert` is the real control; this keeps the
    # permission system as a genuine second line rather than a no-op.
    doc.insert()
    frappe.db.commit()

    # THE OPERATOR'S ONLY INDEPENDENT VIEW OF WHAT THEY ARE AUTHORISING. The broker CLI learned this
    # in the 2026-07-26 redteam: a printout that names only the token renders a one-invoice submit
    # and a five-document unwind identically, and the human's whole picture of the act then comes
    # from the agent's narration — precisely the thing consent is supposed to be independent of.
    print(f"marker:  {doc.name}")
    print(f"act:     {row['ref_action'].upper()} {row['ref_doctype']} {row['ref_docname']}")
    print(f"minted:  {getattr(getattr(frappe, 'session', None), 'user', '?')}")
    print(f"expires: {expires_at} (site time, {ttl}s)")
    print(f"token:   {token}")
    print("hand the token over OUT OF BAND. It is single-use, bound to this document and this act,")
    print("and the agent must not be able to read wherever you put it.")
    # The token is NOT returned — see this function's docstring. `bench execute` echoes the return
    # value, so including it printed the secret twice.
    return {"ok": True, "name": doc.name, "expires_at": expires_at}
