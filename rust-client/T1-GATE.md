# T1 DECISION/EVIDENCE — no_std toolchain + blink/boot (ESP32-S3 / Cardputer)

**Ticket:** T1 — no_std toolchain + blink/boot (design §10). Acceptance: espup
nightly + `xtensa-esp32s3-none-elf`; `rust-toolchain.toml`; esp-hal no_std
binary boots on the Cardputer; espflash via a Bazel target; boots, logs
chip/mac; `cargo-size` snapshot (minimal).

## Result: BUILD PASS · on-device boot PENDING-HARDWARE

The no_std toolchain + firmware **build and link cleanly**; the **flash + boot
+ chip/MAC console log is held** (UNVERIFIABLE from this host) because the only
attached Cardputer is the live production node — see §Hardware honesty.

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

## Hardware honesty (flash held — production)

The only attached Cardputer is the **live production node** (`/dev/ttyACM0`,
id `usb-M5Stack_Cardputer-ADV_UiFlow2_d0cf130dc4500000`): the production
`lmao-server` logged 142 mesh announces in 30 min while this work ran. Flashing
T1's Rust firmware over it would replace the MicroPython runtime (an
esptool-class op — AGENTS.md restrains these; USB-Serial-JTAG is fragile and a
careless flash can need a **physical replug**, which this host cannot do), stop
its production announce until reverted, and there is **no saved MicroPython
firmware image** in the repo for the stable revert. Therefore the on-device
boot + chip/MAC log is **PENDING-HARDWARE / UNVERIFIABLE** here; T1's remaining
acceptance is a supervised flash on a non-production/dev Cardputer (or with a
physical operator + saved revert image), via
`bazel run //rust-client:flash_firmware -- --port /dev/ttyACM0` followed by
`bazel run //cardputer_client:flash` to restore MicroPython.
