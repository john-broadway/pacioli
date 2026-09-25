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
# ---- the stage runner: plain python, EXIT-CODED. `bench console` is IPython: it swallows
# SystemExit/ValueError with a printed traceback and exits 0 at EOF, so a stage could mark itself
# done having created nothing (lens, 2026-09-07), and it journals EVERY input line — passwords
# included — to logs/ipython.log and ~/.ipython/history.sqlite, world-readable under the o+x home.
# Here any exception is a non-zero exit and nothing is journaled. The body arrives on stdin; bash
# expands $VARS before python sees it, so secrets stay OUT of the body (g6 reads its password from
# a 600 file). cwd must be the bench's sites/ dir — frappe resolves the site relative to it.
console(){
  local f rc=0; f=$(mktemp "$STAGEDIR/stage-XXXXXX.py") || return 1
  cat >"$f"; chown frappe:frappe "$f"; chmod 600 "$f"
  su - frappe -c "cd $BENCH/sites && exec $BENCH/env/bin/python - <<'RUN'
import frappe
frappe.init('$SITE'); frappe.connect(); frappe.set_user('Administrator')
try:
    exec(compile(open('$f', encoding='utf-8').read(), 'stage', 'exec'), {'frappe': frappe, '__name__': '__stage__'})
    frappe.db.commit()
finally:
    frappe.destroy()
RUN" || rc=$?
  rm -f "$f"; return $rc
}

# lists -> frappe-readable staging (console runs as frappe; /root is closed to it)
STAGEDIR=/home/frappe/.pacioli-deploy; mkdir -p "$STAGEDIR"
for f in scope-methods.list scope-doctypes.list seat-read-doctypes.list \
         scope-graph-doctypes.list seat-transitive-grants.list; do
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
if n == 0 or frappe.db.get_single_value("Global Defaults", "default_company") != "$COMPANY_NAME":
    raise SystemExit("XX company not bootstrapped: accounts=%s" % n)
print("COMPANY_READY", "$COMPANY_NAME", "| accounts", n, "| default", frappe.db.get_single_value("Global Defaults", "default_company"))
PY
  echo "ok COMPANY_BOOTSTRAPPED"
  done_mark g2-company
fi

# ---- g2b: the fiscal year (no voucher can post without one; the 07-17 live build created it by
# hand and this road never did — a wizard-less install ships ZERO Fiscal Years). Data-driven from
# FISCAL_YEAR_START=MM-DD in deploy.env (01-01 = calendar year, the US small-company default).
# Creates the year that holds today AND the next one, idempotent by name. Getting the start wrong
# is a migration once the first voucher exists — set it before, not after.
if ! skip g2b-fiscal-year; then mark g2b-fiscal-year
  case "${FISCAL_YEAR_START:-}" in
    [0-1][0-9]-[0-3][0-9]) : ;;
    *) echo "XX FISCAL_YEAR_START must be MM-DD (deploy.env); got '${FISCAL_YEAR_START:-}'"; exit 2 ;;
  esac
  # the pattern admits 02-30 and 13-01; a calendar must agree (2001 = non-leap, so 02-29 refuses too)
  date -d "2001-$FISCAL_YEAR_START" >/dev/null 2>&1 || { echo "XX FISCAL_YEAR_START '$FISCAL_YEAR_START' is not a calendar date"; exit 2; }
  console <<PY
import datetime as _dt
from frappe.utils import getdate, today as _site_today
mm, dd = (int(x) for x in "$FISCAL_YEAR_START".split("-"))
today = getdate(_site_today())   # the SITE's date (System Settings time_zone), not the CT's UTC clock
start = _dt.date(today.year, mm, dd)
if start > today:
    start = _dt.date(today.year - 1, mm, dd)
made = []
for _ in range(2):
    end = _dt.date(start.year + 1, mm, dd) - _dt.timedelta(days=1)
    name = str(start.year) if (mm, dd) == (1, 1) else "%d-%d" % (start.year, end.year)
    if not frappe.db.exists("Fiscal Year", name):
        frappe.get_doc({"doctype": "Fiscal Year", "year": name,
                        "year_start_date": start, "year_end_date": end}).insert()
        made.append(name)
    start = end + _dt.timedelta(days=1)
