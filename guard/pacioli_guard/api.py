# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""One whitelisted endpoint: let a credential ask the floor about ITSELF.

Why this exists. `require_consent` is opt-in, so an install can upgrade to 0.7.0 and still be
wide open to the bypass the gate was built to close — and nothing anywhere would say so. A
silent insecure default is how that class of hole survives. The broker's `doctor` is the
readiness gate operators actually read, so the floor needs a way to answer "can this credential
post without consent?" out loud, every run.

Deliberately narrow, because a diagnostic endpoint is still an endpoint:

* **No arguments, no enumeration.** It reports on ``frappe.session.user`` and nothing else. A
  credential cannot ask about another credential, so this leaks nothing across seats. Reading
  `API Key Scope` over REST instead would have required granting the seat read on that DocType,
  which would hand it every OTHER credential's allowlist — trading one exposure for a worse one.
* **Read-only, and it names no allowlist contents.** Whether the gate is on is not a secret; the
  list of methods a credential may call is closer to one, and is not returned here.
* **Deny-biased in what it claims.** Anything it cannot establish is reported as not-established,
  never as "fine".
"""
import frappe

from pacioli_guard import __version__
from pacioli_guard.scope import RESOURCE_DOCTYPE_WILDCARD

SCOPE_DOCTYPE = "API Key Scope"


def _resource_posture(user):
    """How wide is THIS credential's resource branch: ``off`` / ``denies_all`` / ``all_doctypes`` /
    ``narrow``, or ``unknown`` if it cannot be established.

    POSTURE, never contents. The module rule above holds: whether a gate is open is not a secret,
    but the list of DocTypes a credential may touch is closer to one, so no names are returned —
    only which of the four states the grant is in. Reported for the same reason ``require_consent``
    is: guard 0.8.0 changed an empty allowlist from "every DocType on the site" to "nothing", and
    added an explicit ``"*"`` row for site-wide access. An operator should never be quietly unaware
    of how wide their own grant is, in either direction.

    Deny-biased and isolated: a failure here returns ``unknown`` rather than raising, so a posture
    read can never break the consent report it rides along with.
    """
    try:
        name = frappe.db.get_value(SCOPE_DOCTYPE, {"user": user}, "name")
        if not name:
            return "unknown"
        doc = frappe.get_doc(SCOPE_DOCTYPE, name)
        if not getattr(doc, "allow_resource", 0):
            return "off"
        rows = getattr(doc, "resource_doctypes", None) or []
        named = [r for r in (getattr(row, "ref_doctype", None) for row in rows)
                 if isinstance(r, str) and r.strip()]
        if RESOURCE_DOCTYPE_WILDCARD in named:
            return "all_doctypes"
        if not named:
            return "denies_all"
        return "narrow"
    except Exception:  # noqa: BLE001 — a diagnostic must never traceback into the auth path
        return "unknown"


@frappe.whitelist()
def consent_status():
    """Report whether THIS credential is consent-gated. Never reports on anyone else.

    Returns ``guard_version``, ``user``, ``scoped`` (does this credential have a grant at all),
    ``require_consent``, and ``resource_posture`` (see :func:`_resource_posture`). An unscoped
    credential is reported as ``scoped: False`` — that is the loudest possible state, because an
    unscoped credential is not governed at all.
    """
    user = frappe.session.user
    name = frappe.db.get_value(SCOPE_DOCTYPE, {"user": user}, "name")
    if not name:
        return {"guard_version": __version__, "user": user, "scoped": False,
                "require_consent": False, "resource_posture": "unknown"}
    value = frappe.db.get_value(SCOPE_DOCTYPE, name, "require_consent")
    return {"guard_version": __version__, "user": user, "scoped": True,
            "require_consent": bool(value), "resource_posture": _resource_posture(user)}
