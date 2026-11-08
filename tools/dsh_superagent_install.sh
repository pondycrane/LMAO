#!/usr/bin/env bash
# Install the LMAO dsh-superagent deployment on this host (issue #303).
#
# Runs the DeepSeek Harness superagent profile as a systemd service under a
# dedicated OS account, wired to the LOCAL TensorFold model endpoint and to the
# bench service (cluster-ops/benchd on op4) for device verification.
#
# Idempotent: every step checks first and skips what is already in place. An
# existing profile's cordis.patch.yml is never overwritten unless
# --force-config is passed (it holds machine configuration).
#
#   sudo bash tools/dsh_superagent_install.sh
#   sudo bash tools/dsh_superagent_install.sh --force-config
#
# The GitHub agent account is an operator step: after installing, sign the
# wrapper in once, then re-run the settings tool (see docs/superagent.md).
set -euo pipefail

AGENT_USER="${DSH_SUPERAGENT_USER:-dsh-agent}"
NODE_VERSION="${DSH_NODE_VERSION:-24.22.2}"
DSH_VERSION="${DSH_SUPERAGENT_DSH_VERSION:-0.2.1-rc.3}"
PNPM_VERSION="${DSH_SUPERAGENT_PNPM_VERSION:-11.7.0}"
SUPERAGENT_REPO="${DSH_SUPERAGENT_REPO:-https://github.com/deepseek-ai/dsh-superagent-prototype.git}"
SUPERAGENT_COMMIT="${DSH_SUPERAGENT_COMMIT:-8c32a9dc5d72c033816c6a8110b37f40cf94fba4}"
REPOSITORY="${DSH_SUPERAGENT_REPOSITORY:-pondycrane/LMAO}"
MODEL_BASE_URL="${DSH_SUPERAGENT_MODEL_BASE_URL:-http://spark-c34f:8888/v1}"
MODEL_ID="${DSH_SUPERAGENT_MODEL_ID:-DeepSeek-V4.1-Flash-1017}"
BENCH_URL="${BENCH_URL:-http://op4:8099}"
BENCH_CLIENT="${BENCH_CLIENT:-/home/pondycrane/cluster-ops/benchd/bench}"
PORT="${DSH_SUPERAGENT_PORT:-3082}"
FORCE_CONFIG=0

for arg in "$@"; do
  case "$arg" in
    --force-config) FORCE_CONFIG=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo bash $0" >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
AGENT_HOME="/home/${AGENT_USER}"
# The agent account cannot read the operator's checkout; every command below
# runs from its own home.
cd "$AGENT_HOME"
SUPERAGENT_DIR="${AGENT_HOME}/src/dsh-superagent"
CLONE="${AGENT_HOME}/src/${REPOSITORY##*/}"
STATE_DIR="${AGENT_HOME}/.dsh/state/superagent"

as_agent() { runuser -u "$AGENT_USER" -- env HOME="$AGENT_HOME" PATH="/opt/node24/bin:${AGENT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
step() { printf '\n== %s\n' "$*"; }

step "OS account ${AGENT_USER} (dedicated: workers run with danger-full-access)"
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$AGENT_USER"
fi
id "$AGENT_USER"

step "Node ${NODE_VERSION} (dsh needs >= 22.19; the 24 line is the pinned LTS)"
case "$(uname -m)" in
  aarch64|arm64) NODE_ARCH=arm64 ;;
  x86_64|amd64) NODE_ARCH=x64 ;;
  *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
NODE_DIR="/opt/node-v${NODE_VERSION}-linux-${NODE_ARCH}"
if [ ! -x "${NODE_DIR}/bin/node" ]; then
  tmp=$(mktemp -d)
  curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o "${tmp}/node.tar.xz"
  tar xJf "${tmp}/node.tar.xz" -C /opt
  rm -rf "$tmp"
fi
ln -sfn "$NODE_DIR" /opt/node24
/opt/node24/bin/node --version

