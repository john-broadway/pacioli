#!/usr/bin/env bash
# pacioli deploy stage 4/4 — INSTRUMENTS: broker home, doctor gate, genesis anchor, cadence.
#
# Runs ON THE BROKER HOST (the machine that will hold the consent hand and the receipt
# chain) — NOT on the ERP target. Needs: `pip install pacioli` (or a repo venv), the seat's
# api_key (printed by govern.sh), and the seat.secret file CARRIED from the target
# (govern.sh landed it at /home/frappe/pacioli-seat/seat.secret — move it, don't copy-paste).
#
# Usage: bash instruments.sh <target-name> <erp-host-or-ip:port> <api-key> <secret-file>
# Example: bash instruments.sh prod 192.0.2.10:8000 <api-key> /root/.pacioli/live/seat.secret
#
# Definition of done for the WHOLE deploy: this script ends with `doctor -> ready.`,
# a verified genesis anchor, a clean census, and a daily cadence timer. If doctor says
# anything but ready, the refusal body names the missing grant — fix on the target, rerun.
set -Eeuo pipefail
[ $# -eq 4 ] || { echo "usage: instruments.sh <target-name> <host:port> <api-key> <secret-file>"; exit 2; }
NAME=$1; HOSTPORT=$2; API_KEY=$3; SECRET_SRC=$4
PBIN=$(command -v pacioli) || { echo "XX pacioli not on PATH (pip install pacioli)"; exit 2; }
cd "$(dirname "$0")"; [ -f deploy.env ] && . ./deploy.env || true

HOME_DIR=/root/.pacioli/$NAME
mkdir -p "$HOME_DIR/state" "$HOME_DIR/cadence"
chmod 700 /root/.pacioli "$HOME_DIR"

# the secret rides as a 600 FILE, referenced from the registry — never inline (the
# registry parser refuses an inline secret by design)
if [ "$SECRET_SRC" != "$HOME_DIR/seat.secret" ]; then
  install -m 600 "$SECRET_SRC" "$HOME_DIR/seat.secret"
fi

if [ ! -f "$HOME_DIR/targets.toml" ]; then
  cat >"$HOME_DIR/targets.toml" <<EOF
[targets.$NAME]
base_url = "http://$HOSTPORT"
allow_http = true            # by-IP on a private net; fronting TLS proxy serves browsers
api_key = "$API_KEY"
api_secret = "file:$HOME_DIR/seat.secret"
company = "${COMPANY_NAME:-}"
seat_user = "${SEAT_USER:-}"
site_tz = "${SITE_TZ:-}"
posture = "mixed_door"
default = true
EOF
  chmod 600 "$HOME_DIR/targets.toml"
fi

export PACIOLI_REGISTRY=$HOME_DIR/targets.toml
export PACIOLI_STATE_DIR=$HOME_DIR/state

echo "== the doctor is the gate =="
pacioli doctor --target "$NAME" | tee /tmp/pacioli-doctor-$NAME.out
tail -1 /tmp/pacioli-doctor-$NAME.out | grep -q "ready." || { echo "XX doctor not ready — fix the named refusal on the target, rerun"; exit 1; }

echo "== genesis anchor =="
pacioli verify --target "$NAME"
pacioli anchor write --target "$NAME" > "$HOME_DIR/$NAME.anchor"
echo "anchor written: $HOME_DIR/$NAME.anchor — CARRY A COPY OFF THIS HOST (another machine, a git remote, paper)."
echo "re-pin after every seal/unseal/close --advance/attest."

echo "== baseline census =="
pacioli close --target "$NAME" --respond || echo "!! census response raised a finding — read it; a newborn book should be clean"

echo "== daily cadence (10:00 UTC, persistent) =="
cat >"$HOME_DIR/cadence.sh" <<EOF
#!/usr/bin/env bash
# daily instrument sweep — read-only. ATTENTION file exists <=> something failed.
set -u
export PACIOLI_REGISTRY=$HOME_DIR/targets.toml
export PACIOLI_STATE_DIR=$HOME_DIR/state
# absolute path baked at INSTALL time — systemd's PATH has no venv (the lab proof
# caught a first sweep failing all six instruments on `command -v` under systemd)
P=$PBIN
OUT=$HOME_DIR/cadence
LOG="\$OUT/\$(date -u +%Y-%m-%d).log"
FAILS=""
run(){ local name=\$1; shift
  echo "== \$name ==" >>"\$LOG"
  if "\$@" >>"\$LOG" 2>&1; then echo "ok \$name" >>"\$LOG"
  else echo "XX \$name (exit \$?)" >>"\$LOG"; FAILS="\$FAILS \$name"; fi
}
echo "=== pacioli cadence \$(date -u '+%F %T UTC') ===" >>"\$LOG"
run doctor       "\$P" doctor --target $NAME
run verify       "\$P" verify --target $NAME
run anchor-check "\$P" anchor check --in $HOME_DIR/$NAME.anchor --target $NAME
run seal-status  "\$P" seal-status --target $NAME
run close-status "\$P" close-status --target $NAME
run census       "\$P" close --target $NAME --respond
find "\$OUT" -name '20*.log' -mtime +30 -delete 2>/dev/null
if [ -n "\$FAILS" ]; then
  { echo "PACIOLI CADENCE ATTENTION — \$(date -u '+%F %T UTC')"; echo "failed:\$FAILS"; echo "log: \$LOG"; } > "\$OUT/ATTENTION"
  echo "LAST \$(date -u '+%F %T UTC') FAIL:\$FAILS" > "\$OUT/LAST"; exit 1
else
  rm -f "\$OUT/ATTENTION"; echo "LAST \$(date -u '+%F %T UTC') ok" > "\$OUT/LAST"
fi
EOF
chmod 755 "$HOME_DIR/cadence.sh"

cat >/etc/systemd/system/pacioli-cadence-$NAME.service <<EOF
[Unit]
Description=Pacioli daily instrument sweep ($NAME, read-only)
[Service]
Type=oneshot
ExecStart=$HOME_DIR/cadence.sh
EOF
cat >/etc/systemd/system/pacioli-cadence-$NAME.timer <<EOF
[Unit]
Description=Daily pacioli cadence ($NAME, 10:00 UTC)
[Timer]
OnCalendar=*-*-* 10:00:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now pacioli-cadence-$NAME.timer
systemctl start pacioli-cadence-$NAME.service || true
cat "$HOME_DIR/cadence/LAST"
# the gate gates: a cadence whose first sweep failed is not "armed"
grep -q " ok$" "$HOME_DIR/cadence/LAST" || { echo "XX first cadence sweep FAILED — see $HOME_DIR/cadence/ATTENTION"; exit 1; }

echo "### INSTRUMENTS_DONE — doctor ready, anchor pinned, census run, cadence armed."
