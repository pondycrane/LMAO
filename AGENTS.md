# LMAO project rules

## First: read the README

Always start by reading `README.md` in full before doing anything else.
The README is the single source of truth for project architecture, setup,
usage, testing, and conventions. It covers everything from protocol design
to hardware setup to deployment. Do not proceed without reading it first.

## DRY — no copy-paste across the two native firmware trees

The Sprout and Cardputer native clients share one firmware protocol stack;
never duplicate it. The **single canonical copy** lives in
`firmware_common/`:

- `lma_common/lma_encoder.*`  — wire-compatible SensorReport/LMAOEnvelope encoder
- `lma_common/lma_decode.*`   — LMAOEnvelope→TextMessage content decoder (receive side)
- `lma_common/lxmf_send.*`    — send-only LXM body builder (RTReticulum)
- `lma_common/lxm_recv.*`     — inbound LXM body parse + sender signature verify
- `lma_common/lma_identity.*` — NVS identity persistence + hex/delivery helpers
- `lma_common/path_find.*`    — on-demand RNS path request (ESP-IDF only)
- `rtreticulum/CMakeLists.txt`— shared RNS lib wrapper for the ESP-IDF builds

Both `build.sh` scripts stage `firmware_common/` into the targeted
`.rtreticulum/firmware/<name>/components/` at build time, and the shared
Bazel host-testable units are `//firmware_common:lma_encoder` /
`//firmware_common:lma_decode` / `//firmware_common:lxm_recv` /
`//firmware_common:lxmf_send` (the device sources pull ESP-IDF, so they build
only via `build.sh`). Put any code that both devices need in `firmware_common/`
first — copy-pasting into a device `main/` is a DRY violation.

**Display/chart is NOT shared** (PR2): the Sprout native client has no display,
so the ST7789 driver + chart renderer live device-side in
`cardputer_client/firmware/main/{st7789,chart}.{h,cpp}` (chart is pure C++
and host-tested at `//cardputer_client:chart_test`; st7789 is ESP-IDF-only).
The receive *decode* is shared above; only the panel + rendering stay
cardputer-local. Move them to `firmware_common/` only if Sprout ever grows a
display.

## E2E flash verification

Run the following as final verification before marking any feature complete or submitting a PR:

```bash
# Flash verification (requires Cardputer) — native C firmware
bazel test //tests:test_cardputer_native_e2e --test_output=all

# LoRa communication verification (requires Cardputer + Heltec RNode)
bazel test //tests:test_cardputer_native_e2e --test_output=all
```

(The MicroPython-era `test_cardputer_e2e` / `test_cardputer_lora_e2e` targets
still exist for the `--micropython-cardputer` fallback but are no longer the
default gate — native firmware is flashed via `install_all`/`flash.sh`, i.e.
idf.py/esptool, not the raw REPL.)

Tests auto-skip only when no hardware is detected. Since issue #93 the RNode
lives on the K8s node tp4, so on a dev machine with only the Cardputer
attached the gate does NOT skip: it actively verifies the production LoRa path
(Cardputer → pod → NATS → DuckDB) via the server log and the JetStream
`iot-ingest` consumer health, recording PASS / FAIL / UNVERIFIABLE. See
`.archon/commands/lmao-hardware-e2e.md` (Phases 4a-4c) for the exact checks.

## Deployment ("deploy the services" / "e2e deployment") — REQUIRED flags

When a task says to install/uninstall/deploy for e2e, or to put a server-side
change (e.g. `lma_core`) live, that means running `install_all` with **both**
service flags. Missing one is a common source of wasted back-and-forth:

```bash
# Full e2e deployment. Publish images first, then deploy services.
# On a dev box (RNode is on the K8s node tp4, issue #93) also pass --skip-rnode.
bazel run //tools:install_all -- --setup-registry --include-services --skip-rnode
```

