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

Tests auto-skip when the required hardware is not detected.

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

The RNode (Heltec ESP32 LoRa on `/dev/ttyUSB0`) is the server's LoRa radio bridge. It was flashed once via the web tool at https://flasher.rnode.network/ and works reliably.

**Do NOT flash the RNode via esptool or any other method.** The web flasher is the only supported flashing method. Using esptool (especially interrupting a flash) bricks the device and requires physical USB reconnection + reflashing via the web tool to recover.

The RNode firmware responds to the standard RNode DETECT protocol (`0xc0 0x08 0x73 0xc0` → `0xc0 0x08 0x46 0xc0`). It is configured at 868 MHz, BW 125 KHz, SF 7, CR 5, TX 17 dBm.

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
