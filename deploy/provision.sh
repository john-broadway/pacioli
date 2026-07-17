#!/usr/bin/env bash
# pacioli deploy stage 1/4 — PROVISION: blank Debian 13 host -> vanilla ERPNext, production-served.
#
# Runs ON the target host as root. Staged/resumable (rerun-safe; stages skip when done).
# Extracted from the proven 2026-07 live build (erp-install-live.sh + the perimeter stage's
# engine correction) — this stage lands gunicorn+systemd units DIRECTLY: the dev server
# (Werkzeug) never serves, not even for a day. Run records: broker/docs/plans/2026-07-17-*.
#
# Secrets: GENERATED HERE, root-only 600, never echoed, never defaults.
# End state: `frappe-bench.target` serving :8000 (gunicorn) + :9000 (socketio), API answers
# 200 by site name AND by IP (the gunicorn_live.py single-site pin — v16 resolves HTTP site
# as `_site -> X-Frappe-Site-Name -> Host` ONLY; nothing else is consulted).
set -Eeuo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] || { echo "XX copy deploy.env.example -> deploy.env and fill it first"; exit 2; }
. ./deploy.env

exec > >(tee -a /root/pacioli-provision.log) 2>&1
STAGE=init
trap 'echo "### FAILED stage=[$STAGE] line=$LINENO rc=$?"' ERR
MARKS=/root/.pacioli-deploy-marks; mkdir -p "$MARKS"
mark(){ STAGE="$1"; echo "### STAGE $1 $(date -u +%H:%M:%S)"; }
done_mark(){ touch "$MARKS/$1"; }
skip(){ [ -f "$MARKS/$1" ] && { echo "### STAGE $1 already done — skip"; return 0; } || return 1; }

SITE="$ERP_SITE"
SECRETS=/root/erp-secrets.env
if [ -f "$SECRETS" ]; then . "$SECRETS"; else
  ADMIN_PW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  DB_ROOT_PW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  umask 077; printf 'ADMIN_PW=%s\nDB_ROOT_PW=%s\n' "$ADMIN_PW" "$DB_ROOT_PW" >"$SECRETS"
  chmod 600 "$SECRETS"; umask 022
fi
export DEBIAN_FRONTEND=noninteractive

if ! skip apt-deps; then mark apt-deps
  # fresh Debian boxes: unattended-upgrades grabs the dpkg lock and wedges installs
  systemctl mask --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
  apt-get update -y
  apt-get install -y git curl wget build-essential ca-certificates pkg-config \
    python3-dev python3-venv python3-pip pipx \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev tk-dev libncurses-dev xz-utils \
    mariadb-server mariadb-client libmariadb-dev \
    redis-server sudo cron nginx
  done_mark apt-deps
fi

if ! skip wkhtmltopdf; then mark wkhtmltopdf
  ( ARCH=$(dpkg --print-architecture); curl -fsSL -o /tmp/wk.deb "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb" \
    && apt-get install -y /tmp/wk.deb ) || echo "wkhtmltopdf best-effort skipped (PDF print unavailable)"
  done_mark wkhtmltopdf
fi

if ! skip frappe-user; then mark frappe-user
  id frappe &>/dev/null || useradd -m -s /bin/bash frappe
  echo 'frappe ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/frappe; chmod 440 /etc/sudoers.d/frappe
  # deb13 homes are 0700 — nginx (stage 3) must traverse or every static asset 404s
  chmod o+x /home/frappe
  done_mark frappe-user
fi

if ! skip mariadb; then mark mariadb
  cat >/etc/mysql/mariadb.conf.d/99-frappe.cnf <<'CNF'
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci
[mysql]
default-character-set = utf8mb4
CNF
  (systemctl enable --now mariadb || service mariadb start); sleep 4
  mariadb <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('$DB_ROOT_PW') OR unix_socket;
FLUSH PRIVILEGES;
SQL
  (systemctl restart mariadb || service mariadb restart); sleep 4
  done_mark mariadb
fi

if ! skip redis-off; then mark redis-off
  # bench brings its own redis pair (systemd units below); the distro daemon is noise
  systemctl disable --now redis-server 2>/dev/null || true
  done_mark redis-off