frappe.db.commit()
rows = frappe.get_all("Fiscal Year", fields=["name", "year_start_date", "year_end_date"], order_by="year_start_date")
if len(rows) < 2 or not any(r.year_start_date <= today <= r.year_end_date for r in rows):
    raise SystemExit("XX fiscal years not in place: %r" % [r.name for r in rows])
print("FISCAL_YEARS", [(r.name, str(r.year_start_date), str(r.year_end_date)) for r in rows], "| created", made)
PY
  echo "ok FISCAL_YEAR_READY (start $FISCAL_YEAR_START; current + next)"
  done_mark g2b-fiscal-year
fi

# ---- g3: the tight seat (user + dedicated read-role + api keys; NO manager roles) ----
if ! skip g3-seat; then mark g3-seat
  console <<PY
user = "$SEAT_USER"
read_doctypes = [l.strip() for l in open("$STAGEDIR/seat-read-doctypes.list")]
if not frappe.db.exists("Role", "Pacioli Seat"):
    frappe.get_doc({"doctype": "Role", "role_name": "Pacioli Seat", "desk_access": 0}).insert()
from frappe.permissions import setup_custom_perms
for dt in read_doctypes:
    # THE FIRST-CUSTOM-ROW TRAP applies HERE too (lens 2026-09-07; g3b below documents it): the
    # moment one Custom DocPerm row exists for Company / GL Entry / Accounts Settings / Workflow,
    # frappe drops that doctype's ENTIRE standard permission set — every human role's read on the
    # ledger and the company, gone. Invisible on every prior build because every human was
    # Administrator. deploy/bench/mint-broker-seat.py has done this since 07-26; this road had not.
    if not frappe.db.exists("Custom DocPerm", {"parent": dt}):
        setup_custom_perms(dt)
    if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "Pacioli Seat"}):
        frappe.get_doc({"doctype": "Custom DocPerm", "parent": dt, "parenttype": "DocType",
                        "parentfield": "permissions", "role": "Pacioli Seat", "read": 1,
                        "permlevel": 0}).insert()
frappe.db.commit()
for dt in read_doctypes:
    frappe.clear_cache(doctype=dt)
    if not any(p.role != "Pacioli Seat" and p.read for p in frappe.get_meta(dt, cached=False).permissions):
        raise SystemExit("XX first-custom-row trap fired on %s: no human role reads it any more. Way out (plain python, as frappe): from frappe.permissions import reset_perms; reset_perms(%r); frappe.db.commit() -- then rerun this stage" % (dt, dt))
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

# ---- g3b: governed-row + transitive Frappe RBAC (the 2026-07-22 live-prove fold-in) ----
# The 07-21 lab lesson made law: at 51 doctypes spanning stock/assets/manufacturing, "Accounts
# User" standard perms do NOT cover the governed rows — every governed doctype needs a full-verb
# Pacioli Seat DocPerm, and ERPNext's own machinery needs the transitive grants (see
# seat-transitive-grants.list). THE FIRST-CUSTOM-ROW TRAP, handled here and documented in
# DEPLOY.md: the moment ONE Custom DocPerm row exists for a doctype, frappe DROPS that doctype's
# ENTIRE standard permission set — setup_custom_perms() materializes the standard rows as custom
# FIRST, so nothing existing is lost; and the role was already granted to the seat in g3, so the
# interim 403 window (37/38 doctypes dark on the lab, 2026-07-21) cannot occur.
if ! skip g3b-governed-perms; then mark g3b-governed-perms
  console <<PY
from frappe.permissions import setup_custom_perms
read_set = {l.strip() for l in open("$STAGEDIR/seat-read-doctypes.list")}
governed = [l.strip() for l in open("$STAGEDIR/scope-doctypes.list") if l.strip() not in read_set]
FULL = {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 1}
def grant(dt, verbs):
    if not frappe.db.exists("Custom DocPerm", {"parent": dt}):
        setup_custom_perms(dt)   # materialize standard rows BEFORE the first custom row
    if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "Pacioli Seat"}):
        frappe.get_doc({"doctype": "Custom DocPerm", "parent": dt, "parenttype": "DocType",
                        "parentfield": "permissions", "role": "Pacioli Seat", "permlevel": 0,
                        **verbs}).insert()
