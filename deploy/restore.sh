#!/usr/bin/env bash
# pacioli deploy — RESTORE: bring the books back from a bench-backup triple, doctor as the gate.
#
# Runs ON the target host as root. Drilled 2026-07-17: database DROPPED and on-box backups
# deleted, then restored from the off-box copy alone — 97/97 accounts back, doctor `ready.`,
# chain still matching the anchor (run record: broker/docs/plans/2026-07-17-restore-drill.md).
#
# Seams the drill found, baked in here:
#   - bench backups land ON the box (`sites/<site>/private/backups/`) — a real disaster
#     takes them with it. CARRY EVERY BACKUP OFF-BOX (sha-checked); this script restores
#     from files you carried back. Host-layer loss (whole guest) is the hypervisor's
#     restore path (snapshot rollback / PBS) — drilled separately, not this script's job.
#   - `bench restore` leaves the SCHEDULER DISABLED (its safety) — a silently-degraded
#     site if nobody knows. This script re-enables it and reads the state back.
#   - mariadb root comes from the provision-generated secrets env BY REFERENCE
#     (never typed, never echoed).
#   - serving units stop, redis stays; restart + 200 through gunicorn AND nginx.
#
# Usage: restore.sh <database.sql.gz> [public-files.tar] [private-files.tar]
# After: run the gate from the BROKER host — doctor must say `ready.`, anchor must match.
set -Eeuo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] || { echo "XX copy deploy.env.example -> deploy.env first"; exit 2; }
. ./deploy.env
SECRETS_ENV=${SECRETS_ENV:-/root/erp-secrets.env}
[ -f "$SECRETS_ENV" ] || { echo "XX $SECRETS_ENV missing (provision generates it); set SECRETS_ENV="; exit 2; }

DBFILE=${1:-}; PUBTAR=${2:-}; PRIVTAR=${3:-}
[ -n "$DBFILE" ] && [ -f "$DBFILE" ] || { echo "XX usage: $0 <database.sql.gz> [public.tar] [private.tar]"; exit 2; }

exec > >(tee -a /root/pacioli-restore.log) 2>&1
STAGE=init; trap 'echo "### FAILED stage=[$STAGE] line=$LINENO rc=$?"' ERR
mark(){ STAGE="$1"; echo "### STAGE $1 $(date -u +%H:%M:%S)"; }

BENCH=/home/frappe/frappe-bench
SITE="$ERP_SITE"
SERVING="frappe-web frappe-socketio frappe-worker-short frappe-worker-long frappe-schedule"

mark r1-stage-files   # frappe must be able to read them
STAGE_DIR=/home/frappe/restore-in; mkdir -p "$STAGE_DIR"
cp -f "$DBFILE" "$STAGE_DIR/"; DB_BASE=$(basename "$DBFILE")
ARGS="$STAGE_DIR/$DB_BASE"
[ -n "$PUBTAR" ]  && { cp -f "$PUBTAR"  "$STAGE_DIR/"; ARGS="$ARGS --with-public-files $STAGE_DIR/$(basename "$PUBTAR")"; }
[ -n "$PRIVTAR" ] && { cp -f "$PRIVTAR" "$STAGE_DIR/"; ARGS="$ARGS --with-private-files $STAGE_DIR/$(basename "$PRIVTAR")"; }
chown -R frappe:frappe "$STAGE_DIR"

mark r2-stop-serving   # redis stays up; workers must not touch a mid-restore DB
systemctl stop $SERVING

mark r3-restore        # mariadb root by reference from the secrets env — never typed
set -a; . "$SECRETS_ENV"; set +a
RC=$(su frappe -c "source ~/.frappe_env && cd $BENCH && bench --site $SITE restore $ARGS --mariadb-root-password \"\$DB_ROOT_PW\" >/home/frappe/last-restore.log 2>&1; echo \$?" )
su - frappe -c "tail -3 /home/frappe/last-restore.log"
[ "$RC" = 0 ] || { echo "XX bench restore rc=$RC — read /home/frappe/last-restore.log"; exit 1; }

mark r4-scheduler-and-migrate   # restore leaves the scheduler DISABLED — re-enable, read back
SCHED=$(su frappe -c "source ~/.frappe_env && cd $BENCH && bench --site $SITE scheduler enable >/dev/null 2>&1; bench --site $SITE scheduler status")
case "$SCHED" in *enabled*) : ;; *) echo "XX scheduler not enabled after restore: $SCHED"; exit 1;; esac
su frappe -c "source ~/.frappe_env && cd $BENCH && bench --site $SITE migrate >/home/frappe/last-restore-migrate.log 2>&1"

mark r5-restart-and-readback
systemctl start $SERVING
CODE=""
for i in $(seq 1 24); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Frappe-Site-Name: $SITE" http://127.0.0.1:8000/api/method/ping || true)
  [ "$CODE" = 200 ] && break; sleep 5
done
[ "$CODE" = 200 ] || { echo "XX gunicorn not answering 200 after restore"; exit 1; }
CODE_FRONT=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE" http://127.0.0.1/api/method/ping || true)
[ "$CODE_FRONT" = 200 ] || { echo "XX nginx front not answering 200"; exit 1; }

echo "### RESTORE_DONE on the target — now run the gate from the broker host:"
echo "###   pacioli doctor --target <name>   (must say: ready.)"
echo "###   pacioli verify / anchor check / close --respond"
