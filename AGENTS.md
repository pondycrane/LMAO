# LMAO project rules

## First: read the README

Always start by reading `README.md` in full before doing anything else.
The README is the single source of truth for project architecture, setup,
usage, testing, and conventions. It covers everything from protocol design
to hardware setup to deployment. Do not proceed without reading it first.

## E2E flash verification

Run the following as final verification before marking any feature complete or submitting a PR:

```bash
# Flash verification (requires Cardputer)
bazel test //tests:test_cardputer_e2e --test_output=all

# LoRa communication verification (requires Cardputer + Heltec RNode)
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

Tests auto-skip only when no hardware is detected. Since issue #93 the RNode
lives on the K8s node tp4, so on a dev machine with only the Cardputer
attached the gate does NOT skip: it actively verifies the production LoRa path
(Cardputer → pod → NATS → DuckDB) via the server log and the JetStream
`iot-ingest` consumer health, recording PASS / FAIL / UNVERIFIABLE. See
`.archon/commands/lmao-hardware-e2e.md` (Phases 4a-4c) for the exact checks.

### Humidity Sensor E2E Validation

When an external humidity sensor (e.g., DHT20) is connected to the Cardputer,
the E2E test (`test_cardputer_lora_e2e`) validates humidity readings in
addition to temperature. Set the following environment variable to configure
the sensor type expected in the test:

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

**NEVER use esptool on the Cardputer.** It can only be flashed via:
  1. The Bazel `//cardputer_client:flash` target (uploads MicroPython files via raw REPL)
  2. The serial flash tool (for initial MicroPython firmware install, done via esptool in download mode with G0+RESET)

**Do NOT run `esptool ... chip_id` or any other esptool probing/inspection command on the Cardputer.** Doing so disconnects the USB-Serial-JTAG interface and requires a physical USB unplug/replug to recover.

The Cardputer runs MicroPython (M5Stack Cardputer ADV firmware), not native firmware. All communication is via the MicroPython raw REPL over `/dev/ttyACM0`.

**Vendored changes to flash.py:**
- `DEVICE_PREFIX = "/flash"` — M5Stack firmware mounts flash at `/flash/`, not root
- `boot.py` does `M5.begin()` then runs the LMAO client
- `ucontextlib.py` must be in `lib/` (MicroPython needs `ucontextlib`, not `contextlib`)

**First-time setup:** Erase + flash MicroPython firmware, then `bazel run //cardputer_client:flash` to upload client files.

## Archon workflows (LMAO-specific)

Use the dedicated, versioned workflows in `.archon/workflows/` — not the
generic bundled ones — for feature-to-PR work in this repo:

```bash
# Fix a GitHub issue end-to-end (gates: BUILD completeness + unit tests +
# mandatory hardware E2E + production health, re-run after every fix phase)
archon workflow run lmao-fix-issue "Fix issue #N"

# Feature idea to reviewed PR with the same gates
archon workflow run lmao-feature-dev "description of feature"
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
