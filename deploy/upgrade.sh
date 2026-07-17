#!/usr/bin/env bash
# pacioli deploy — UPGRADE: `bench update` under the unit split, the doctor as the gate.
#
# Runs ON the target host as root, AFTER a host-level snapshot (your hypervisor's hand —
# this script cannot verify one exists, so it refuses to run without the --snapshotted
# attestation). Drilled 2026-07-17 against a paved-road install (run record:
# broker/docs/plans/2026-07-17-upgrade-drill.md).
#
# Seams the drill found, baked in here so nobody rediscovers them:
#   - `bench build` dirties apps/erpnext/banking/yarn.lock IN-TREE; an undiscarded
#     lockfile makes `bench update` refuse the pull as "local changes in app erpnext".
#     Discard build drift with a targeted checkout — NOT `bench update --reset`,
#     which hard-resets every app and would eat a change that was actually yours.
#   - node/yarn live under nvm: every bench call must ride `source ~/.frappe_env`,
#     or the build step dies with FileNotFoundError: 'yarn'.
#   - stop the five SERVING units, keep the two redis units up — migrate needs
#     redis-cache alive; "stop the target" would take redis down with it. Workers must
#     NOT run during migrate (old code against a mid-migration schema).
#   - provision.sh sets restart_{supervisor,systemd}_on_update=false from birth, so
#     bench update mutates code+schema+assets only; THIS script owns the restart.
#   - exit codes are captured INSIDE the su shell — `| tee` outside it eats them.
#
# After this script: run the gate from the BROKER host —
#   pacioli doctor --target <name>   -> must say `ready.`
#   pacioli verify / anchor check / close --respond
# The upgrade is not done until the doctor says it is.
set -Eeuo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] || { echo "XX copy deploy.env.example -> deploy.env first"; exit 2; }
. ./deploy.env

[ "${1:-}" = "--snapshotted" ] || {
  echo "XX refuse: take a host-level snapshot of this guest first (your hypervisor's"
  echo "   hand), then re-run:  $0 --snapshotted"
  echo "   The snapshot is the UNDO for a schema migration; there is no bench-level undo."
  exit 2
}

exec > >(tee -a /root/pacioli-upgrade.log) 2>&1
STAGE=init; trap 'echo "### FAILED stage=[$STAGE] line=$LINENO rc=$?"' ERR
mark(){ STAGE="$1"; echo "### STAGE $1 $(date -u +%H:%M:%S)"; }

BENCH=/home/frappe/frappe-bench
SITE="$ERP_SITE"
SERVING="frappe-web frappe-socketio frappe-worker-short frappe-worker-long frappe-schedule"

mark u1-preflight
systemctl is-active --quiet frappe-bench.target || { echo "XX frappe-bench.target not active — upgrade a running box"; exit 1; }
AVAIL=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
[ "$AVAIL" -ge 5 ] || { echo "XX <5G free on / — backups + assets need headroom"; exit 1; }
echo "== versions before =="
su - frappe -c "source ~/.frappe_env && cd $BENCH && bench version"

mark u2-discard-build-drift
su - frappe -c "cd $BENCH/apps/erpnext && git checkout -- banking/yarn.lock 2>/dev/null || true"
DIRTY=$(su - frappe -c "cd $BENCH/apps/erpnext && git status --porcelain; cd ../frappe && git status --porcelain")
[ -z "$DIRTY" ] || { echo "XX apps still dirty after drift discard — a REAL local change exists; rule on it by hand:"; echo "$DIRTY"; exit 1; }

mark u3-stop-serving   # redis stays up: migrate needs it; workers must not see mid-migration schema
systemctl stop $SERVING

mark u4-bench-update   # backup+pull+requirements+patch+build; no restart (config false from birth)
RC=$(su - frappe -c "source ~/.frappe_env && cd $BENCH && bench update >/home/frappe/last-bench-update.log 2>&1; echo \$?")
su - frappe -c "tail -5 /home/frappe/last-bench-update.log"
[ "$RC" = 0 ] || { echo "XX bench update rc=$RC — read /home/frappe/last-bench-update.log; guest snapshot is the UNDO"; exit 1; }

mark u5-discard-build-drift-again   # the build re-dirties the lockfile every run
su - frappe -c "cd $BENCH/apps/erpnext && git checkout -- banking/yarn.lock 2>/dev/null || true"

mark u6-restart-and-readback
systemctl start $SERVING
CODE=""
for i in $(seq 1 24); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Frappe-Site-Name: $SITE" http://127.0.0.1:8000/api/method/ping || true)
  [ "$CODE" = 200 ] && break; sleep 5
done
[ "$CODE" = 200 ] || { echo "XX gunicorn not answering 200 after restart"; exit 1; }
CODE_FRONT=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE" http://127.0.0.1/api/method/ping || true)
[ "$CODE_FRONT" = 200 ] || { echo "XX nginx front not answering 200"; exit 1; }

echo "== versions after =="
su - frappe -c "source ~/.frappe_env && cd $BENCH && bench version"
echo "### UPGRADE_DONE on the target — now run the gate from the broker host:"
echo "###   pacioli doctor --target <name>   (must say: ready.)"
echo "###   pacioli verify / anchor check / close --respond"