for dt in governed:
    grant(dt, FULL)
for line in open("$STAGEDIR/seat-transitive-grants.list"):
    dt, _, verbs = line.partition(":")
    grant(dt.strip(), {v.strip(): 1 for v in verbs.split(",") if v.strip()})
frappe.db.commit()
n = frappe.db.count("Custom DocPerm", {"role": "Pacioli Seat"})
print("GOVERNED_PERMS_SET | governed", len(governed), "| seat DocPerm rows", n)
PY
  echo "ok GOVERNED_PERMS_APPLIED (full-verb governed rows + transitive grants; trap handled)"
  done_mark g3b-governed-perms
fi

# ---- g4: the scope (deny-by-default floor bound to the seat; data-driven) ----
if ! skip g4-scope; then mark g4-scope
  console <<PY
user = "$SEAT_USER"
methods = [l.strip() for l in open("$STAGEDIR/scope-methods.list")]
doctypes = [l.strip() for l in open("$STAGEDIR/scope-doctypes.list")]
graph = [l.strip() for l in open("$STAGEDIR/scope-graph-doctypes.list")]
read_set = {l.strip() for l in open("$STAGEDIR/seat-read-doctypes.list")}
# THE SCOPE-METHODS RIPPLE, closed structurally (the twice-live-confirmed gap: the allowlist
# "never grew past the founding-four snapshot" — 2026-07-21 run-1, then Budget.submit refused
# 2026-07-22): the guard resolves every item-URL run_method call to "<DocType>.<verb>"
# (pacioli_guard scope.py, _classify_full), so every governed doctype needs its own
# .submit/.cancel pattern. GENERATED from the doctypes list here — landings ripple by data,
# never by hand-editing a methods file. Graph participants get .cancel ONLY (the cascade
# executor's vector; never .submit — a participant is not a governed row).
for dt in doctypes:
    if dt not in read_set:
        methods += [dt + ".submit", dt + ".cancel"]
for dt in graph:
    methods.append(dt + ".cancel")
seen = set()
methods = [m for m in methods if not (m in seen or seen.add(m))]
if frappe.db.exists("API Key Scope", {"user": user}):
    s = frappe.get_doc("API Key Scope", {"user": user})
    s.set("methods", []); s.set("resource_doctypes", [])
else:
    s = frappe.get_doc({"doctype": "API Key Scope", "user": user})
s.enabled = 1
s.allow_resource = 1   # the master Check DEFAULTS 0 — without it every resource call refuses.
# Since guard 0.8.0 the Check is necessary and NOT sufficient: an EMPTY resource_doctypes table
# now denies too (it used to mean every DocType on the site). The explicit rows appended below
# are what actually grants. A literal "*" row is the stated opt-in to site-wide access.
s.verb_read = 1; s.verb_create = 1; s.verb_write = 0; s.verb_delete = 0
for m in methods:  s.append("methods", {"pattern": m})
for d in doctypes + graph: s.append("resource_doctypes", {"ref_doctype": d})
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

