#!/usr/bin/env bash
# pacioli deploy stage 2/4 — GOVERN: guard day-one, the books, the tight seat, the workflow.
#
# Runs ON the target host as root, after provision.sh. Staged/resumable.
# Extracted from the proven 2026-07 live build (B3/B4/B5/B8). The order matters:
#   guard FIRST (the credential floor exists before any credential does),
#   then the company, then the seat + its scope, then the workflow.
#
# Scope is DATA, not folklore: scope-methods.list + scope-doctypes.list +
# seat-read-doctypes.list ship next to this script (one entry per line, # comments ok).
set -Eeuo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] || { echo "XX copy deploy.env.example -> deploy.env first"; exit 2; }
. ./deploy.env

exec > >(tee -a /root/pacioli-govern.log) 2>&1
STAGE=init; trap 'echo "### FAILED stage=[$STAGE] line=$LINENO rc=$?"' ERR
MARKS=/root/.pacioli-deploy-marks; mkdir -p "$MARKS"
mark(){ STAGE="$1"; echo "### STAGE $1 $(date -u +%H:%M:%S)"; }
done_mark(){ touch "$MARKS/$1"; }
skip(){ [ -f "$MARKS/$1" ] && { echo "### STAGE $1 already done — skip"; return 0; } || return 1; }

BENCH=/home/frappe/frappe-bench
SITE="$ERP_SITE"
console(){ su - frappe -c "cd $BENCH && bench --site $SITE console"; }

# lists -> frappe-readable staging (console runs as frappe; /root is closed to it)
STAGEDIR=/home/frappe/.pacioli-deploy; mkdir -p "$STAGEDIR"
for f in scope-methods.list scope-doctypes.list seat-read-doctypes.list; do
  [ -f "$f" ] || { echo "XX missing $f (scope is data — ship it next to this script)"; exit 2; }
  grep -v '^\s*#' "$f" | grep -v '^\s*$' > "$STAGEDIR/$f"
done
chown -R frappe:frappe "$STAGEDIR"

# ---- g1: guard from day one ----
if ! skip g1-guard; then mark g1-guard
  [ -n "${GUARD_WHEEL:-}" ] && [ -f "$GUARD_WHEEL" ] || { echo "XX GUARD_WHEEL not set or missing (deploy.env)"; exit 2; }
  # keep the wheel's REAL filename — pip refuses a wheel whose name doesn't carry the
  # full name-version-abi convention (the lab proof caught a rename to 'pacioli_guard.whl')
  WHL=/home/frappe/$(basename "$GUARD_WHEEL")
  cp "$GUARD_WHEEL" "$WHL"; chown frappe:frappe "$WHL"
  su - frappe -c "$BENCH/env/bin/pip install $WHL"
  # apps.txt append — NEWLINE-SAFE (a missing trailing newline once glued two app names)
  APPS=$BENCH/sites/apps.txt
  grep -qw pacioli_guard "$APPS" || { tail -c1 "$APPS" | od -An -c | grep -q '\\n' || echo >> "$APPS"; echo pacioli_guard >> "$APPS"; }
  su - frappe -c "cd $BENCH && (bench --site $SITE list-apps | grep -qw pacioli_guard || bench --site $SITE install-app pacioli_guard) && bench --site $SITE migrate"
  systemctl restart frappe-bench.target   # fresh workers pick up the guard's hooks
  echo "ok GUARD_INSTALLED"
  done_mark g1-guard
fi

# ---- g2: the books (wizard-less installs lack the Transit fixture Company links to) ----
if ! skip g2-company; then mark g2-company
  console <<PY
if not frappe.db.exists("Warehouse Type", "Transit"):
    frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
if not frappe.db.exists("Company", "$COMPANY_NAME"):
    frappe.get_doc({"doctype": "Company", "company_name": "$COMPANY_NAME",
                    "abbr": "$COMPANY_ABBR", "default_currency": "$COMPANY_CURRENCY",
                    "country": "$COMPANY_COUNTRY"}).insert()
frappe.db.commit()
gd = frappe.get_doc("Global Defaults"); gd.default_company = "$COMPANY_NAME"; gd.save(); frappe.db.commit()
n = frappe.db.count("Account", {"company": "$COMPANY_NAME"})
print("COMPANY_READY", "$COMPANY_NAME", "| accounts", n, "| default", frappe.db.get_single_value("Global Defaults", "default_company"))
PY
  echo "ok COMPANY_BOOTSTRAPPED"
  done_mark g2-company
fi

# ---- g3: the tight seat (user + dedicated read-role + api keys; NO manager roles) ----
if ! skip g3-seat; then mark g3-seat
  console <<PY
user = "$SEAT_USER"
read_doctypes = [l.strip() for l in open("$STAGEDIR/seat-read-doctypes.list")]
if not frappe.db.exists("Role", "Pacioli Seat"):
    frappe.get_doc({"doctype": "Role", "role_name": "Pacioli Seat", "desk_access": 0}).insert()
