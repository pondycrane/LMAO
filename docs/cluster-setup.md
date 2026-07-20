# Turing Pi 2 Cluster — Kubernetes Setup

**Hardware:** Turing Pi 2 board with 3x RK1 compute modules, running K3s v1.35.5+k3s1.

## Node Network

Each node has **two network interfaces**:

| Interface | Subnet | Purpose | Stable? |
|-----------|--------|---------|---------|
| `eth0` | `192.168.10.0/24` | Internal cluster network (node-to-node) | ✅ Static |
| `wlan0` | `192.168.0.0/24` | LAN / external access | ❌ DHCP (may change) |

| Hostname | Role | Internal IP (eth0) | LAN IP (wlan0) | Hardware | OS |
|----------|------|-------------------|----------------|----------|----|
| `tp3` | **control-plane** | `192.168.10.40` | `192.168.0.49` | RK1 module slot 2 | Debian 11 (bullseye), kernel 6.1.21-v8+ |
| `tp2` | worker | `192.168.10.28` | `192.168.0.43` | RK1 module slot 1 | Debian 11 (bullseye), kernel 5.15.84-v8+ |
| `tp4` | worker | `192.168.10.19` | `192.168.0.44` | RK1 module slot 3 | Debian 11 (bullseye), kernel 5.15.84-v8+ |
| `turing-bmc` | BMC | — | `192.168.0.47` | ESP32 on Turing Pi 2 | BMC firmware (default `root:turing`) |
| `selfhost` | workstation | — | `192.168.0.36` (eth0) | Raspberry Pi 5 | Ubuntu 26.04 LTS (Resolute Raccoon), aarch64 |

The workstation `selfhost` is a **separate machine** — not a cluster node.
The internal `192.168.10.x` network is **not reachable from selfhost** — only node-to-node.

---

## Cluster Infrastructure

| Namespace | Service | Type | Details |
|-----------|---------|------|---------|
| `kube-system` | **traefik** | LoadBalancer (192.168.0.43-49) | Ingress controller, v3.6.13, ports 80/443 |
| `kube-system` | **coredns** | ClusterIP (10.43.0.10) | DNS, v1.14.3 |
| `kube-system` | **local-path-provisioner** | — | Default StorageClass, local PVCs on nodes |
| `kube-system` | **metrics-server** | ClusterIP | Resource metrics, v0.8.1 |
| `default` | **nats-server** | ClusterIP (10.43.45.156:4222) | NATS 2.10 with JetStream, single replica, 1Gi PVC |
| `default` | **lmao-server** | Headless (ClusterIP: None) | External gRPC service → physical LoRa RPi (port 50051) |

### State (as of 2026-07-20)

| Pod | Status | Notes |
|-----|--------|-------|
| nats-server | ✅ Running | Healthy, 0 restarts |
| iot-ingest-consumer | ✅ Running | Healthy, 0 restarts |
| coredns | ✅ Running | Healthy |
| traefik | ✅ Running | On all 3 nodes via svclb |
| local-path-provisioner | ✅ Running | Stabilized after crash-loop fix |
| metrics-server | ✅ Running | Stabilized after crash-loop fix |

---

## Configuration: K3s Config Files

No hardcoded IP flags in systemd services. All configuration is in YAML config files.

### Control Plane (tp3)

**`/etc/rancher/k3s/config.yaml`** (see [docs/k3s-config.yaml](k3s-config.yaml)):
```yaml
tls-san:
  - 192.168.10.40     # internal (stable)
  - 192.168.0.49      # LAN (DHCP — update if changed)
  - 192.168.0.45      # legacy (TLS backwards compat)
```

**`/etc/systemd/system/k3s.service`** — now minimal:
```
ExecStart=/usr/local/bin/k3s \
    server
```

Node IP is auto-detected from `eth0`/`wlan0`.

### Workers (tp2, tp4)

**`/etc/rancher/k3s/config.yaml`** (see [docs/k3s-agent-config.yaml](k3s-agent-config.yaml)):
```yaml
node-ip: 192.168.0.43    # or 192.168.0.44 for tp4
server: https://192.168.0.49:6443
```

**`/etc/systemd/system/k3s-agent.service`** — only the token:
```
ExecStart=/usr/local/bin/k3s \
    agent \
    --token \
    K10b44...::server:...
```

### Registries

**`/etc/rancher/k3s/registries.yaml`:**
```yaml
mirrors:
  192.168.0.36:5000:
    endpoint:
      - "http://192.168.0.36:5000"
```

---

## LMAO IoT Pipeline (NATS JetStream → DuckDB)

The cluster runs a sensor data pipeline as part of the LMAO project (`/home/pondycrane/LMAO`):