- **`--setup-registry`** → publish step (`manage.sh start` + `manage.sh push`):
  starts the local Docker registry and **builds + pushes all LMAO images**.
  This must come first so the deploy runs the freshly built image.
- **`--include-services`** → builds/pushes `lmao-server` + `lmao-iot-ingest`,
  applies the K8s manifests, and deploys the in-cluster `lmao-server`.

⚠️ **Gotcha — applying unchanged `:latest` does not roll the pod.** The
`k8s/lmao-server.yaml` Deployment references the mutable tag
`192.168.50.153:5000/lmao-server:latest`. `install_all`'s `kubectl apply` of an
*identical* spec creates no rollout, so **the running pod keeps the OLD code**
and the "Deployment rolled out" banner is misleading. After any service
deploy, ALWAYS force the rollover and verify:

```bash
kubectl rollout restart deployment/lmao-server \
  && kubectl rollout status deployment/lmao-server --timeout=240s
POD=$(kubectl get pods -l app=lmao-server -o jsonpath='{.items[0].metadata.name}')
kubectl logs "$POD" | grep "Delivery destination"     # DEST_HASH must be unchanged (identity PVC, #70/#93)
kubectl logs "$POD" | grep -E "DATA .*" | tail -1      # confirm the new server payload is being served
```

Notes:
- `--setup-registry`'s `manage.sh start` is **not idempotent**: if the
  `lmao-registry` container already exists it fails with a name Conflict,
  even though the registry is fine and the image is still published by the
  `--include-services` build steps. A registry `[FAIL]` of that shape is a
  false alarm — verify with `curl -s -XGET http://192.168.50.153:5000/v2/_catalog`.
