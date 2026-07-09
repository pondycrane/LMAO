# LMAO project rules

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
3. If firmware is missing → `rnodeconf --autoinstall` is called automatically
   (output is printed to the test log so you can monitor progress).
4. After flashing, the device port is re-discovered (it may re-enumerate to
   a different path) and the firmware is verified.
5. If auto-flash fails, the test skips with a diagnostic message.

**Prerequisites**:
- The `rns` package must be installed (already declared in
  `lmao_server/requirements_lock.txt` and available via Bazel).
  `rnodeconf` is bundled inside `rns` at `RNS.Utilities.rnodeconf`.
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
