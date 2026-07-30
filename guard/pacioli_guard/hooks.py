app_name = "pacioli_guard"
app_title = "Pacioli Guard"
app_publisher = "John Broadway"
app_description = (
    "Least-privilege API capability scoping for Frappe/ERPNext — bind a credential to an "
    "allowlist of methods and DocTypes, enforced at the credential layer. No core fork."
)
app_email = "271895126+john-broadway@users.noreply.github.com"
app_license = "apache-2.0"

# TWO extension points, because they answer two different questions at the two altitudes where
# those questions are legible. Both are frappe's own public hooks — no core fork either way.
#
#   auth_hooks  — WHICH CREDENTIAL is acting, and therefore what it may call at all. Only knowable
#                 at authentication time, so it can only live here. Runs after the api-key
#                 authenticates and before dispatch.
#   doc_events  — WHETHER THIS ACT WAS CONSENTED. A property of a document, so it belongs on the
#                 document. Registered on "*" (frappe uses that wildcard itself, see frappe's own
#                 hooks.py) because the gate is keyed on the acting principal's grant, not on a
#                 fixed list of doctypes: a governed seat must not be able to slip through by
#                 touching a doctype nobody thought to enumerate.
#
# The consent gate deliberately does NOT also run in auth_hooks. Two gates cannot both hold a
# single-use marker — whichever ran first would spend it and the second would refuse a burned one.
# One act, one claim, one place. See pacioli_guard/act.py for the altitude reasoning and for what
# still walks around BOTH layers (writes that skip the document lifecycle entirely).
auth_hooks = ["pacioli_guard.enforce.check_scope"]

doc_events = {
    "*": {
        "before_submit": "pacioli_guard.act.before_submit",
        "before_cancel": "pacioli_guard.act.before_cancel",
        # NOT a gate. `after_insert` decides nothing and refuses nothing — it records that a
        # document was created inside an act that already established consent, so that the
        # `before_submit` gate can tell "the act made this" from "the caller named this" when
        # ERPNext creates and submits in two separate calls. Added 0.10.0; before it, every
        # `doc.save(); doc.submit()` cascade inside a governed act was REFUSED. See act.py.
        "after_insert": "pacioli_guard.act.after_insert",
        # NOT gates on a posting — gates on a REHEARSAL of one. ERPNext previews a posting by
        # performing it and rolling back (`stock_controller.py:2058-2066`), so with consent enforced
        # the preview's own cascade was refused and the broker's PLAN step could not complete. The
        # preview now requires the same marker as the submit it previews, and does not spend it.
        # frappe fires these through `run_method`, which composes `doc_events` exactly as the
        # lifecycle events above (`model/document.py:1252`). Added 0.13.0.
        "before_gl_preview": "pacioli_guard.act.before_gl_preview",
        "before_sl_preview": "pacioli_guard.act.before_sl_preview",
    }
}
