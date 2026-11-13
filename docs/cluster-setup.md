# Turing Pi 2 Cluster — Kubernetes Setup

**Hardware:** Turing Pi 2 board with 3x RK1 compute modules, running K3s v1.35.5+k3s1.

## Node Network

Each node has **two network interfaces**:

| Interface | Subnet | Purpose | Stable? |
|-----------|--------|---------|---------|
| `eth0` | `192.168.10.0/24` | Internal cluster network (node-to-node) | ✅ Static |
| `wlan0` | `192.168.50.0/24` | LAN / external access | ❌ DHCP (may change) |

| Hostname | Role | Internal IP (eth0) | LAN IP (wlan0) | Hardware | OS |
|----------|------|-------------------|----------------|----------|----|
| `tp1` | worker | `192.168.10.11` | DHCP (wlan0 down 2026-11-02) | RK1 module slot 1 (new) | Debian 13 (trixie), kernel 6.12.47+rpt |
| `tp2` | worker | `192.168.10.28` | `192.168.50.157` (DHCP) | RK1 module slot 1 | Debian 11 (bullseye), kernel 5.15.84-v8+ |
| `tp3` | **control-plane** | `192.168.10.40` | `192.168.50.91` (DHCP) | RK1 module slot 2 | Debian 11 (bullseye), kernel 6.1.21-v8+ |
| `tp4` | worker | `192.168.10.19` | `192.168.50.68` (DHCP) | RK1 module slot 3 | Debian 11 (bullseye), kernel 5.15.84-v8+ |
| `turing-bmc` | BMC | — | `192.168.0.47` (verify — moves w/ LAN) | ESP32 on Turing Pi 2 | BMC firmware (default `root:turing`) |
| `selfhost` | workstation | — | `192.168.50.153` (eth0) | Raspberry Pi 5 | Ubuntu 26.04 LTS (Resolute Raccoon), aarch64 |
| `op4` | bench host | `192.168.10.20` | `192.168.50.202` (DHCP) | Orange Pi 4 (RK3399) | Ubuntu, NVMe root — **hosts the Docker registry** |

**Address the nodes by hostname, never by LAN IP.** `wlan0` is DHCP and has
already moved (`tp3` .90→.91, `tp4` .67→.68 on 2026-11-02, which silently broke
both `kubectl` and the RoboDog panel). The router's DNS resolves the DHCP
hostnames, so `https://tp3:6443`, `ssh tp4` and the board's `RD_SERVER_HOST_DEFAULT "tp4"`
all follow a lease change. The LAN IPs above are a snapshot only.

### Preventing address drift

DHCP moving a node's `wlan0` lease used to break four things at once — and
each failed *silently*: `kubectl` (kubeconfig pinned the old IP), `ssh tp4`,
the RoboDog panel (`RD_SERVER_HOST_DEFAULT`), and every client whose
`LMAO_SERVER_HOST` was an IP. Defence in depth, cheapest first:

1. **DHCP reservations (root fix, manual).** On the router (`192.168.50.1`)
   bind each node's `wlan0` MAC to its current address. That makes the leases
   stable, but it is a router-side setting with no API here — do it once and
   the addresses above stop moving. Node MACs:

   ```bash
   ssh tp3 'ip -br link show wlan0'   # repeat per node
   ```

2. **Hostname addressing everywhere (already in the tree).** Runtime defaults,
   manifests and client configs use `tp1`…`tp4`; the router's DNS resolves them
   for whatever lease DHCP hands out. `tp4.local` (mDNS) also works from
   machines on the same L2 segment if router DNS is unavailable.

3. **Guard against regressions (automated).** A hardcoded node address in a
   runtime path now fails the build:

   ```bash
   bazel run //tools:check_node_addresses        # scans code/manifests/README
   bazel test //tests:test_node_address_drift
   ```

   It scans `lma_core`, `lmao_server`, `human_client`, `tools`, `k8s`,
   `k8s-app`, `cardputer_client`, `robodog_client/main`, `firmware_common`,
   `smart_irrigation/native-client/main` and `README.md`, and exits 1 listing
   every offending `file:line`. `docs/cluster-setup.md` is the only allowlisted
   place that may list the addresses (this snapshot).

