#!/usr/bin/env bash
# Pacioli bench receipts — run these against ANY ERPNext site, ours or yours.
#
# Nothing here is special to our bench. Point it at a site with pacioli-guard installed,
# give it two credentials, and it prints what the floor does. If your numbers differ from
# ours, that is the useful outcome and worth posting.
#
#   SITE=https://your-erpnext.example \
#   SEAT_KEY=<api_key:api_secret of a guard-scoped read-only credential> \
#   BORROWED_KEY=<api_key:api_secret of an ordinary unscoped credential> \
#   bash receipts.sh
#
# BORROWED_KEY is optional; without it the borrowed-key section is skipped rather than faked.
#
# SITE is REQUIRED and deliberately has no default. This script sends your credential in an
# Authorization header and performs a write probe, so a default host would mean anyone who
# exported SEAT_KEY and forgot SITE would hand their key to whoever owned that default and
# take an unsolicited write there. A script handed to strangers does not get to guess.
set -uo pipefail

SITE="${SITE:?set SITE=https://your-erpnext.example — no default: this sends your credential}"
SEAT_KEY="${SEAT_KEY:?set SEAT_KEY=api_key:api_secret}"
BORROWED_KEY="${BORROWED_KEY:-}"

rd() {  # read: $1=key $2=doctype
  curl -s -o /dev/null -w "%{http_code}" --max-time 25 \
    -H "Authorization: token $1" "$SITE/api/resource/${2// /%20}?limit_page_length=1"
}
wr() {  # write: $1=key
  curl -s -o /dev/null -w "%{http_code}" --max-time 25 -X POST \
    -H "Authorization: token $1" -H "Content-Type: application/json" \
    -d '{"customer_name":"receipt-probe","customer_group":"Commercial","territory":"All Territories"}' \
    "$SITE/api/resource/Customer"
}
row() { printf "  %-34s %s\n" "$1" "$2"; }

echo
echo "site: $SITE"
echo "date: $(date -u +'%Y-%m-%d %H:%M UTC')"
echo
echo "── the scoped seat ──────────────────────────────────────────"
echo "  a read-only credential bound to a guard scope. this is what a"
echo "  visiting agent holds."
row "read  Customer            (in scope)" "$(rd "$SEAT_KEY" Customer)"
# EXTRA_DOCTYPE lets you name one of your OWN in-scope doctypes. Unset, this row is skipped
# rather than probing a doctype your site has no reason to own — on our bench it is a synthetic
# custom doctype, and hardcoding it here would print a 404 on your site and read as a refusal.
if [ -n "${EXTRA_DOCTYPE:-}" ]; then
  row "read  $EXTRA_DOCTYPE (in scope)" "$(rd "$SEAT_KEY" "$EXTRA_DOCTYPE")"
fi
row "read  Journal Entry       (NOT in scope)" "$(rd "$SEAT_KEY" "Journal Entry")"
row "WRITE Customer            (verb denied)" "$(wr "$SEAT_KEY")"
echo
echo "  the last two are the point. the credential authenticated fine."
echo "  it was refused on what it was not scoped for, not on who it is."
echo

if [ -n "$BORROWED_KEY" ]; then
echo "── the borrowed over-broad key ──────────────────────────────"
echo "  one ordinary integration credential. sales roles, nothing exotic."
echo "  run this section BEFORE binding a scope to it, then again after."
row "read  Customer" "$(rd "$BORROWED_KEY" Customer)"
row "WRITE Customer" "$(wr "$BORROWED_KEY")"
echo
echo "  unbound, that key writes because its user can write. that is not a"
echo "  bug in erpnext, it is what an api credential means today. bind the"
echo "  same key to a read-only scope and the write refuses while the read"
echo "  keeps working. same key. same call. the floor is the only change."
echo
fi

echo "── what this does NOT prove ─────────────────────────────────"
echo "  refusals are half the story. the other half is a governed write:"
echo "  plan, consent, prove, with a receipt you can check afterward."
echo "  that runs separately and its output belongs in the thread too."
echo