- `DEST_HASH` continuity is critical (#70, #93): the flashed Cardputer only
  works if the rolled pod serves the same delivery destination. The identity
  persists in the `lmao-server-identity` PVC, so a rollover keeps it.

### Humidity Sensor E2E Validation

When an external humidity sensor (e.g., DHT20) is connected to the Cardputer,
the E2E test (`test_cardputer_native_e2e`) validates humidity readings in
addition to temperature. Set the following environment variable to configure
the sensor type expected in the test:

```bash
E2E_SENSOR_TYPE=DHT20 bazel test //tests:test_cardputer_native_e2e --test_output=all
```

When `E2E_SENSOR_TYPE` is not set (default), the test runs in single-reading
mode (die temperature only), which is the normal configuration.

## RNode

The RNode (Heltec ESP32 LoRa) is the server's LoRa radio bridge. Since issue #93 it is plugged into the **K8s node tp4** (`/dev/ttyUSB0` there) and consumed by the in-cluster `lmao-server` Deployment — it is no longer on the dev machine. It was flashed once via the web tool at https://flasher.rnode.network/ and works reliably.

**Do NOT flash the RNode via esptool or any other method.** The web flasher is the only supported flashing method. Using esptool (especially interrupting a flash) bricks the device and requires physical USB reconnection + reflashing via the web tool to recover.

The RNode firmware responds to the standard RNode DETECT protocol (`0xc0 0x08 0x73 0xc0` → `0xc0 0x08 0x46 0xc0`). It is configured at 868 MHz, BW 125 KHz, SF 7, CR 5, TX 17 dBm.

## Cardputer

Since PR1 the Cardputer runs **native C firmware** (ESP-IDF + RTReticulum, in
`cardputer_client/firmware/`), like the Sprout native client — not MicroPython.
Flashing is via the ESP-IDF toolchain (`idf.py flash`, esptool under the hood),
NOT raw REPL.

- **Flash only through the sanctioned tools**: `bazel run //tools:install_all`
  (bakes the server's `DEST_HASH` at build time) or the manual
  `bazel run //cardputer_client:flash_firmware` / `build_firmware` targets.
  Do not run ad-hoc esptool probing/flashing on the Cardputer outside these
  paths (USB-Serial-JTAG is fragile to careless reflashes; the raw-REPL era is
  over). `esptool.py chip_id` type probes still disconnect the USB-Serial-JTAG
  and need a physical unplug/replug to recover — avoid them.
- The legacy MicroPython client (`cardputer_client/*.py`) is a **documented
  fallback** only (chart/display era): `bazel run //tools:install_all --
  --micropython-cardputer`. The native firmware's settings are build-time
  defines in `cardputer_client/firmware/build.sh` (`LMAO_DEST_HASH_HEX`,
  `LMAO_INTERVAL_SECONDS`, `LMAO_SENSOR_TYPE`) — there is no on-device
  config.py anymore.
- **DEST_HASH continuity is critical (#70, #93): always (re)flash the client
  via `install_all`, not the bare `:flash_firmware` target.** The bare targets
  build with `LMAO_DEST_HASH_HEX` unset (announce-only, device logs "No
  destination configured — not sending"); only `//tools:install_all` resolves
  the server's current delivery hash and bakes it in at build time.
- The native identity is minted fresh in NVS on first boot; its
  `lxmf/delivery` hash is printed on the serial console and must be added to
  the server's `ALLOWED_CLIENTS` (env `LMAO_ALLOWED_CLIENTS` in
  `k8s/lmao-server.yaml`) for the server to accept its reports (same flow as
  Sprout's native hash).

**Vendored changes to flash.py (legacy MicroPython flash tool, fallback only):**
- `DEVICE_PREFIX = "/flash"` — M5Stack firmware mounts flash at `/flash/`, not root
- `boot.py` does `M5.begin()` then runs the LMAO client
- `ucontextlib.py` must be in `lib/` (MicroPython needs `ucontextlib`, not `contextlib`)

## Archon workflows (LMAO-specific)

Use the dedicated, versioned workflows in `.archon/workflows/` — not the
generic bundled ones — for feature-to-PR work in this repo:

```bash
# Fix a GitHub issue end-to-end (gates: BUILD completeness + unit tests +
# mandatory hardware E2E + production health, re-run after every fix phase)
archon workflow run lmao-fix-issue "Fix issue #N"

# Feature idea to reviewed PR with the same gates
archon workflow run lmao-feature-dev "description of feature"

# Sprout — smart irrigation node (Atom Lite + DTU base + Watering Unit + ENV III). Blueprint phases.
# Firmware work is hard-blocked until smart_irrigation/docs/algorithm-evaluation.md
# exists (blueprint Appendix 13). ⚠️ The smart-irrigation-dev workflow is NOT ready
# to run yet — pending refinement in #122. Do not run `archon workflow run
# smart-irrigation-dev`; run Phase 0 (algorithm evaluation) as a manual gate, and
# verify hardware with `bazel test //tests:test_sprout_e2e --test_output=all`.
```

These workflows encode the rules on this page (esptool bans, Bazel BUILD
completeness, hardware E2E evidence in the PR body, production Cardputer
left running). See `docs/archon-workflows.md` for the full reference.

**Gate-chain contract**: `$ARTIFACTS_DIR/.gate-head` is written **only** by
`lmao-production-health` when the full chain completes. No other gate node
may write it — doing so causes downstream hardware gates to fast-pass
without running (regression guards: #87, #100). PR creation is blocked
unless the mandatory `$ARTIFACTS_DIR/hardware-e2e.md` artifact exists.
Detail in `docs/archon-workflows.md`.

## Archon GitHub Webhook Relay

A local polling relay lets Archon respond to `@archon` mentions on GitHub
issues and PRs without exposing a public webhook endpoint.

See `docs/archon-webhook-relay.md` for full documentation.

**Quick reference**:
- Comment `@archon ...` on any issue/PR in `pondycrane/LMAO`
- Relay polls every 15s, delivers to Archon at `localhost:3090`
- Runs as `systemctl --user` service: `archon-webhook-relay`
- Script: `tools/archon_webhook_relay.py`
- State: `~/.archon/relay-state.json`
