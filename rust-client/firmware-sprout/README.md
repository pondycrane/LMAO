# Sprout sender — Rust firmware (Atom Lite + RAK3172 DTU)

The "**rust on sprout**" leg: a no_std Rust sender for the Sprout irrigation
node that reads the soil/air sensors and sends **LXMF SensorReport** bundles to
the Rust LMAO server over the RAK3172 DTU (LoRa P2P) — the same mesh + RNS
framing the Cardputer chart client uses.

When flashed to the Sprout Atom Lite, the Rust server's RF ingest
(`lmao-server-rs` `rf.rs` → `contacts.rs` → `sprout.rs`) starts folding real
Sprout sensor data into the Cardputer chart — **no Python, no native-client C++
in the send path**.

**Verified on the Sprout (2026-10-02, ttyUSB1):** boots clean on the rev-1.1
chip, configures the DTU P2P radio, announces `lmao/sprout` + `lxmf.delivery`,
and sends the first LXMF SensorReport (moisture 39.8%, air 25.9 °C/68%,
339 B) over the wire. Data now flows from the Sprout into the server's mesh —
the remaining step to see it on the Cardputer chart is adding the allow-list
hash below to the server.

## What it does

- Boots esp-hal (Atom Lite = ESP32 classic), UART2 → RAK3172 DTU P2P config
  (868 MHz / BW125 / SF7 / CR 4:5 / preamble 24 / syncword 0x1424 / TX 17 dBm).
- Provisions a **persistent RNS identity** (fixed seed, `src/sender.rs`), announces
  `lmao/sprout` + `lxmf.delivery` every 30 s.
- Every 5 min reads moisture (ADC1_CH4 / GPIO32, 2-point calibrated %), air
  T/H (ENV III SHT30 @0x44 on I2C, SCL=21/SDA=25 — omitted on bus failure) and
  sends one LXMF SensorReport (sensor ids 2/3/4 + 10/11 plant band) to the
  server's `lxmf/delivery` destination, X25519-encrypted to the server key —
  the same message-construction path the Cardputer firmware uses (proven live).
- **Front controls** (native-client parity): the **G39 button** — hold ~2 s **arms actuation** (SK6812 red LED on G27 lights), quick tap disarms; per-session only (a reset always drops back to dry-run). Pump enable (GPIO26) is driven LOW at boot and stays LOW — the irrigation control engine that would energise it is not part of the send leg; arming adds the ML pump-tag readings (sensor ids 6/7) to each report.
- DTU RX is polled (`+EVT:RXP2P` → RNode-frame reassembly) so the node stays a
  mesh peer and any server replies are logged (ACK/DATA reply handling is a
  follow-up).

## Build + flash

```sh
cd rust-client/firmware-sprout
export PATH="$HOME/.cargo/bin:$PATH"; source ~/export-esp.sh   # espup esp toolchain
cargo build --release --target-dir /home/pondycrane/sprout-target
espflash flash --port /dev/ttyUSB1 --chip esp32 --after hard-reset \
  /home/pondycrane/sprout-target/xtensa-esp32-none-elf/release/lmao-firmware-sprout
```

> ⚠️ **Pick the Sprout's port, never the server RNode.** On this staging box the
> RNode is `/dev/ttyUSB0` (prints `mp_cmd=off`); the Sprout Atom is an "M5stack"
> FTDI (`0403:6001`) too — confirm the serial (`69526EE94F` vs `A952AD9F4F`)
> so you don't reflash the RNode radio.
>
> **Rev-1 silicon note.** The Sprout's ESP32 is silicon **rev1.1** (MAC
> `c8:85:41:67:dd:34`). esp-hal 1.2.2 defaults `ESP_HAL_CONFIG_MIN_CHIP_REVISION`
> to v3.0 and panics at boot on older silicon (and the IDF-6.1 bootloader refuses
> rev<3 app images). `.cargo/config.toml` `[env]` sets it to `100` (v1.0) so a
> stock `espflash flash` builds a rev-1 app image that boots clean — no manual
> image patching needed.

## Server allow-list (ONE-TIME ACTION)

The Sprout's `lxmf.delivery` hash must be in the server's
`LMAO_ALLOWED_CLIENTS` or `delivery::DeliveryHandler` drops it at the source
gate. Add to `k8s/lmao-server.yaml` + the server env:

```
6f876d40fe1a3ca663be290108341230
```

(derived from the seed in `src/sender.rs`; the firmware prints it at boot too:
`[sprout] lxmf/delivery hash: …`)

## Crates

- `rust-client/crates/lma-dtu` — the RAK-AT radio protocol (config cmds,
  PSEND/PRECV sequence, `+EVT:RXP2P` hex parser, RNode frame split/reasm) + the
  no_std SensorReport envelope encoder. Pure logic, 11 host tests (golden
  vectors from the C++ `lma_encoder_test.cpp`).
- `src/sender.rs` — RNS identity/announce/LXMF message builder (port of the
  Cardputer firmware's `rns_link` sender helpers).
- Radio params must match the server RNode / native-client DTU (same table in
  `lma-dtu/src/at_dtu.rs`).