step "pnpm ${PNPM_VERSION} + dsh ${DSH_VERSION} for ${AGENT_USER}"
as_agent /opt/node24/bin/npm config set prefix "${AGENT_HOME}/.local" >/dev/null
if ! as_agent test -x "${AGENT_HOME}/.local/bin/pnpm"; then
  as_agent /opt/node24/bin/npm install -g "pnpm@${PNPM_VERSION}" "@deepseek-ai/dsh@${DSH_VERSION}"
fi
as_agent "${AGENT_HOME}/.local/bin/pnpm" --version
as_agent "${AGENT_HOME}/.local/bin/dsh" --version

step "dsh-superagent checkout at ${SUPERAGENT_COMMIT}"
if [ ! -d "${SUPERAGENT_DIR}/.git" ]; then
  as_agent git clone "$SUPERAGENT_REPO" "$SUPERAGENT_DIR"
fi
as_agent git -C "$SUPERAGENT_DIR" fetch --tags origin
as_agent git -C "$SUPERAGENT_DIR" checkout -q "$SUPERAGENT_COMMIT"
if [ ! -f "${SUPERAGENT_DIR}/lib/index.js" ]; then
  as_agent "${AGENT_HOME}/.local/bin/pnpm" install --frozen-lockfile --dir "$SUPERAGENT_DIR"
  as_agent "${AGENT_HOME}/.local/bin/pnpm" build --dir "$SUPERAGENT_DIR"
fi

step "profile superagent (created from the Web template, bundle linked)"
mkdir -p "$STATE_DIR"
chown -R "$AGENT_USER:$AGENT_USER" "${AGENT_HOME}/.dsh"
if [ ! -f "${AGENT_HOME}/.dsh/profiles/superagent/cordis.patch.yml" ]; then
  ( cd "$SUPERAGENT_DIR" && as_agent "${AGENT_HOME}/.local/bin/dsh" superagent --from-default-profile web --dump-config >/dev/null )
  ( cd "$SUPERAGENT_DIR" && as_agent "${AGENT_HOME}/.local/bin/dsh" plugin --profile superagent add "link:${SUPERAGENT_DIR}" )
fi

PROFILE_PATCH="${AGENT_HOME}/.dsh/profiles/superagent/cordis.patch.yml"
if [ "$FORCE_CONFIG" -eq 1 ] || ! grep -q "dsh-superagent" "$PROFILE_PATCH"; then
  step "machine configuration -> ${PROFILE_PATCH}"
  cat > "$PROFILE_PATCH" <<YAML
# Machine configuration for the LMAO dsh-superagent profile (issue #303).
# Everything here is local: the model is the TensorFold DeepSeek endpoint on
# the LAN, never api.deepseek.com.
- id: webserver
  config:
    host: 127.0.0.1
    port: ${PORT}
- id: dsh-superagent
  config:
    stateDir: ${STATE_DIR}
- id: llm-deepseek
  config:
    baseURL: ${MODEL_BASE_URL}
    apiKeyEnv: DEEPSEEK_API_KEY
    reasoningEffort: high
    maxTokens: 32768
    defaultContextWindow: 1048576
    models:
      - id: ${MODEL_ID}
        name: ${MODEL_ID} (local TensorFold)
        contextWindow: 1048576
        maxTokens: 32768
# The local endpoint is the only route: make it the default for every Agent.
- id: agent-default-model
  config:
    provider: deepseek-official
    model: ${MODEL_ID}
YAML
  chown "$AGENT_USER:$AGENT_USER" "$PROFILE_PATCH"
else
  echo "keeping existing ${PROFILE_PATCH} (pass --force-config to replace it)"
fi

step "agent CLI wrapper + bench client"
mkdir -p "${AGENT_HOME}/.local/bin" "${AGENT_HOME}/.config/gh-agent"
chmod 700 "${AGENT_HOME}/.config/gh-agent"
cat > "${AGENT_HOME}/.local/bin/gh-agent" <<'EOF'
#!/bin/sh
# GitHub CLI restricted to the agent account's own credentials (issue #303).
GH_CONFIG_DIR="$HOME/.config/gh-agent" exec gh "$@"
EOF
chmod +x "${AGENT_HOME}/.local/bin/gh-agent"
if [ -f "$BENCH_CLIENT" ]; then
  install -m 755 "$BENCH_CLIENT" "${AGENT_HOME}/.local/bin/bench"