fi

if ! skip write-steps; then mark write-steps
  cat >/home/frappe/.frappe_env <<'ENV'
export PYENV_ROOT=$HOME/.pyenv
export NVM_DIR=$HOME/.nvm
export PATH=$HOME/.local/bin:$PYENV_ROOT/bin:$PATH
eval "$(pyenv init - bash 2>/dev/null)" || true
[ -s $NVM_DIR/nvm.sh ] && . $NVM_DIR/nvm.sh && nvm use default >/dev/null 2>&1 || true
ENV

  cat >/home/frappe/s1_python.sh <<'S1'
set -e
cd "$HOME"
export PYENV_ROOT=$HOME/.pyenv
[ -d $PYENV_ROOT/.git ] || git clone --depth 1 https://github.com/pyenv/pyenv.git $PYENV_ROOT
export PATH=$PYENV_ROOT/bin:$PATH
eval "$(pyenv init - bash)"
PYV=$(pyenv install --list | tr -d ' ' | grep -E '^3\.14\.[0-9]+$' | tail -1)
echo "python target: $PYV"
pyenv install -s "$PYV"
pyenv global "$PYV"
python --version
S1

  cat >/home/frappe/s2_node.sh <<'S2'
set -e
cd "$HOME"
export NVM_DIR=$HOME/.nvm
if [ ! -s $NVM_DIR/nvm.sh ]; then
  for i in 1 2 3 4 5; do
    rm -rf $NVM_DIR; mkdir -p $NVM_DIR
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash && [ -s $NVM_DIR/nvm.sh ] && break
    echo "nvm install attempt $i failed (transient); retry in 6s"; sleep 6
  done
fi
[ -s $NVM_DIR/nvm.sh ]
. $NVM_DIR/nvm.sh
for i in 1 2 3 4 5; do nvm install 24 && break; echo "nvm install 24 attempt $i failed; retry"; sleep 6; done
nvm alias default 24
corepack enable 2>/dev/null || true
command -v yarn >/dev/null || npm install -g yarn
node --version; yarn --version
S2

  cat >/home/frappe/s3_bench.sh <<'S3'
set -e
cd "$HOME"
export PATH=$HOME/.local/bin:$PATH
pipx install frappe-bench 2>/dev/null || pip install --user --break-system-packages frappe-bench
command -v uv >/dev/null || pipx install uv 2>/dev/null || pip install --user --break-system-packages uv
~/.local/bin/bench --version
S3

  cat >/home/frappe/s4_init.sh <<'S4'
set -e
source ~/.frappe_env
cd $HOME
PYBIN=$(pyenv which python)
echo "init with $PYBIN ($($PYBIN --version)), node $(node --version)"
[ -d frappe-bench/apps/frappe ] || { rm -rf frappe-bench; bench init --frappe-branch __BRANCH__ --python "$PYBIN" frappe-bench; }
S4

  cat >/home/frappe/s5_site.sh <<'S5'
set -e
source ~/.frappe_env
cd $HOME/frappe-bench
for cfg in config/redis_cache.conf config/redis_queue.conf; do [ -f "$cfg" ] && redis-server "$cfg" --daemonize yes 2>/dev/null || true; done; sleep 2
CFG=sites/__SITE__/site_config.json
getcfg(){ python3 -c "import json;print(json.load(open('$CFG')).get('$1',''))" 2>/dev/null; }
site_ok(){ [ -f "$CFG" ] || return 1; local n p; n=$(getcfg db_name); p=$(getcfg db_password); [ -n "$n" ] && mariadb -u "$n" -p"$p" "$n" -e "SELECT 1" >/dev/null 2>&1 && bench --site __SITE__ list-apps 2>/dev/null | grep -qw erpnext; }
if site_ok; then
  echo "site db user auth OK — keeping site"