4. **Recovering from a move** (if one slips through): find the node by MAC
   (`sudo arp-scan --interface=wlan0 192.168.50.0/24`), then point the
   kubeconfig at the hostname (`https://tp3:6443`) rather than the new IP, so
   the next move needs no edit.

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
  - 192.168.50.90     # LAN (DHCP — updated 2026-09-16). NOTE: API cert SAN
                      # does not yet include this IP; run `k3s certificate rotate`
                      # (or add via tls-san + restart) to make direct kubectl
                      # TLS-valid without --insecure-skip-tls-verify.
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
  op4:5000:
    endpoint:
      - "http://op4:5000"
```

Plus the name pin, since no DNS zone resolves `op4` for the nodes:
```bash
echo "192.168.50.202 op4" | sudo tee -a /etc/hosts   # on every node
```

> **Per-node, and easy to miss.** A node without this stanza cannot pull from
> the local registry: its pods fail `ImagePullBackOff` on
> `op4:5000/<image>`.  This bit the calendar-sync CronJob when it
> first scheduled onto **tp1** (which lacks the mirror) — the manifest now pins
> `nodeSelector: kubernetes.io/hostname: tp4`, where lmao-server, postgres and
> iot-ingest already run.  Apply the stanza to **every** node before relying on
> the scheduler's free placement.

---

## LMAO IoT Pipeline (NATS JetStream → DuckDB)

The cluster runs a sensor data pipeline as part of the LMAO project (`/home/pondycrane/LMAO`):

1. **NATS JetStream** — Message broker, single node, 1Gi PVC at `/data`, max 1GB file store / 256MB memory store
2. **iot-ingest-consumer** — Python consumer subscribing to NATS JetStream,
   persists sensor readings, text messages, and command acknowledgements to
   DuckDB (`/data/sensors.db`) on a 1Gi PVC; exposes an in-cluster HTTP query
   API on port 8080 (Service `iot-query`, ClusterIP)

**ConfigMaps:** `iot-ingest-config` (NATS_SERVER, DUCKDB_PATH, CONSUMER_NAME,
QUERY_PORT=8080, QUERY_MAX_ROWS=1000, QUERY_TIMEOUT=10), `iot-ingest-code`
(inline Python consumer), `nats-server-config` (JetStream nats.conf),
`lma-core` (shared protobuf wrapper)

The LMAO server runs **in-cluster** as `deployment/lmao-server` (issue #93), pinned to **tp4** where the Heltec ESP32 RNode is attached via USB (`/dev/ttyUSB0`, hostPath + privileged, hostNetwork). The LXMF identity and Reticulum transport state live on the `lmao-server-identity` PVC (`/data/lxmf`, `/data/transport`). A ClusterIP Service resolves `lmao-server.default.svc.cluster.local:50051` to the pod; gRPC is also reachable on the node IP `192.168.0.44:50051`. After an RNode replug or tp4 reboot: `kubectl rollout restart deployment/lmao-server`.

### LMAO server — env knobs

- **Discovery is on demand** (issue #142) — the server sends a single LXMF
delivery announce at startup and does **not** re-announce periodically; the
old `LMAO_ANNOUNCE_INTERVAL` knob is retired. Clients discover the server via
RNS path requests (Sprout native `path_find`, Cardputer native `path_find`),
which the server answers as a leaf node (issue #135). The
answer is deferred by `_PATH_REQUEST_ANSWER_DELAY` (1 s) so a half-duplex
requester does not miss the PATH_RESPONSE in its own TX→RX turnaround window.
- **Client allow-list (security)** — the server accepts LXMF messages only from
known client identities (sender's `lxmf/delivery` destination hash, i.e. the
`Source/From:` the server logs). Defaults are the Cardputer
(`7b38fa21e75d8866c18de3da01540f2e`, native C firmware, NVS-persisted
identity, printed at boot as "my lxmf/delivery hash") and Sprout native
(`f5f05952392627393f067df8c9eaf6c6`, NVS-persisted identity, printed at boot
as "my lxmf/delivery hash"). Extend without a rebuild via
`LMAO_ALLOWED_CLIENTS` (comma-separated hex). Add a new client: capture the
identity/delivery hash from a boot log, then add it here or via env.

**Querying stored data:** `kubectl port-forward svc/iot-query 8080:8080` then
use `POST /query`, `GET /tables`, `GET /schema/<table>` (see README
§"Querying stored data"). Readiness probe is HTTP `GET /healthz` on 8080.

---

## Image Loading

### Option A: Push to local registry
```bash
cd /home/pondycrane/LMAO
docker build -f Dockerfile.iot-ingest -t op4:5000/lmao-iot-ingest:latest .
docker push op4:5000/lmao-iot-ingest:latest
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
| `k8s/iot-ingest.yaml` | IoT ingest consumer Deployment + PVC + ConfigMap + HTTP query API (`iot-query` Service, ClusterIP, port 8080) |
| `k8s/lmao-server.yaml` | In-cluster LMAO server: identity PVC + Deployment (tp4, RNode USB) + ClusterIP Service |