else
  echo "WARNING: bench client not found at ${BENCH_CLIENT}; set BENCH_CLIENT to cluster-ops/benchd/bench" >&2
fi
chown -R "$AGENT_USER:$AGENT_USER" "${AGENT_HOME}/.local"

step "persistent clone for ticket worktrees: ${CLONE}"
if [ ! -d "${CLONE}/.git" ]; then
  as_agent git clone "https://github.com/${REPOSITORY}.git" "$CLONE"
else
  as_agent git -C "$CLONE" fetch --no-tags origin
fi
as_agent git -C "$CLONE" log --oneline -1

step "systemd unit + environment"
install -d -m 755 /etc/lmao
cat > /etc/lmao/dsh-superagent.env <<EOF
# Environment for dsh-superagent (LMAO issue #303); read by dsh-superagent.service.
PATH=/opt/node24/bin:${AGENT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
HOME=${AGENT_HOME}
# The local endpoint does not authenticate; any non-empty value satisfies the
# DeepSeek Messages credential reference. Not a cloud key.
DEEPSEEK_API_KEY=dummy-local-noauth
# The bench owns every attached board; workers submit jobs instead of touching
# a serial port.
BENCH_URL=${BENCH_URL}
EOF
chmod 600 /etc/lmao/dsh-superagent.env
cat > /etc/systemd/system/dsh-superagent.service <<EOF
[Unit]
Description=dsh-superagent (DeepSeek Harness superagent profile) — LMAO issue -> PR driver
Documentation=https://github.com/deepseek-ai/dsh-superagent-prototype
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${AGENT_USER}
Group=${AGENT_USER}
WorkingDirectory=${SUPERAGENT_DIR}
EnvironmentFile=/etc/lmao/dsh-superagent.env
ExecStart=${AGENT_HOME}/.local/bin/dsh --profile superagent --no-open
# Executors get up to 5 minutes to finish a turn (docs/006-deployment.md).
KillSignal=SIGTERM
TimeoutStopSec=330
KillMode=mixed
Restart=always
RestartSec=10
# The panel is loopback-only; no inbound ingress, outbound polling only.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${AGENT_HOME}/.dsh ${AGENT_HOME}/src ${AGENT_HOME}/.config/gh-agent ${AGENT_HOME}/.cache ${AGENT_HOME}/.local/share

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now dsh-superagent.service
systemctl restart dsh-superagent.service

step "wait for the runtime to answer"
for _ in $(seq 1 60); do
  if curl -fsS -m 5 -H 'x-orchestrator-request: 1' "http://127.0.0.1:${PORT}/orchestrator/api/state" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -s -m 10 -H 'x-orchestrator-request: 1' "http://127.0.0.1:${PORT}/orchestrator/api/state" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"problem":d["setup"]["problem"],"accounts":d["setup"]["accounts"],"health_runtime":d["health"]["runtime"]},indent=1))' || true

step "settings (policy, eval, executor, prompts, GitHub Source)"
DSH_SUPERAGENT_PORT="$PORT" DSH_SUPERAGENT_CLONE="$CLONE" DSH_SUPERAGENT_REPOSITORY="$REPOSITORY" \
  python3 "${SCRIPT_DIR}/dsh_superagent_settings.py" || true

cat <<EOF

Panel (loopback only — tunnel it):  ssh -N -L ${PORT}:127.0.0.1:${PORT} <this host>
  URL with a fresh token: journalctl -u dsh-superagent -n 40 | grep -o 'http://127.0.0.1:${PORT}/?token=[A-Za-z0-9_-]*' | tail -1

Remaining operator step (dedicated agent GitHub account):
  runuser -u ${AGENT_USER} -- ${AGENT_HOME}/.local/bin/gh-agent auth login
  runuser -u ${AGENT_USER} -- ${AGENT_HOME}/.local/bin/gh-agent api user --jq .login
  python3 tools/dsh_superagent_settings.py     # now saves the GitHub Source
EOF