# ---- g6: desk logins (humans). The seat is an API credential, never a login; people who need the
# desk are declared as data: DESK_USERS="login|First|Last|Role1,Role2;login2|..." in deploy.env.
# Passwords are GENERATED here, land root-only 600 in /root/erp-desk-logins.env, and cross to the
# stage as a frappe-owned 600 file that is deleted after — never in the stage body (a traceback
# would print it), never journaled (the runner is plain python). Welcome mail is OFF — the login
# id is an id, not a mailbox. Each login is PROVEN: the stored password authenticates, and the
# human can read Company (and report on GL Entry when they hold the approver role). With the SoD workflow above, a company needs TWO humans holding
# $WORKFLOW_APPROVER_ROLE or nothing a person drafts can ever be approved (self-approval is OFF).
if ! skip g6-desk-users; then mark g6-desk-users
  rm -f "$STAGEDIR/.pw"   # a stage killed mid-loop can leave one behind
  if [ -z "${DESK_USERS:-}" ]; then
    echo "!! DESK_USERS empty — no human logins created (Administrator only). Set it and rerun to add them."
    # deliberately NOT marked done: the rerun the message promises must actually run
  else
    LOGINS=/root/erp-desk-logins.env
    [ -f "$LOGINS" ] || { umask 077; : >"$LOGINS"; chmod 600 "$LOGINS"; umask 022; }
    IFS=';' read -ra _users <<<"$DESK_USERS"
    for spec in "${_users[@]}"; do
      IFS='|' read -r login first last roles <<<"$spec"
      [ -n "$login" ] && [ -n "$first" ] && [ -n "$roles" ] || { echo "XX DESK_USERS entry malformed: '$spec' (login|First|Last|Role1,Role2)"; exit 2; }
      if grep -q "^$login=" "$LOGINS"; then
        pw=$(grep "^$login=" "$LOGINS" | head -1 | cut -d= -f2-)
      else
        pw=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)
        printf '%s=%s\n' "$login" "$pw" >>"$LOGINS"
      fi
      (umask 077; printf '%s' "$pw" >"$STAGEDIR/.pw"); chown frappe:frappe "$STAGEDIR/.pw"; chmod 600 "$STAGEDIR/.pw"
      console <<PY || { rm -f "$STAGEDIR/.pw"; echo "XX desk user $login did not land"; exit 1; }
from frappe.utils.password import update_password, check_password
login, first, last = "$login", "$first", "$last"
roles = [r.strip() for r in "$roles".split(",") if r.strip()]
for r in roles:
    if not frappe.db.exists("Role", r):
        raise SystemExit("XX unknown role %r for %s" % (r, login))
if not frappe.db.exists("User", login):
    frappe.get_doc({"doctype": "User", "email": login, "first_name": first, "last_name": last,
                    "user_type": "System User", "send_welcome_email": 0, "enabled": 1}).insert()
u = frappe.get_doc("User", login)
have = {r.role for r in u.roles}
for r in roles:
    if r not in have:
        u.append("roles", {"role": r})
u.save()
pw = open("$STAGEDIR/.pw").read()
update_password(login, pw)
frappe.db.commit()
check_password(login, pw)   # raises AuthenticationError unless the stored password authenticates
u.reload()
for dt in ("Company", "Fiscal Year"):
    if not frappe.has_permission(dt, "read", user=login):
        raise SystemExit("XX %s cannot read %s — the first-custom-row trap (g3) or a missing role" % (login, dt))
if "Accounts Manager" in roles and not frappe.has_permission("GL Entry", "report", user=login):
    raise SystemExit("XX %s holds Accounts Manager but cannot run ledger reports" % login)
print("DESK_USER_READY", login, "| roles", sorted(r.role for r in u.roles), "| login proven | password: /root/erp-desk-logins.env (600), never printed")
PY
      rm -f "$STAGEDIR/.pw"
    done
    approvers=0
    for spec in "${_users[@]}"; do IFS='|' read -r _ _ _ roles <<<"$spec"; roles=${roles//, /,}; case ",$roles," in *",$WORKFLOW_APPROVER_ROLE,"*) approvers=$((approvers+1));; esac; done
    if [ "$approvers" -lt 2 ]; then
      echo "!! only $approvers desk login(s) hold $WORKFLOW_APPROVER_ROLE — a HUMAN-drafted invoice can never be approved by someone other than its author (self-approval is OFF); seat-drafted invoices need one. Add a second approver or accept Administrator as the break-glass."
    fi
    echo "ok DESK_USERS_CREATED (passwords: /root/erp-desk-logins.env, root-only; hand them over out of band; approvers=$approvers)"
    done_mark g6-desk-users
  fi
fi

echo "### GOVERN_DONE $(date -u) — next: perimeter.sh, then instruments.sh on the broker host"
