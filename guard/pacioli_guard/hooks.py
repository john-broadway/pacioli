app_name = "pacioli_guard"
app_title = "Pacioli Guard"
app_publisher = "John Broadway"
app_description = (
    "Least-privilege API capability scoping for Frappe/ERPNext — bind a credential to an "
    "allowlist of methods and DocTypes, enforced at the credential layer. No core fork."
)
app_email = "271895126+john-broadway@users.noreply.github.com"
app_license = "apache-2.0"

# Enforced at frappe's auth_hooks chokepoint (runs after api-key auth, before dispatch).
auth_hooks = ["pacioli_guard.enforce.check_scope"]
