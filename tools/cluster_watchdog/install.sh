#!/usr/bin/env bash
# Install the LMAO cluster watchdog on this host (selfhost) as a systemd timer.
#
#   sudo bash tools/cluster_watchdog/install.sh          # install + enable
#   sudo bash tools/cluster_watchdog/install.sh --remove # uninstall
#
# The unit runs as $SUDO_USER (the account that owns the kubeconfig, ssh keys
# and the headless agent), not as root: the watchdog needs `kubectl`, `ssh` and
# `omp`, all of which live in that user's environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN_USER="${SUDO_USER:-${USER}}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
UNIT_DIR="/etc/systemd/system"
ENV_DIR="$RUN_HOME/.config/lmao"
ENV_FILE="$ENV_DIR/cluster-watchdog.env"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run with sudo (writes $UNIT_DIR)" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now cluster-watchdog.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/cluster-watchdog.timer" "$UNIT_DIR/cluster-watchdog.service"
    systemctl daemon-reload
    echo "removed cluster-watchdog.timer (config kept at $ENV_FILE)"
    exit 0
fi

install -d -o "$RUN_USER" -g "$RUN_USER" "$ENV_DIR" "$RUN_HOME/.local/state/lmao-watchdog"
if [ ! -f "$ENV_FILE" ]; then
    install -o "$RUN_USER" -g "$RUN_USER" -m 0600 \
        "$SCRIPT_DIR/watchdog.env.example" "$ENV_FILE"
    echo "wrote $ENV_FILE (edit EXPECTED_DEST_HASH / ALERT_TARGET)"
fi

cat >"$UNIT_DIR/cluster-watchdog.service" <<UNIT
[Unit]
Description=LMAO cluster watchdog — deterministic checks + agentic recovery
Documentation=file://$REPO_ROOT/tools/cluster_watchdog/runbook.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$SCRIPT_DIR
Environment=HOME=$RUN_HOME
Environment=WATCHDOG_ENV=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$RUN_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 $SCRIPT_DIR/watchdog.py watch
TimeoutStartSec=900
Nice=10
UNIT

cat >"$UNIT_DIR/cluster-watchdog.timer" <<'UNIT'
[Unit]
Description=Periodically probe the LMAO cluster and self-recover

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now cluster-watchdog.timer
systemctl start cluster-watchdog.service || true

echo
systemctl --no-pager status cluster-watchdog.timer | head -5
echo
echo "First run output:"
journalctl -u cluster-watchdog.service -n 30 --no-pager