else
  echo "site missing/broken — hard reset + recreate"
  [ -d sites/__SITE__ ] && bench drop-site __SITE__ --db-root-password __DBPW__ --no-backup --force 2>/dev/null || true
  rm -rf sites/__SITE__
  bench new-site __SITE__ --no-mariadb-socket --mariadb-root-password __DBPW__ --admin-password __ADMPW__
  # force the fresh site user to password auth (defeats unix_socket 1698 when bench runs as frappe)
  N=$(getcfg db_name); P=$(getcfg db_password)
  mariadb -u root -p__DBPW__ -e "ALTER USER '$N'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('$P'); FLUSH PRIVILEGES;" 2>/dev/null || true
  mariadb -u root -p__DBPW__ -e "ALTER USER '$N'@'%' IDENTIFIED VIA mysql_native_password USING PASSWORD('$P'); FLUSH PRIVILEGES;" 2>/dev/null || true
fi
S5
  sed -i "s/__DBPW__/$DB_ROOT_PW/g; s/__ADMPW__/$ADMIN_PW/g" /home/frappe/s5_site.sh

  cat >/home/frappe/s6_erpnext.sh <<'S6'
set -e
source ~/.frappe_env
cd $HOME/frappe-bench
for cfg in config/redis_cache.conf config/redis_queue.conf; do [ -f "$cfg" ] && redis-server "$cfg" --daemonize yes 2>/dev/null || true; done; sleep 2
[ -d apps/erpnext ] || bench get-app --branch __BRANCH__ erpnext
bench --site __SITE__ list-apps | grep -qw erpnext || bench --site __SITE__ install-app erpnext
S6

  cat >/home/frappe/s7_configure.sh <<'S7'
set -e
source ~/.frappe_env
cd $HOME/frappe-bench
bench use __SITE__
echo "__SITE__" > sites/currentsite.txt   # CLI default (NOT consulted for HTTP in v16)
bench set-config -g developer_mode 0 || true
bench --site __SITE__ enable-scheduler
echo "import frappe; frappe.db.set_single_value('System Settings','time_zone','__TZ__'); frappe.db.commit(); print('TZ_SET', frappe.db.get_single_value('System Settings','time_zone'))" | bench --site __SITE__ console
$HOME/frappe-bench/env/bin/pip show gunicorn >/dev/null || $HOME/frappe-bench/env/bin/pip install gunicorn
S7

  sed -i "s/__SITE__/$SITE/g; s|__TZ__|$SITE_TZ|g; s/__BRANCH__/$FRAPPE_BRANCH/g" \
    /home/frappe/s4_init.sh /home/frappe/s5_site.sh /home/frappe/s6_erpnext.sh /home/frappe/s7_configure.sh
  chown -R frappe:frappe /home/frappe
  done_mark write-steps
fi

cd /home/frappe
if ! skip s1; then mark pyenv-python;  sudo -u frappe -H bash /home/frappe/s1_python.sh;    done_mark s1; fi
if ! skip s2; then mark node24;        sudo -u frappe -H bash /home/frappe/s2_node.sh;      done_mark s2; fi
if ! skip s3; then mark bench-cli;     sudo -u frappe -H bash /home/frappe/s3_bench.sh;     done_mark s3; fi
if ! skip s4; then mark bench-init;    sudo -u frappe -H bash /home/frappe/s4_init.sh;      done_mark s4; fi
if ! skip s5; then mark new-site;      sudo -u frappe -H bash /home/frappe/s5_site.sh;      done_mark s5; fi
if ! skip s6; then mark get-erpnext;   sudo -u frappe -H bash /home/frappe/s6_erpnext.sh;   done_mark s6; fi
if ! skip s7; then mark configure;     sudo -u frappe -H bash /home/frappe/s7_configure.sh; done_mark s7; fi

# ---- production serving: systemd unit split, gunicorn from birth ----
if ! skip units; then mark units
  BENCH=/home/frappe/frappe-bench
  NODE=$(su - frappe -c 'source ~/.frappe_env && which node')
  BENCH_CLI=$(su - frappe -c 'source ~/.frappe_env && which bench')
  [ -x "$NODE" ] && [ -x "$BENCH_CLI" ] || { echo "XX node/bench not found via .frappe_env"; exit 1; }

  # the single-site pin: same variable bench serve sets (frappe.app._site). Without it,
  # gunicorn 404s any request whose Host isn't the site dir name — including a broker
  # talking by IP. v16 consults NOTHING else for this (read from source, cost 2 cutovers).
  cat >$BENCH/config/gunicorn_live.py <<PIN
