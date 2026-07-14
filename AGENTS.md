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

### Heltec RNode auto-flash

The LoRa E2E test (`test_cardputer_lora_e2e`) now **auto-flashes** the
Heltec ESP32 with RNode firmware if the device is detected via USB but is
not responding as an RNode.  This makes the test self-healing for the most
common failure mode (freshly erased or mis-flashed Heltec).

**What happens**:
1. The test detects the Heltec by USB VID (Espressif / CP210x / CH340).
2. If firmware is already present → proceeds immediately.
3. If firmware is missing → `flash_rnode_firmware()` from
   `lma_core.rnode_flasher` is called automatically.  This erases the
   ESP32 via `esptool.py`, writes firmware at 921600 baud, then provisions
   EEPROM via the KISS serial protocol (product info, checksum, signature).
4. After flashing, the device port is re-discovered (it may re-enumerate to
   a different path) and the firmware is verified.
5. If auto-flash fails, the test skips with a diagnostic message.

**Prerequisites**:
- The `esptool` and `pyserial` packages must be installed (declared in
  `lmao_server/requirements_lock.txt` and available via Bazel).
- The Heltec must be connected via USB.
- Only **one** E2E test should run at a time — flashing is not safe for
  concurrent access to the same USB device.

**User impact**: With auto-flash, the developer workflow reduces to:
```
plug in devices → bazel test //tests:test_cardputer_lora_e2e → green ✔
```
No manual `rnodeconf --autoinstall` step is required.

**Note**: Auto-flash overwrites any existing firmware on the Heltec. If you
have custom RNode settings you want to preserve, ensure RNode firmware is
already running before the test.

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
