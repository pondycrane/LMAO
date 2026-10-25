# LMAO project rules

## First: read the README

Always start by reading `README.md` in full before doing anything else.
The README is the single source of truth for project architecture, setup,
usage, testing, and conventions. It covers everything from protocol design
to hardware setup to deployment. Do not proceed without reading it first.

## No vendored third-party code — fetch opensource deps, never copy them in

NEVER commit a verbatim copy of a third-party open-source library or its
generated files into this repo (e.g. a vendored `u8g2` blew a PR past 500k
lines / 44 MB of font tables). This includes `components/`
sub-trees copied from an upstream, `.c`/`.h` dropped verbatim from a tarball,
and bulk generated outputs that belong to the upstream build.

- **fetch** open-source at build time instead of shipping it:
  - Bazel-managed builds: use `http_archive` (e.g. the `@codec2` precedent in
    `lmao-master/WORKSPACE` + a `*.BUILD`, carried in the target's runfiles and
    staged into the ESP-IDF tree by `build.sh`).
  - ESP-IDF `idf.py` projects: use the component manager
    (`idf_component.yml` → the gitignored `managed_components/`).
- Only LMAO-owned source lives in the repo. A third-party dep is allowed in
  the tree only while it is provisionally staged by `build.sh` into a
  gitignored target dir — never in a committed source dir.
- Long generated/derived output files (font tables, codebooks, protobuf
  wire blobs) count as third-party output: regenerate or fetch, don't commit.

If you are about to `cp -r` an upstream `components/<lib>` into the repo, stop
and wire the fetch instead.


## DRY — no copy-paste across the two native firmware trees

The Sprout and Cardputer native clients share one firmware protocol stack;
never duplicate it. The **single canonical copy** lives in
`firmware_common/`:

- `lma_common/lma_encoder.*`  — wire-compatible SensorReport/LMAOEnvelope encoder
- `lma_common/lxmf_send.*`    — send-only LXM body builder (RTReticulum)
- `lma_common/lma_identity.*` — NVS identity persistence + hex/delivery helpers
- `lma_common/path_find.*`    — on-demand RNS path request (ESP-IDF only)
- `rtreticulum/CMakeLists.txt`— shared RNS lib wrapper for the ESP-IDF builds

Both `build.sh` scripts stage `firmware_common/` into the targeted
`.rtreticulum/firmware/<name>/components/` at build time, and the shared
Bazel host-testable units are `//firmware_common:lma_encoder` /
`//firmware_common:lxmf_send` (the device sources pull ESP-IDF, so they build
only via `build.sh`). Put any code that both devices need in `firmware_common/`
first — copy-pasting into a device `main/` is a DRY violation.

## E2E flash verification

Run the following as final verification before marking any feature complete or submitting a PR:

```bash
# Flash verification (requires Cardputer) — MicroPython client (default runtime)
bazel test //tests:test_cardputer_e2e --test_output=all

# LoRa communication verification (requires Cardputer + Heltec RNode)
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

These flash the MicroPython client over the raw REPL (`install_all`'s default
path). The native C firmware is opt-in (`--native-cardputer`, idf.py/esptool)
and its gate is `bazel test //tests:test_cardputer_native_e2e --test_output=all`.

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

Given the Deployment references the mutable tag
`192.168.50.153:5000/lmao-server:latest`, `install_all` **force-restarts**
(`kubectl rollout restart`) each Deployment after applying, so a `:latest`
change actually goes live instead of silently keeping the old pod (the
"unchanged spec ⇒ no rollout" trap — fixed in
`tools/install_services.py` `_force_restart_and_wait`). After any deploy,
verify the pod is on the intended code and DEST_HASH is unchanged:

```bash
kubectl rollout status deployment/lmao-server --timeout=240s
POD=$(kubectl get pods -l app=lmao-server -o jsonpath='{.items[0].metadata.name}')
kubectl logs "$POD" | grep "Delivery destination"            # DEST_HASH unchanged (identity PVC, #70/#93)
kubectl logs "$POD" | grep -E "Reply sent|DATA .*" | tail -1 # confirm the new server payload is served
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
the E2E test validates humidity readings in addition to temperature. Set the
following environment variable to configure the sensor type expected in the
test:

```bash
E2E_SENSOR_TYPE=DHT20 bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

When `E2E_SENSOR_TYPE` is not set (default), the test runs in single-reading
mode (die temperature only), which is the normal configuration.

