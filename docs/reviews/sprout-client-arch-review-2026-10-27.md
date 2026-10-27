# Sprout Client — Architectural Review (2026-10-27)

Scope: `smart_irrigation/` — the native ESP-IDF client
(`native-client/firmware/`), its relationship to `firmware_common/` and the
Cardputer native tree, the legacy MicroPython shell, and patch/vendoring
hygiene. Focus: architecture + DRY. Every finding is cited `file:line`;
headline claims verified first-hand.

## Architecture as built

```
smart_irrigation/native-client/firmware/main (ESP-IDF C++)
  main.cpp (541 ln) — boot + 1 s loop + protocol glue (~48% of the file)
    ├── control.{h,cpp} (316 ln) — pulse-train hysteresis engine, pure Q8.8, host-tested
    ├── moisture.{h,cpp} — ADC1 driver + pure calibration math
    ├── pump.{h,cpp} — G26 driver, runtime-only actuation gate (boot dry-run)
    ├── button_led.{h,cpp} — G39 arm UI + SK6812 over RMT
    ├── sht30.{h,cpp} — ENV III air T/RH over I2C0
    └── uart_at_interface.{h,cpp} — RAK3172 DTU P2P AT, RNS InterfaceImpl
          │
firmware_common/ (staged by build.sh — single canonical protocol stack)
    └── lma_encoder · lxmf_send · lma_identity · path_find
          │
   LoRa P2P via DTU UART → LMAO server → NATS → DuckDB
```