def post_fork(server, worker):
    import frappe.app
    frappe.app._site = "$SITE"
PIN
  chown frappe:frappe $BENCH/config/gunicorn_live.py

  unit(){ cat >"/etc/systemd/system/$1"; }
  unit frappe-redis-cache.service <<EOF
[Unit]
Description=Frappe redis cache
After=network.target
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=/usr/bin/redis-server $BENCH/config/redis_cache.conf --daemonize no
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-redis-queue.service <<EOF
[Unit]
Description=Frappe redis queue
After=network.target
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=/usr/bin/redis-server $BENCH/config/redis_queue.conf --daemonize no
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-web.service <<EOF
[Unit]
Description=Frappe web (gunicorn)
After=network.target mariadb.service frappe-redis-cache.service frappe-redis-queue.service
Wants=frappe-redis-cache.service frappe-redis-queue.service
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH/sites
ExecStart=$BENCH/env/bin/gunicorn -c $BENCH/config/gunicorn_live.py -b 0.0.0.0:8000 \\
  -w 3 --worker-class gthread --threads 4 \\
  -t 120 --max-requests 5000 --max-requests-jitter 500 --preload frappe.app:application
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-socketio.service <<EOF
[Unit]
Description=Frappe socketio
After=network.target frappe-redis-queue.service
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=$NODE $BENCH/apps/frappe/socketio.js
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-worker-short.service <<EOF
[Unit]
Description=Frappe worker (short,default)
After=network.target mariadb.service frappe-redis-queue.service
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=/bin/bash -c 'source /home/frappe/.frappe_env && exec $BENCH_CLI worker --queue short,default'
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-worker-long.service <<EOF
[Unit]
Description=Frappe worker (long)
After=network.target mariadb.service frappe-redis-queue.service
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=/bin/bash -c 'source /home/frappe/.frappe_env && exec $BENCH_CLI worker --queue long'
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-schedule.service <<EOF
[Unit]
Description=Frappe scheduler
After=network.target mariadb.service frappe-redis-queue.service
PartOf=frappe-bench.target
[Service]
User=frappe
WorkingDirectory=$BENCH
ExecStart=/bin/bash -c 'source /home/frappe/.frappe_env && exec $BENCH_CLI schedule'
Restart=on-failure
[Install]
WantedBy=frappe-bench.target
EOF
  unit frappe-bench.target <<EOF
[Unit]
Description=Frappe bench (production: gunicorn + socketio + workers + scheduler + redis)
Wants=frappe-redis-cache.service frappe-redis-queue.service frappe-web.service \\
 frappe-socketio.service frappe-worker-short.service frappe-worker-long.service \\
 frappe-schedule.service
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload

  # stop any ad-hoc serving from the site steps (redis daemonized by ports, no pkill —
  # pkill over ssh kills the session when the pattern matches the remote command line)
  command -v redis-cli >/dev/null && for p in 13000 11000; do redis-cli -p $p shutdown nosave 2>/dev/null || true; done
  systemctl enable frappe-bench.target
  systemctl start frappe-bench.target
  done_mark units
fi

mark verify
ok=""; code=000
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H "Host: $SITE" http://127.0.0.1:8000/api/method/ping || true)
  [ "$code" = "200" ] && { ok=1; break; }; sleep 2
done
[ -n "$ok" ] || { echo "XX by-name ping never answered (last $code) — journalctl -u frappe-web"; exit 1; }
ipcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/method/ping || true)
[ "$ipcode" = "200" ] || { echo "XX by-IP answered $ipcode — the single-site pin is not holding"; exit 1; }
hdr=$(curl -s -o /dev/null -w '%header{server}' --max-time 5 http://127.0.0.1:8000/api/method/ping)
case "$hdr" in *gunicorn*) : ;; *) echo "XX engine is '$hdr', expected gunicorn"; exit 1;; esac
echo "ok PING_BY_NAME + PING_BY_IP, engine=gunicorn"
echo "### PROVISION_DONE $(date -u)"