## RNode

The RNode (Heltec ESP32 LoRa) is the server's LoRa radio bridge. Since issue #93 it is plugged into the **K8s node tp4** (`/dev/ttyUSB0` there) and consumed by the in-cluster `lmao-server` Deployment — it is no longer on the dev machine. It was flashed once via the web tool at https://flasher.rnode.network/ and works reliably.

**Do NOT flash the RNode via esptool or any other method.** The web flasher is the only supported flashing method. Using esptool (especially interrupting a flash) bricks the device and requires physical USB reconnection + reflashing via the web tool to recover.

The RNode firmware responds to the standard RNode DETECT protocol (`0xc0 0x08 0x73 0xc0` → `0xc0 0x08 0x46 0xc0`). It is configured at 868 MHz, BW 125 KHz, SF 7, CR 5, TX 17 dBm.

## Cardputer

The Cardputer runs the **MicroPython client** (`cardputer_client/*.py`,
µReticulum + the micropython-lib `lora-sx126x` driver). That library stack is
the maintained, field-proven path for this board — including the
chart/display — and `install_all` flashes it by default. Flashing copies the
client over the raw REPL.

The **native C firmware** (`cardputer_client/firmware/`, ESP-IDF +
RTReticulum, PR1) is **opt-in** (`--native-cardputer`) and reserved for
hardware with a very tight heap; the **Sprout / Atom Lite** native client
(`smart_irrigation/native-client`) is its primary user. On the Cardputer it has
no display/receive path — the chart needs the MicroPython client.

- **Flash only through the sanctioned tools**: `bazel run //tools:install_all`
  injects the server's `DEST_HASH` and flashes the MicroPython client by
  default (`--native-cardputer` selects the C firmware). The manual
  `bazel run //cardputer_client:flash` (MicroPython) and
  `//cardputer_client:flash_firmware` / `:build_firmware` (native) targets
  exist but do **not** inject `DEST_HASH`. Do not run ad-hoc esptool
  probing/flashing on the Cardputer outside these paths (USB-Serial-JTAG is
  fragile to careless reflashes; `esptool.py chip_id`-style probes disconnect
  it and need a physical unplug/replug). Installing the M5Stack MicroPython
  firmware itself is a one-off esptool step — see README §5.
- MicroPython-client settings (report interval, sensor type, DEST_HASH) live in
  `cardputer_client/config.py`. The native firmware takes them as build-time
  defines in `cardputer_client/firmware/build.sh` (`LMAO_DEST_HASH_HEX`,
  `LMAO_INTERVAL_SECONDS`, `LMAO_SENSOR_TYPE`) — it has no on-device config.
