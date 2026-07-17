#!/usr/bin/env bash
# dump-live-scope.sh — read-only: print the proven install's guard scope as kit data.
# Run against the reference install (operator's hand); paste the METHOD/DOCTYPE blocks
# into scope-methods.list / scope-doctypes.list verbatim. No secrets touched or printed.
# Usage: bash dump-live-scope.sh <host> <site> <seat-user>
set -euo pipefail
[ $# -eq 3 ] || { echo "usage: dump-live-scope.sh <host> <site> <seat-user>"; exit 2; }
HOST=$1; SITE=$2; SEAT=$3

ssh "root@$HOST" 'su - frappe -c "cd ~/frappe-bench && bench --site '"$SITE"' console"' <<PY
s = frappe.get_doc("API Key Scope", {"user": "$SEAT"})
print("== scope-methods.list ==")
for m in s.methods: print(m.pattern)
print("== scope-doctypes.list ==")
for d in s.resource_doctypes: print(d.ref_doctype)
print("== flags ==")
print("enabled", s.enabled, "| allow_resource", s.allow_resource,
      "| verbs r%dc%dw%dd%d" % (s.verb_read, s.verb_create, s.verb_write, s.verb_delete),
      "| enforce_workflow", getattr(s, "enforce_workflow", 0),
      "| rate_limit_per_minute", getattr(s, "rate_limit_per_minute", 0))
print("== seat-read-doctypes.list (Pacioli Seat role grants) ==")
print("\n".join(p.parent for p in frappe.get_all("Custom DocPerm", {"role": "Pacioli Seat"}, ["parent"], order_by="parent")))
PY