Boot: pump-OFF first (`main.cpp:267`, #119) → NVS → button/LED → DTU-AT radio
→ identity (`lma_identity`) → destinations → control init (NVS blob +
profile). Loop: announce 30 s → probe read → `control.tick` → gated pump →
NVS persist → 5-min SensorReport (ids 3/2/4 + band 10/11 + pump 6/7 when
armed) via `lxmf_send` / `Transport::broadcast`.

### What's done well (keep)

- `control.{h,cpp}` is pure C++ with zero ESP-IDF deps and a 564-line host
  sim (`tests/control_test.cpp`) — and it is **wired into Bazel** (unlike
  RoboDog's orphaned selftests, see the RoboDog review).
- `firmware_common` reuse is real: both native mains call
  `lma_encoder`/`lxmf_send`/`lma_identity`/`path_find` — no re-implementation;
  host tests share the same Bazel targets and golden bytes
  (`native-client/BUILD:14-25`).
- Sensor-id contract is consistent across sprout, cardputer, and the
  canonical registry (`proto/lma_messages.proto:61-75`; `main.cpp:177-208`).
- Vendoring rule followed for mbedtls / RTReticulum / u8g2 (`http_archive` +
  patches at fetch time).
- Safety layering correct in code: pump-OFF first boot action, runtime-only
  actuation arming, dry-run skips persistence.

## P1 — Correctness & safety

1. **`board_pins.h` contradicts itself on the pump/probe assignment — both
   sides claim field verification.** Verified: the S3 block comment says
   *"Moisture probe → GPIO2 … pump → GPIO1. NOTE: field-verified"*
   (`board_pins.h:12-16`), then the `#define` section says *"Probe is ON
   GPIO1 (ADC1_CH0); pump GPIO2. Field-verified on the S3 … GPIO2 read 0 (no
   probe)"* (`board_pins.h:20-30`). The code does probe=GPIO1/pump=GPIO2; the
   first comment says the opposite. On a board where a wrong GPIO energizes a
   water pump, one of these "field-verified" notes is stale — resolve and
   delete the loser.

2. **Server identity is a hardcoded literal, diverging from the Cardputer
   convention.** Verified: `main.cpp:62` bakes `DEST_HASH_HEX = "dad35b…"`
   and `main.cpp:129-133` a pubkey; the Cardputer native tree injects the
   same knobs at build time (`cardputer_client/firmware/build.sh:61-84`
   generates `lmao_config.h` with `LMAO_DEST_HASH_HEX` /
   `LMAO_NODE_IDENTITY_HEX`, empty-hash = announce-only fallback). Sprout has
   no injection path and no empty-hash guard — re-pointing a Sprout means
   editing committed source, and the baked hash silently goes stale if the
   server identity is ever regenerated (#70/#93 territory). Two native trees,
   two config conventions.

3. **Device build compiles an unpinned, unpatched RTReticulum.** Verified:
   `build.sh:72-75` clones `--depth 1` from moving HEAD, while the WORKSPACE
   pins `dab4362…` for host builds and applies `rtreticulum_fixes.patch`
   **only to the host archive** (`WORKSPACE:37`). The device firmware can
   silently diverge from the host-tested rev and silently lack the patched
   behaviors (`has_key`, path-request) that `path_find` uses. The Cardputer
   build.sh deliberately reuses Sprout's checkout to avoid a second drift
   source. Fix: pin the clone to the WORKSPACE commit and apply the patch in
   the staged tree.

## P2 — DRY findings

| # | Finding | Evidence |
|---|---|---|
| 1 | **The "RNS mesh client core" is copy-pasted between the two native `main.cpp`s — a direct violation of the repo's firmware_common rule.** Near-identical: announce callback + server-identity statics (sprout `main.cpp:101-118` ≈ cardputer `main.cpp:91-110`), the `unix_s = 946684800 + uptime` epoch hack (sprout:168 ≈ cardputer:121), the LXMF send tail (sprout:228-246 ≈ cardputer:159-176), destination registration + 30 s announce + `path_find` loop (sprout:308-330,408-423 ≈ cardputer:198-213,247-262). Belongs in `firmware_common/` as a `mesh_client` component; only sensor read + control belong in device `main/`. ~48% of sprout `main.cpp` is protocol glue. | both sides cited |
| 2 | build.sh staging/docker/firmware_common-copy logic duplicated line-for-line between the two trees; only the define-injection blocks differ. | sprout `build.sh:62-114` ≈ `cardputer_client/firmware/build.sh:19-87` |
| 3 | Plant-profile whitelist in 3 places: `kProfiles[]` (`control.cpp:15-26`) + `build.sh:52-57` + `flash.sh` — a new profile means three edits. | — |
| 4 | Hex-decode re-implemented: `baked_from_hex` (`main.cpp:133-148`) vs file-static `hex_to_bytes` (`firmware_common/lma_common/path_find.cpp:26`) vs `lma_identity`'s internal parser — should be one exported helper. | — |
| 5 | Sensor ids are magic literals in both C++ mains; the registry lives only in proto comments — a `firmware_common/lma_common/sensor_ids.h` would enforce it. | `main.cpp:173-208` |
| 6 | Dead MicroPython residue: `smart_irrigation/firmware/lib/proto/` and `urns_overlay/` contain **only `__pycache__/*.pyc`** (verified — sources deleted, dirs and `BUILD:13-20` globs remain); `lib/contextlib.py` is an unimported copy of the Cardputer's; `config.py:27` says 60 s interval vs 300 s everywhere else. | verified |
| 7 | `device_config.h` is both a committed header and a build-generated artifact (overwritten in the staged tree, `build.sh:97-101`) — staged vs repo copy is a drift hazard only partially mitigated by flash.sh's stale-image compare; `--mode/--plant/--target` parsing duplicated between build.sh and flash.sh. | `build.sh:88-101`, `flash.sh:28-70` |
| 8 | [RULE] `mbedtls_min_config.patch` (158 KB) is a committed generated blob — it deletes 4,392 upstream lines and inserts the same 34 lines that already sit in `mbedtls_min_config.h`, which **nothing references** (verified: zero BUILD/WORKSPACE/bzl refs). Replace with `patch_cmds` copying the 1.3 KB header. | AGENTS.md bans bulk generated outputs |

## P3 — Risks

- **No watchdog, no deep sleep — contradicts `smart_irrigation/AGENTS.md`**
  ("deep sleep mandatory; keep the WDT + heap-fragmentation recovery patterns
  from cardputer_client"). Bare `for(;;) + vTaskDelay` (`main.cpp:399,541`);
  `rmt_wait_tx_done(..., portMAX_DELAY)` (`button_led.cpp:58`) can hang
  forever with a pump-adjacent GPIO live.
- **Fake wall clock in the ML stream**: every LXMF message stamped
  `946684800 + uptime` (`main.cpp:161`) — the training data this node exists
  to produce carries meaningless timestamps indistinguishable from real ones.
- **NVS blob**: magic + length check but no version field (`control.h:108`,
  `main.cpp:342-344`); `nvs_flash_erase()` on `NEW_VERSION_FOUND`
  (`main.cpp:273-276`) wipes the stable RNS identity and profile along with
  the state.
- **I2C**: lazy mid-loop init, no bus-lockup recovery (`sht30.cpp:29-36`);
  first-contact failure indistinguishable from init failure.
- **Dead announce-learn path**: with the baked pubkey, `on_announce_cb` and
  `path_find::request` can never fire (code admits "Defensive only…",
  `main.cpp:188-191`) — delete it or make it primary and the bake the
  fallback.
- `moisture.h:15-16` float calibration constants feeding an integer engine;
  header comment still describes the classic GPIO32 map as primary.

## Highest-leverage fixes, in order

1. Resolve the `board_pins.h` pump/probe contradiction (P1.1) —
   safety-relevant, cheapest fix in the list.
2. Extract the mesh-client core into `firmware_common/` (P2.1) — the biggest
   DRY win; shrinks both mains to pure application code and gives
   DEST_HASH/interval/identity injection one home (fixing P1.2 en passant).
3. Pin the RTReticulum clone to the WORKSPACE commit + apply
   `rtreticulum_fixes.patch` to the device tree (P1.3).
4. Add WDT + deep sleep per the project's own AGENTS.md; fix the epoch hack
   (P3).
5. Delete the dead MicroPython residue + shrink the mbedtls patch to a
   `patch_cmds` copy (P2.6, P2.8).

---
*Method: two parallel read-only code scouts + first-hand verification of the
pin-map contradiction, hardcoded DEST_HASH, unpinned clone, dead MicroPython
dirs, and the unreferenced mbedtls header.*
