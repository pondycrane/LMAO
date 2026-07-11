#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# LMAO Local Registry Helper
#
# Builds, tags, and pushes LMAO Docker images to the local registry.
# Also generates a K3s registries.yaml config for cluster nodes.
#
# Usage:
#   ./docker/registry/manage.sh start        # Start the registry
#   ./docker/registry/manage.sh stop         # Stop the registry
#   ./docker/registry/manage.sh push         # Build & push all images
#   ./docker/registry/manage.sh push-server  # Build & push lmao-server only
#   ./docker/registry/manage.sh push-ingest  # Build & push lmao-iot-ingest only
#   ./docker/registry/manage.sh list         # List images in the registry
#   ./docker/registry/manage.sh k3s-config   # Print K3s registries.yaml
#   ./docker/registry/manage.sh status       # Check registry health
#
# Config:
#   REGISTRY_HOST  - Registry host (default: 192.168.0.36)
#   REGISTRY_PORT  - Registry port (default: 5000)
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGISTRY_HOST="${REGISTRY_HOST:-192.168.0.36}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
REGISTRY="${REGISTRY_HOST}:${REGISTRY_PORT}"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Check prerequisites ──
check_prereqs() {
    if ! command -v docker &>/dev/null; then
        error "Docker not found. Install with: apt-get install docker.io"
        exit 1
    fi
}

# ── Start the registry ──
start() {
    info "Starting local Docker registry on ${REGISTRY_HOST}:${REGISTRY_PORT}..."
    docker compose -f "${REPO_ROOT}/docker/registry/docker-compose.yml" up -d
    info "Waiting for registry to be ready..."
    sleep 3
    if curl -s "http://localhost:${REGISTRY_PORT}/v2/" > /dev/null 2>&1; then
        info "Registry is running at http://${REGISTRY_HOST}:${REGISTRY_PORT}"
        info "API:    http://${REGISTRY_HOST}:${REGISTRY_PORT}/v2/"
        info "Images: http://${REGISTRY_HOST}:${REGISTRY_PORT}/v2/_catalog"
    else
        error "Registry failed to start. Check docker logs:"
        docker compose -f "${REPO_ROOT}/docker/registry/docker-compose.yml" logs
        exit 1
    fi
}

# ── Stop the registry ──
stop() {
    info "Stopping local Docker registry..."
    docker compose -f "${REPO_ROOT}/docker/registry/docker-compose.yml" down
    info "Registry stopped."
}

# ── Build and push an image ──
push_image() {
    local image_name="$1"   # e.g. lmao-server
    local dockerfile="$2"   # e.g. Dockerfile or Dockerfile.iot-ingest
    local registry_ref="${REGISTRY}/${image_name}:latest"

    info "Building ${image_name} from ${dockerfile}..."
    docker build -f "${REPO_ROOT}/${dockerfile}" -t "${image_name}:latest" "${REPO_ROOT}"

    info "Tagging ${image_name}:latest → ${registry_ref}..."
    docker tag "${image_name}:latest" "${registry_ref}"

    info "Pushing ${registry_ref}..."
    docker push "${registry_ref}"

    info "Done: ${registry_ref}"
}

# ── Push lmao-server ──
push_server() {
    push_image "lmao-server" "Dockerfile"
}

# ── Push lmao-iot-ingest ──
push_ingest() {
    push_image "lmao-iot-ingest" "Dockerfile.iot-ingest"
}

# ── Push all LMAO images ──
push_all() {
    push_server
    push_ingest
    info "All images pushed to ${REGISTRY}"
    info "Catalog: http://${REGISTRY}/v2/_catalog"
}

# ── List images in the registry ──
list_images() {
    info "Images in local registry at ${REGISTRY}:"
    local catalog
    catalog="$(curl -sf "http://${REGISTRY}/v2/_catalog" || echo '{"repositories":[]}')"
    local repos
    repos="$(echo "$catalog" | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f'  • {r}') for r in data.get('repositories',[])]" 2>/dev/null || echo "  (empty or unreachable)")"
    echo "$repos"
    echo ""
    # Show tags for each repo
    echo "$catalog" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for r in data.get('repositories', []):
    print(r)
" 2>/dev/null | while IFS= read -r repo; do
        local tags
        tags="$(curl -sf "http://${REGISTRY}/v2/${repo}/tags/list" 2>/dev/null | python3 -c "
import sys,json
data = json.load(sys.stdin)
print(', '.join(data.get('tags', [])))
" 2>/dev/null || echo "no tags")"
        echo "    ${repo}: ${tags}"
    done
}

# ── Print K3s registries.yaml ──
k3s_config() {
    cat <<YAML
# K3s registries.yaml — configure cluster nodes to use the local registry.
#
# Place this file on each K3s node at /etc/rancher/k3s/registries.yaml,
# then restart K3s:
#   sudo systemctl restart k3s       # on control-plane nodes
#   sudo systemctl restart k3s-agent # on worker nodes
#
# The local registry runs on ${REGISTRY_HOST}:${REGISTRY_PORT}
# as an insecure (HTTP) registry.

mirrors:
  ${REGISTRY_HOST}:${REGISTRY_PORT}:
    endpoint:
      - "http://${REGISTRY_HOST}:${REGISTRY_PORT}"
  docker.io:
    endpoint:
      - "https://registry-1.docker.io"
YAML
}

# ── Registry health check ──
status() {
    info "Registry status:"
    local running
    running="$(docker ps --filter name=lmao-registry --format '{{.Status}}' 2>/dev/null || true)"
    if [ -n "$running" ]; then
        echo "  Container: ${GREEN}running${NC} — ${running}"
    else
        echo "  Container: ${RED}NOT running${NC}"
    fi

    if curl -sf "http://localhost:${REGISTRY_PORT}/v2/" > /dev/null 2>&1; then
        echo "  API:       ${GREEN}responding${NC} at http://localhost:${REGISTRY_PORT}/v2/"
    else
        echo "  API:       ${RED}not responding${NC}"
    fi

    echo ""
    list_images

    echo ""
    info "Volume usage:"
    docker volume inspect registry-data 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        mp = d[0].get('Mountpoint', 'unknown')
        print(f'  Mountpoint: {mp}')
    else:
        print('  (no data)')
except:
    print('  (no data)')
" 2>/dev/null || echo "  Volume does not exist"
}

# ── Main dispatch ──
main() {
    check_prereqs

    case "${1:-help}" in
        start)
            start
            ;;
        stop)
            stop
            ;;
        push)
            push_all
            ;;
        push-server)
            push_server
            ;;
        push-ingest)
            push_ingest
            ;;
        list)
            list_images
            ;;
        k3s-config)
            k3s_config
            ;;
        status)
            status
            ;;
        *)
            echo "Usage: $0 <command>"
            echo ""
            echo "Commands:"
            echo "  start        Start the local Docker registry"
            echo "  stop         Stop the local Docker registry"
            echo "  push         Build & push all LMAO images to the registry"
            echo "  push-server  Build & push lmao-server only"
            echo "  push-ingest  Build & push lmao-iot-ingest only"
            echo "  list         List images in the registry"
            echo "  k3s-config   Print K3s registries.yaml for cluster nodes"
            echo "  status       Check registry health"
            echo ""
            echo "Environment:"
            echo "  REGISTRY_HOST  (default: 192.168.0.36)"
            echo "  REGISTRY_PORT  (default: 5000)"
            ;;
    esac
}

main "$@"