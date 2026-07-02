# Alternative Client Firmware Options

This document covers client firmware options beyond the default MicroPython
`cardputer_client` setup described in the main [README](../README.md).

## rsCardputer Native Firmware

Flash the [rsCardputer](https://github.com/ratspeak/rsCardputer) dual-mode firmware
for a full LXMF messenger with display, keyboard, and LoRa support out of the box:

```bash
# Using esptool (download the full firmware zip first)
esptool.py --chip esp32s3 --port /dev/ttyACM0 write-flash 0x0 rscardputer-full.bin
```

See [rsCardputer README](https://github.com/ratspeak/rsCardputer) for details.
This firmware works with the LMAO server — both use the same Reticulum/LXMF protocol.

### Radio Parameter Compatibility

**All LoRa devices must use identical radio parameters to communicate.**

The server defaults to fast/short-range settings. If using rsCardputer firmware
(which defaults to Long Fast: SF11, BW250 kHz), update the server config to match:

```python
# In lmao_server/config.py, update the RNode LoRa interface:
{
    "type": "RNodeInterface",
    "port": "/dev/ttyUSB0",
    "frequency": 868000000,
    "bandwidth": 250000,        # Match client
    "spreadingfactor": 11,       # Match client
    "codingrate": 5,
    "txpower": 17,
}
```

| Parameter | LMAO default (fast) | rsCardputer default (Long Fast) |
|-----------|--------------------|-------------------------------|
| SF | 7 | 11 |
| BW | 125 kHz | 250 kHz |
| Bitrate | 10.84 kbps | 1.07 kbps |
| Link budget | 143 dB | 153 dB |

Also see the [rsCardputer radio presets](https://github.com/ratspeak/rsCardputer?tab=readme-ov-file#radio-presets) for other options.
