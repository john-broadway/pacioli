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

# The handlers that ARE the consent gate — every `doc_events` entry that can DENY. Named here so
# the probe below checks for the real thing rather than for the mere presence of some
# `before_submit` on the site.
#
# ⚠️ This held only `before_submit`/`before_cancel` until 2026-08-11, while `hooks.py` has
# registered the two preview gates since 0.13.0 and both of them refuse (`act.py`'s
# `_deny("consent (preview)", …)`). So the receipt reported the gate LOADED on a site carrying
# only the pre-0.13.0 pair — which is exactly the stale-hooks-cache shape this probe was written
# to catch, one release later: the 2026-07-29 incident was a cached registry that predated the
# handlers the site needed, and a cache predating 0.13.0 is the same failure wearing a newer
# version number.
#
# `after_insert` is deliberately NOT here: hooks.py's own comment records that it decides nothing
# and refuses nothing, so requiring it would make the receipt stricter than the gate.
# `test_api_reports_the_truth.py` derives the expected set from `hooks.py` itself, so this cannot
# drift from the registration it describes.
CONSENT_HANDLERS = {
    "before_submit": "pacioli_guard.act.before_submit",
    "before_cancel": "pacioli_guard.act.before_cancel",
    "before_gl_preview": "pacioli_guard.act.before_gl_preview",
    "before_sl_preview": "pacioli_guard.act.before_sl_preview",
}


def _gate_registered():
    """Is the consent gate ACTUALLY in this site's ``doc_events`` registry?

    ``require_consent`` is a REQUEST recorded on a document. This is the RECEIPT: it asks the
    running site whether the handlers that enforce that request are loaded. The two can disagree,
    and when they do, the flag is the one that lies.

    Earned on 2026-07-29, on the public bench. ``require_consent`` was ``1``, the installed
    ``hooks.py`` was byte-identical to source and declared all three ``doc_events``, and
    ``frappe.get_hooks("doc_events")["*"]`` returned ``None`` for every one of them — a stale
    hooks cache from a site created back when guard shipped ``auth_hooks`` only. Scope enforcement
    rode ``auth_hooks`` and worked perfectly, which made the floor look present. Consent rode
    ``doc_events`` and was not there at all. A submit with no marker returned 200 and moved the
    ledger. The card said governed; the room was open.

    ``consent_status`` reported ``require_consent: True`` throughout, and was useless for catching
    it, because a declared intention is not an enforced one. So: probe, never declare.

    Deny-biased, like everything else in this module: anything that cannot be established is
    reported as NOT established. An exception here means we could not prove the gate is loaded,
    which is exactly the state an operator must be told about.

    **Residual, stated because the field would otherwise overclaim.** This proves the handlers are
    REGISTERED, not that they FUNCTION. A handler listed in the registry whose module fails to
    import at call time would report ``True`` here and still fail open at the moment it mattered.
    So ``True`` means "the wiring the stale-cache class of failure destroys is present" — it is not
    a substitute for the live refusal receipt. The only thing that proves a gate holds is watching
    it refuse, which is why the bench matrix exists and why it runs the negative cases first.
    """
    try:
        star = (frappe.get_hooks("doc_events") or {}).get("*") or {}
        for event, handler in CONSENT_HANDLERS.items():
            entries = star.get(event) or []
            if isinstance(entries, str):
                entries = [entries]
            if handler not in entries:
                return False
        return True
    except Exception:  # noqa: BLE001 — a diagnostic must never traceback into the auth path
        return False


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
        # The parent-level flag grants EVERY doctype regardless of the child table
        # (``scope.is_permitted`` returns True before it ever looks at the rows), so it is read
        # first and reported first.
        #
        # ⚠️ This branch did not exist until 2026-08-11. Without it, the widest grant this app can
        # express — ``allow_resource=1, allow_all_doctypes=1`` with an empty child table — fell
        # through to the ``not named`` case and was reported as ``denies_all``, the NARROWEST of
        # the four states. This function's own docstring says an operator must never be quietly
        # unaware of how wide their grant is "in either direction", and it inverted the answer in
        # the direction that matters. Found by an independent review; the sentinel-row mechanism
        # named in the docstring above was retired (see ``scope.py``'s own correction: a ``"*"``
        # row is a validated Link and cannot be stored) and this flag replaced it, but the posture
        # report was never moved across.
        if getattr(doc, "allow_all_doctypes", 0):
            return "all_doctypes"
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
    ``require_consent``, ``gate_registered``, ``consent_enforced``, and ``resource_posture``
    (see :func:`_resource_posture`). An unscoped credential is reported as ``scoped: False`` —
    that is the loudest possible state, because an unscoped credential is not governed at all.

    ``require_consent`` is what the grant ASKS FOR. ``gate_registered`` is whether the machinery
    that honours it is loaded (see :func:`_gate_registered`). **``consent_enforced`` is the only
    one of the three an operator should act on**, because it is the conjunction — and on
    2026-07-29 the first was ``True`` while the second was ``False`` on a live public site, so
    reporting the request alone actively misled. A caller reading ``require_consent`` and
    concluding "gated" is making the same mistake this endpoint exists to prevent.
    """
    user = frappe.session.user
    registered = _gate_registered()
    name = frappe.db.get_value(SCOPE_DOCTYPE, {"user": user}, "name")
    if not name:
        return {"guard_version": __version__, "user": user, "scoped": False,
                "require_consent": False, "gate_registered": registered,
                "consent_enforced": False, "resource_posture": "unknown"}
    value = frappe.db.get_value(SCOPE_DOCTYPE, name, "require_consent")
    return {"guard_version": __version__, "user": user, "scoped": True,
            "require_consent": bool(value), "gate_registered": registered,
            "consent_enforced": bool(value) and registered,
            "resource_posture": _resource_posture(user)}