---

## Access

```bash
kubectl get nodes          # tp2, tp3, tp4
kubectl get pods -A        # all pods
kubectl get svc -A         # all services
```

API server: `https://tp3:6443` (tp3, via the router's DNS). Kubeconfig at
`~/.kube/config` on selfhost uses the hostname and
`insecure-skip-tls-verify` (the API cert SAN does not yet cover the DHCP LAN IP
or the hostname).

SSH (from selfhost — hostnames, so a DHCP move needs no edit):
```bash
ssh tp3          # control plane
ssh tp2          # worker
ssh tp1          # worker
ssh tp4          # worker (lmao-server + postgres + RNode)
# internal (from within cluster): tp3=192.168.10.40, tp2/tp4=192.168.10.28/.19, tp1=192.168.10.11
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

---

## Calendar Sync CronJob (hourly)

`k8s/calendar-sync.yaml` runs the family-calendar refresh in-cluster. It
replaced a host Hermes cron (`lmao-kids-calendar-sync` →
`~/.hermes/scripts/lmao_kids_sync.sh`), which is now **paused**.

Two stages per run, matching the old script:

1. `tools/calendar_sync.py --window-days 14` — Google Calendar → `cal_events`
   (Postgres), upsert keyed by event `uid`.
2. `tools/calendar_pics_sync.py` — themed art → 1-bit 176×176 RLCD tiles in
   `cal_pics`.

**Why hourly matters beyond data freshness:** the RoboDog panel syncs by delta
(`since_ms` = its last watermark), so an event row only re-ships when its
`updated_ms` moves. `CalendarStore.upsert` is an unconditional
`ON CONFLICT DO UPDATE` and `lmao_cal_touch_trg` stamps `updated_ms` on every
update — so this job is what re-delivers each in-window event, and therefore
what refreshes that event's **weather forecast** as its date approaches.
Stopping the job freezes per-event weather on the panel (the "now" reading is
unaffected: `CalendarBundle.current` ships on every reply).

Deploy:

```bash
# with the rest of the services (recommended)
bazel run //tools:install_all -- --setup-registry --include-services --skip-rnode

# or just this job
python3 -c "from tools import install_services as s; s.deploy_calendar_sync(r)"
kubectl apply -f k8s/calendar-sync.yaml
```

`install_all` also refreshes the `lmao-gcal-token` Secret from
`~/.hermes/google_token.json` (or `$LMAO_GCAL_TOKEN`) — the token is never in
the repo, and a missing token fails the deploy rather than shipping a CronJob
that cannot read Google.

Run one now and watch it:

```bash
kubectl create job --from=cronjob/lmao-calendar-sync lmao-cal-sync-manual
kubectl logs -f job/lmao-cal-sync-manual
```

### Backfilling tiles after a subject-selection change

Stage 2 is idempotent: a `pic` key already in `cal_pics` is skipped, so
existing tiles keep whatever subject they were generated with. To regenerate
recent ones (e.g. after changing the subject pools), run it with `--force` and
a look-back window — this rewrites every kid event that started within the
last N days (and every upcoming one):

```bash
# From the host, through a port-forward (no cluster Job needed):
kubectl port-forward svc/postgres 5433:5432 &
PASS=$(kubectl get secret lmao-postgres -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
LMAO_CONTACTS_URL="postgresql://lmao:${PASS}@127.0.0.1:5433/lmao" \
  bazel run //tools:calendar_pics_sync -- --force --since-days 14 --limit 10

# Preview what would be regenerated (no image work, no DB writes):
LMAO_CONTACTS_URL=... bazel run //tools:calendar_pics_sync -- \
  --force --since-days 14 --dry-run
```

In-cluster, derive a one-off Job from the CronJob template (so the env,
Secret mounts and node pin stay identical), add the backfill flags to stage 2
and lift the 900 s job deadline — a tile costs ~20-40 s (search + vision
crop/gate + download), so the CronJob's `activeDeadlineSeconds: 900` only
covers ~20-40 events:

```bash
kubectl create job --from=cronjob/lmao-calendar-sync lmao-cal-backfill \
  --dry-run=client -o yaml \
| sed -e 's|python tools/calendar_pics_sync.py|python tools/calendar_pics_sync.py --force --since-days 14|' \
      -e 's|activeDeadlineSeconds: 900|activeDeadlineSeconds: 5400|' \
| kubectl apply -f -
kubectl logs -f job/lmao-cal-backfill
```

(`kubectl patch job` cannot do this: a Job's pod template is immutable.)

The backfill only lands on the panel for events the RoboDog re-syncs: each
regenerated tile bumps `updated_ms` on the events using that `pic`
(`PicStore.put`), so the panel picks them up on its next 30-min delta sync.

**There is no image generator.** Tiles are fetched **line drawings** from the
internet: Wikimedia Commons (public-domain line art) plus DuckDuckGo's image
index queried with a `line drawing` / `coloring page` bias — that is what
reaches character art (Pokémon, PAW Patrol) which Commons has no licensed copy
of. The vision box (`192.168.50.200:8000`) crops each candidate to its subject
and gates it against the theme's subject pool; a final geometric gate rejects
anything that is not outline work — ink fraction 0.02–0.45 and ≥ 0.15 of the
tile as enclosed white interior (a filled silhouette or a badly cropped blank
page fails the second test). A rejected subject retries once with another
subject from the same pool, and an event with no acceptable hit stays artless
and retries on the next hourly run. To place a specific tile by hand, use the
skill tool with an explicit source URL:

```bash
bazel run //tools:calendar_pics -- --key <pic-key> \
    --source https://commons.wikimedia.org/wiki/Special:FilePath/<File>.svg
```

**The fix must be in the deployed image.** Rebuild/push
`lmao-calendar-sync` before a backfill or the next hourly run
(`bazel run //tools:install_all -- --setup-registry --include-services
--skip-rnode`, or `./docker/registry/manage.sh push-calendar-sync` if
present).

Networking notes (measured from an in-cluster pod on tp4):

| Target | Reachable | Effect when down |
|---|---|---|
| `postgres.default.svc.cluster.local:5432` | yes | stage 1 fails |
| `192.168.50.200:8000` (vision) | yes | tile crop/gate skipped (candidate accepted as-is) |
| `192.168.50.138:8000` (gx10 LLM) | refused (box down) | deterministic "reasoned" search queries |
| `commons.wikimedia.org` (tile source) | yes | no Commons line art that run |
| `duckduckgo.com` (tile source) | yes | no character line art that run (events retry next hour) |

LAN hostnames do **not** resolve under cluster DNS, so the ConfigMap addresses
the model endpoints by IP (`gx10-f156` == `192.168.50.138`).