1. **NATS JetStream** — Message broker, single node, 1Gi PVC at `/data`, max 1GB file store / 256MB memory store
2. **iot-ingest-consumer** — Python consumer subscribing to NATS JetStream, persists sensor readings to DuckDB at `/data/sensors.db` on a 1Gi PVC

**ConfigMaps:** `iot-ingest-config` (NATS_SERVER, DUCKDB_PATH, CONSUMER_NAME), `iot-ingest-code` (inline Python consumer), `nats-server-config` (JetStream nats.conf), `lma-core` (shared protobuf wrapper)

The LMAO gRPC server itself runs on a **separate physical RPi** with LoRa RNode hardware (not in-cluster). A headless Service + Endpoints manifest resolves `lmao-server.default.svc.cluster.local:50051` to the RPi's LAN IP (currently set to `192.168.1.100` in `k8s/lmao-service.yaml` — update to actual IP).

---

## Image Loading

### Option A: Push to local registry
```bash
cd /home/pondycrane/LMAO
docker build -f Dockerfile.iot-ingest -t 192.168.0.36:5000/lmao-iot-ingest:latest .
docker push 192.168.0.36:5000/lmao-iot-ingest:latest
```

### Option B: Load directly into containerd
```bash
docker build -f Dockerfile.iot-ingest -t lmao-iot-ingest:latest .
docker save lmao-iot-ingest:latest | k3s ctr image import -
```

---

## K8s Manifests

All in `k8s/`:

| File | Purpose |
|------|---------|
| `k8s/nats-server.yaml` | NATS JetStream Deployment + PVC + Service + ConfigMap |
| `k8s/iot-ingest.yaml` | IoT ingest consumer Deployment + PVC + ConfigMap |
| `k8s/lmao-service.yaml` | Headless Service + Endpoints for external LMAO RPi server |

---

## Access

```bash
kubectl get nodes          # tp2, tp3, tp4
kubectl get pods -A        # all pods
kubectl get svc -A         # all services
```

API server: `https://192.168.0.49:6443` (tp3). Kubeconfig at `~/.kube/config` on selfhost.

SSH (from selfhost):
```bash
ssh tp3-lan     # 192.168.0.49 — via LAN (from workstation)
ssh tp3         # 192.168.10.40 — internal network (from within cluster)
ssh tp2         # 192.168.10.28 — internal
ssh tp4         # 192.168.10.19 — internal
```

---

## BMC Access

- **Web:** http://192.168.0.47
- **SSH:** `ssh root@192.168.0.47` (password: `turing`)
- **Serial (via BMC):** `picocom /dev/ttyS5` (tp2), `/dev/ttyS6` (tp3), `/dev/ttyS7` (tp4)
- **API:** `GET /api/bmc?opt=get&type=power`, `POST /api/bmc?opt=set&type=power&nodeX=1/0`

Node SSH key: `~/.ssh/pi_cluster_key` on selfhost.

---

## SSH Config (selfhost)

**`~/.ssh/config`:**
```ssh-config
Host tp2
    HostName 192.168.10.28
    User pondycrane
    IdentityFile ~/.ssh/pi_cluster_key
    StrictHostKeyChecking accept-new

Host tp3
    HostName 192.168.10.40
    User pondycrane
    IdentityFile ~/.ssh/pi_cluster_key
    StrictHostKeyChecking accept-new

Host tp3-lan
    HostName 192.168.0.49
    User pondycrane
    IdentityFile ~/.ssh/pi_cluster_key
    StrictHostKeyChecking accept-new

Host tp4
    HostName 192.168.10.19
    User pondycrane
    IdentityFile ~/.ssh/pi_cluster_key
    StrictHostKeyChecking accept-new

Host turing-bmc
    HostName 192.168.0.47
    User root
```

---

## History

### 2026-07-20 — Crash loop fix + config file migration

**Problem:** DHCP reassigned tp3's LAN IP from `192.168.0.45` → `192.168.0.49`, but `--node-ip` and `--tls-san` were hardcoded with the old IP in systemd service files. On restart, k3s couldn't find the old IP on any interface and shut down immediately.

**Fix:**
- Moved all configuration out of systemd service files into `/etc/rancher/k3s/config.yaml`
- Removed `--node-ip` from control plane — k3s now auto-detects from interfaces
- Cleaned stale containerd-shim processes and flannel.1 VXLAN interface that accumulated during crash loop
- Updated worker agent configs to point to new control plane IP
- Deleted stale node entries from SQLite database to force fresh node registration

**Lesson:** Always use `config.yaml` — never hardcode IPs in systemd service files. If DHCP changes the LAN IP, only `config.yaml` needs updating (and the kubeconfig on selfhost).