- **DEST_HASH continuity is critical (#70, #93): always (re)flash the client
  via `install_all`, not the bare `:flash_firmware` target.** The bare native
  targets build with `LMAO_DEST_HASH_HEX` unset (announce-only, device logs
  "No destination configured — not sending"); only `//tools:install_all`
  resolves the server's current delivery hash and injects it.
- **Client identities are pinned — no drift across re-flashes.** `install_all`
  keeps one canonical client identity on the host at
  `~/.local/share/lmao_client/lxmf/identity` (**outside the repo; NEVER commit
  private keys** — `lma_core/client_identity.py` knows where to load them) and
  writes it onto the device each flash (`/flash/rns/identity` for MicroPython,
  `LMAO_NODE_IDENTITY_HEX` for native), adopting the on-device identity on the
  first install so an already-allow-listed hash keeps working.  A wiped
  re-flash therefore comes back with the same `lxmf/delivery` hash; only a
  brand-new device mints a fresh canonical identity.  That hash is printed on
  the serial console and must be added to the server's `ALLOWED_CLIENTS` (env
  `LMAO_ALLOWED_CLIENTS` in `k8s/lmao-server.yaml`) for the server to accept
  its reports (same flow as Sprout's native hash).

**Vendored changes to flash.py (the MicroPython flash tool — default Cardputer path):**
- `DEVICE_PREFIX = "/flash"` — M5Stack firmware mounts flash at `/flash/`, not root
- `boot.py` does `M5.begin()` then runs the LMAO client
- `ucontextlib.py` must be in `lib/` (MicroPython needs `ucontextlib`, not `contextlib`)
- `lib/lora/` (micropython-lib `lora` + `lora-sx126x` + `lora-sync`) and
  `lib/contextlib.py` (python-stdlib) are **vendored**, so a flash needs no
  network: the client's WiFi is off, which makes `mip install` fail
  (`MIP_FAIL: -202`).  `install_all`/`flash.py` skip the mip step while the
  vendored driver is present and only fall back to mip if it is missing.

## Archon workflows (LMAO-specific)

Use the dedicated, versioned workflows in `.archon/workflows/` — not the
generic bundled ones — for feature-to-PR work in this repo:

```bash
# Fix a GitHub issue end-to-end (gates: BUILD completeness + unit tests +
# mandatory hardware E2E + production health, re-run after every fix phase)
archon workflow run lmao-fix-issue "Fix issue #N"

# Feature idea to reviewed PR with the same gates
archon workflow run lmao-feature-dev "description of feature"

# RoboDog — Waveshare ESP32-S3 RLCD-4.2 (ST7305) family-calendar display (u8g2
# native firmware, gRPC GetCalendar from the LMAO server). Verification is
# PIXEL-DRIVEN: the mandatory robodog-hardware-e2e gate builds + flashes the
# board (host esptool via robodog_client/flash.sh — the one sanctioned esptool
# path for the 0x303A:0x1001 board), screen-captures the exact ST7305 pixels
# (capture.py streams the live u8g2 buffer), and reads the render back with the
# vision model (Qwen2.5-VL via capture.py --describe). Loud skip when no board.
archon workflow run robodog-client-dev "description of feature"

# RoboDog hermes VOICE — the SAME board running the XiaoZhi voice firmware
# (robodog_client/voice/, RD_SHOT capture hook + RD_LISTEN software button) +
# the voice bridge + acoustic voice-loop E2E (issue #267). Mutually exclusive
# with the calendar firmware. Verification is VOICE-LOOP-DRIVEN: robodog-hermes-e2e
# (screen capture) + robodog-voice-e2e (RD_LISTEN -> ASR -> Hermes reply ->
# test-mic ring closure -> RLCD subtitle). Never run the calendar gates.
archon workflow run robodog-hermes-dev "description of feature"
# Review an EXISTING hermes-voice PR (starts AT the code review, no planning/
# implement/finalize): the PR must already exist — `gh pr create` first, then:
archon workflow run robodog-hermes-review "review PR #NNN"

# RoboDog ESP-Claw standalone agent — the SAME board running the esp-claw
# edge_agent (robodog_client/esp-claw/, self-built from the pinned upstream +
# the LMAO overlay: text-only UI + RD_SHOT + the Phase 2 native voice loop
# rd_listen -> ASR -> agent -> TTS -> speaker; the board calls Whisper/LLM/
# Kokoro on its own, no bridge). Verification is VOICE-LOOP-DRIVEN:
# robodog-claw-e2e (serial-console asserts + test-mic ring re-ASR + RD_SHOT
# subtitle readback). Mutually exclusive with the calendar/hermes firmware.
# Firmware fixes from review MUST re-flash the board before the e2e gate runs
# (robodog-claw-e2e never reflashes — do it via robodog-claw-implement-review-fixes).
archon workflow run robodog-claw-dev "description of feature"
# Review an EXISTING ESP-Claw PR (starts AT the code review, no planning/
# implement/finalize): the PR must already exist — `gh pr create` first, then:
archon workflow run robodog-claw-review "review PR #NNN"

# Kid panels (per-kid dedicated boards, one firmware image): the per-board
# `kid_name` is set at flash time with `flash_firmware --kid-name <kid>` (writes
# the NVS `kid_name` key at 0x9000, wiping the 24 KB NVS partition — desired for
# re-assignment delta-safety); an empty/absent `--kid-name` leaves NVS untouched
# = family mode.  A kid panel only shows that kid's events (server filters on
# `participants @> '"<kid>"'::jsonb`) and prints the kid name LEFT + live clock
# RIGHT on the single header row.

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

**esptool allowance**: the ONLY sanctioned esptool-on-the-robodog-board path is
`robodog_client/flash.sh` (`bazel run //robodog_client:flash_firmware` — host
esptool; Docker ttyACM RTS passthrough breaks the button-free reset). The
Cardputer (`0x303A:0x8120`) and RNode (`/dev/ttyUSB*`) remain esptool-banned
under every workflow.

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
