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