for dt in read_doctypes:
    if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "Pacioli Seat"}):
        frappe.get_doc({"doctype": "Custom DocPerm", "parent": dt, "parenttype": "DocType",
                        "parentfield": "permissions", "role": "Pacioli Seat", "read": 1,
                        "permlevel": 0}).insert()
if not frappe.db.exists("User", user):
    frappe.get_doc({"doctype": "User", "email": user, "first_name": "Pacioli",
                    "last_name": "Seat", "user_type": "System User",
                    "send_welcome_email": 0}).insert()
u = frappe.get_doc("User", user)
have = {r.role for r in u.roles}
for r in ("Accounts User", "Pacioli Seat"):
    if r not in have:
        u.append("roles", {"role": r})
u.save()
from frappe.core.doctype.user.user import generate_keys
secret = generate_keys(user)["api_secret"]
u.reload()
frappe.db.commit()
import os
os.makedirs("/home/frappe/pacioli-seat", exist_ok=True)
p = "/home/frappe/pacioli-seat/seat.secret"
with open(p, "w") as f: f.write(secret)
os.chmod(p, 0o600)
print("SEAT_READY", user, "| api_key", u.api_key, "| roles", sorted(r.role for r in u.roles))
print("secret landed at", p, "(600, frappe-owned) — CARRY it to the broker host; it is never printed")
PY
  echo "ok SEAT_CREATED (no Accounts Manager, no System Manager — the doctor certifies this later)"
  done_mark g3-seat
fi

# ---- g4: the scope (deny-by-default floor bound to the seat; data-driven) ----
if ! skip g4-scope; then mark g4-scope
  console <<PY
user = "$SEAT_USER"
methods = [l.strip() for l in open("$STAGEDIR/scope-methods.list")]
doctypes = [l.strip() for l in open("$STAGEDIR/scope-doctypes.list")]
if frappe.db.exists("API Key Scope", {"user": user}):
    s = frappe.get_doc("API Key Scope", {"user": user})
    s.set("methods", []); s.set("resource_doctypes", [])
else:
    s = frappe.get_doc({"doctype": "API Key Scope", "user": user})
s.enabled = 1
s.allow_resource = 1   # the master Check DEFAULTS 0 — without it every resource call refuses
s.verb_read = 1; s.verb_create = 1; s.verb_write = 0; s.verb_delete = 0
for m in methods:  s.append("methods", {"pattern": m})
for d in doctypes: s.append("resource_doctypes", {"ref_doctype": d})
s.save() if s.name else s.insert()
frappe.db.commit()
s.reload()
print("SCOPE_SET", user, "| methods", len(s.methods), "| doctypes", len(s.resource_doctypes),
      "| verbs r%dc%dw%dd%d" % (s.verb_read, s.verb_create, s.verb_write, s.verb_delete))
PY
  echo "ok SCOPE_APPLIED"
  done_mark g4-scope
fi

# ---- g5: the workflow (separation of duties; MASTERS FIRST — the API-side ordering lesson) ----
if ! skip g5-workflow; then mark g5-workflow
  console <<PY
for st in ("Draft", "Pending Approval", "Approved"):
    if not frappe.db.exists("Workflow State", st):
        frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": st}).insert()
for ac in ("Request Approval", "Approve"):
    if not frappe.db.exists("Workflow Action Master", ac):
        frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": ac}).insert()
if not frappe.db.exists("Workflow", "SI Approval"):
    frappe.get_doc({"doctype": "Workflow", "workflow_name": "SI Approval",
        "document_type": "Sales Invoice", "workflow_state_field": "workflow_state",
        "is_active": 1,
        "states": [
            {"state": "Draft", "doc_status": "0", "allow_edit": "Accounts User"},
            {"state": "Pending Approval", "doc_status": "0", "allow_edit": "$WORKFLOW_APPROVER_ROLE"},
            {"state": "Approved", "doc_status": "1", "allow_edit": "$WORKFLOW_APPROVER_ROLE"},
        ],
        "transitions": [
            {"state": "Draft", "action": "Request Approval", "next_state": "Pending Approval",
             "allowed": "Accounts User", "allow_self_approval": 1},
            {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
             "allowed": "$WORKFLOW_APPROVER_ROLE", "allow_self_approval": 0},
        ]}).insert()
frappe.db.commit()
w = frappe.get_doc("Workflow", "SI Approval")
print("WORKFLOW_ACTIVE", w.name, "| states", len(w.states), "| self-approval on Approve:",
      [t.allow_self_approval for t in w.transitions if t.action == "Approve"])
PY
  echo "ok WORKFLOW_BOOTSTRAPPED (self-approval OFF on Approve)"
  done_mark g5-workflow
fi

echo "### GOVERN_DONE $(date -u) — next: perimeter.sh, then instruments.sh on the broker host"
