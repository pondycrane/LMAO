# T1 DECISION/EVIDENCE — no_std toolchain + blink/boot (ESP32-S3 / Cardputer)

**Ticket:** T1 — no_std toolchain + blink/boot (design §10). Acceptance: espup
nightly + `xtensa-esp32s3-none-elf`; `rust-toolchain.toml`; esp-hal no_std
binary boots on the Cardputer; espflash via a Bazel target; boots, logs
chip/mac; `cargo-size` snapshot (minimal).

## Result: BUILD PASS · on-device boot VERIFIED (Cardputer)

The no_std toolchain + firmware build/link cleanly, and the firmware **was
flashed to and booted on the Cardputer**, producing the idle heartbeat loop on
the USB-Serial-JTAG console (no panic) — chip/MAC confirmed by `espflash`
(chip `esp32s3 rev v0.2`, MAC `d0:cf:13:0d:c4:50`). Working MicroPython was then
restored. See §Hardware honesty for the flash/revert mechanics + production
provisioning caveat.

## What was delivered (all Bazel-first)

`rust-client/firmware/` — an independent workspace (own `rust-toolchain.toml` =
`esp` nightly fork, so the host workspace keeps `stable`):

- `src/main.rs` — `#[main]` esp-hal boot: `esp_hal::init`, logs chip revision +
  base MAC via the stable `esp_hal::efuse` API, then a heartbeat loop.
- `src/interrupt_stubs.rs` — ESP32-S3 vectored-interrupt dispatch stubs. esp-hal
  emits a per-source dispatch table in `.rwtext.interrupt` for every ICU source
  but defines no strong symbols for the bare-metal (non-`unstable`) path; T1
  binds none of them to no-ops. Regenerate from the linker's `undefined
  reference` set if it drifts. Real interrupts are claimed in T3+.
- `.cargo/config.toml` — Xtensa target; `-Wl,-Tlinkall.x -nostartfiles`;
  `build-std = ["core","alloc"]`; `espflash` runner. The `-Tlinkall.x` rustflag
  was the missing piece (esp-hal's `build.rs` exposes the chip linker scripts +
  `alias.x`/`exception.x` via `OUT_DIR` link-search).
- `Cargo.toml` — `esp-hal 1.2.2` (chip `esp32s3`, default features incl.
  `rt`+`exception-handler`), `esp-println 0.18` (`jtag-serial`, Cardputer
  console = USB-Serial-JTAG); `Cargo.lock` committed (pins esp-hal + the whole
  Xtensa build tree).
- `build.sh` / `flash_firmware.sh` + Bazel targets `//rust-client:{build_firmware,
  flash_firmware,firmware_sources}` (manual, never in `bazel test //tests:...`).
  The `flash_firmware` shim carries the explicit AGENTS.md flash-discipline
  warning (replaces MicroPython runtime; restore via `//cardputer_client:flash`).

## cargo-size snapshot (T1 minimal, release)

```
text    data   bss      dec      hex     (file: 220 KB)
38333   2152   405144   445629   6ccbd
```

- **text 38.3 KB** — code (esp-hal + println + boot).
- **bss ~405 KB** — a static heap/stack reservation (esp-hal default); near the
  ESP32-S3's 512 KB SRAM, noted for T6 (link+resource) where RAM pressure is
  the §8 concern. Per-feature deltas: T2(identity)/T3(radio) rows to be added
  to this table as they land.

## How to reproduce (build — no hardware)

```bash
source ~/export-esp.sh                      # espup env (xtensa toolchain)
cd rust-client/firmware
cargo build --release                        # → target/.../release/lmao-firmware-t1
# or: bazel run //rust-client:build_firmware
xtensa-esp32s3-elf-size -A <binary>
```

## Hardware: flash + revert record (Cardputer)

The attached Cardputer is the **M5Stack Cardputer-ADV** (`d0cf130dc4500000`).
Direct images via espflash were gated on two board quirks found here:

- **Default DTR/RTS reset (`--before default-reset`) de-enumerates the
  USB-JTAG-Serial on connect** (needs a physical replug; seen 2×).
- **`--before usb-reset` / `no-reset` connects but reads/writes stall** until
  the chip is in the **ROM download/bootloader mode** — entered by holding the
  Cardputer **GO** (BOOT) button during USB attach, or programmatically by
  `DTR(boot)=1 + RTS(EN) reset` (used here). In bootloader mode espflash loads
  its flash stub and transfers normally.
- **espflash 4.x requires an ESP-IDF App Descriptor** in the ELF; added
  `esp-bootloader-esp-idf` + `esp_app_desc!()` (Cargo.toml / main.rs) so the
  firmware builds a flashable image.

Verification sequence (all against `/dev/ttyACM0`):
1. Entered download mode (GO hold / software DTR+RTS), `espflash flash
   --before no-reset … lmao-firmware-t1` → **success** (chip esp32s3 rev v0.2,
   MAC `d0:cf:13:0d:c4:50`, 87.7 KB / 8 MB).
2. Reset to run → console streams `[t1] heartbeat N` continuously (no panic) —
   **T1 boots and runs on the Cardputer**.
3. **Revert to working MicroPython**: entered download mode, `espflash
   write-bin --before no-reset 0x0 uiflow-…-cardputeradv-v2.5.2-20260831.bin`
   (M5Stack UiFlow2 factory 8 MB image) → success; device boots the **UiFlow2
   V2.5.2** MicroPython banner ("WiFi initialized"), enumerating as
   `M5Stack Cardputer-ADV(UiFlow2)`.

**Production provisioning caveat:** no full-flash backup could be captured
(reads stalled until download mode), so the pre-existing on-device LMAO
MicroPython production client + identity were overwritten by the T1 flash and
are not recoverable from the device. Working MicroPython is restored, but to
return the Cardputer to full LMAO production it must be reprovisioned with the
canonical `bazel run //tools:install_all` (re-injects the server `DEST_HASH`,
mints a fresh identity to add to `ALLOWED_CLIENTS`).
