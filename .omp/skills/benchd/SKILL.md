---
name: benchd
description: Verify LMAO hardware changes through the benchd service on op4 (board queue + leases + builds + flash + pixel capture + vision readback) instead of touching a serial port. Covers the `bench` CLI (submit/wait/result/evidence/probe/boards), the four families (calendar, home, claw, hermes), which job kind to use for which verification, the commit-ref transport for local-only commits, exit-code handling, the quarantine protocol, and the payload keys each kind reads. Use whenever a task needs a RoboDog board built, flashed, captured, or voice-gated, or before claiming any hardware verdict.
---

# benchd

The **bench** is the LMAO hardware test bench: a daemon (`benchd`) on **op4**
that owns the RoboDog board and the test audio rig, serialises access with
leases, and runs build → flash → capture → vision-readback as queued jobs.
It is the **preferred and normally only** way to verify hardware here.

```bash
curl -s -m 5 -o /dev/null -w 'benchd: %{http_code}\n' http://op4:8099/v1/health
bench boards          # which board, its state, the head it currently holds
```

`bench` fails over to `http://op4.local:8099` automatically when `op4` is
unreachable (mDNS tracks the box's current address). Override with
`BENCH_URL` / `BENCH_FALLBACK_URLS`; an explicit `--url` disables failover.

## The rules

1. **Never touch a serial port on this host to verify hardware.** The board
   lives on op4 (`/dev/bench/robodog-0`); the bench owns it. Only fall back to
   `bazel run //robodog_client:build_firmware` + `:flash_firmware` when
   `/v1/health` is unreachable, and record `BENCH=UNREACHABLE` in the evidence.
2. **Never re-flash after the bench path succeeded.** The bench already flashed
   the exact commit; a second flash invalidates the evidence and the `head`
   stamp in `bench boards`.
3. **Never retry into a quarantined board.** A killed flash leaves it
   unbootable; retrying can make it worse. Tell the operator to power-cycle,
   then `bench probe <board>`.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `lmao-production-health`
   owns that marker (regressions #87, #100). Use `bench evidence` for the
   gate's own artifact instead.
5. **A verdict you did not obtain is not evidence.** `bench wait` exit codes
   are the truth; a timeout (`75`) is `BENCH=BUSY`, not a pass.

## Families

Each family is a firmware tree the bench knows how to build, flash, and judge.
`--family` defaults to `calendar`.

| Family | Tree | Flash provisioning |
|---|---|---|
| `calendar` | `robodog_client/` (family-calendar display, framed TCP :4247) | bench passes `--wifi-ssid/--wifi-pass/--server-host/--kid-name ""` from its own creds |
| `home` | `robodog_home_client/firmware/` (SHTC3 env chart, Reticulum TCP) | NVS is rewritten every flash; bench supplies device name + identity + dest hash |
| `claw` | `robodog_client/esp-claw/` (ESP-Claw edge agent, text UI + voice) | NVS rewritten (WiFi + LLM endpoint) from bench creds |
| `hermes` | `robodog_client/voice/` (XiaoZhi voice firmware) | bridge OTA URL comes from NVS |
| `cardputer` | `cardputer_client/` (MicroPython client, UIFlow2 — its own board, `0x303A:0x816B`) | `flash.py` uploads the client; benchd's `cardputer.py` then pins `/flash/rns/identity` + writes `DEST_HASH` |

A family names the USB **fingerprint** it needs, so a job is routed to a board
that can run it; naming a board whose fingerprint does not match is a 400
(`refusing to flash the wrong stack`), and a family with no matching board is
refused rather than flashed elsewhere.

**Boards are auto-discovered — do not maintain a list.** `bench boards` is
whatever is on the USB bus: `0x303A:0x1001` → `robodog-<n>`,
`0x303A:0x816B` → `cardputer-<n>` (the UIFlow2 runtime the bench flashes),
`0x303A:0x8120` → listed `quarantined` (UiFlow bootloader; boot it into
MicroPython). Ports are re-resolved from `/dev/serial/by-id` on every listing,
probe and flash, so a `ttyACM0 → ttyACM1` re-enumeration cannot send a flash to
the wrong device. `BENCH_BOARDS` is only an optional pin.

Hot-plug: a board plugged in later shows up on the next `bench boards`/job.
A board quarantined because it was **unplugged** or left in the bootloader
clears itself when it returns; a quarantine from a **killed flash** or a
daemon restart is sticky and needs `bench probe <board>` after a power-cycle.

Credentials, identities, and dest hashes live in `/etc/benchd.env` and are
injected into the job env — **never** put them in a `--payload`.

### `cardputer` — the console-judged family

The Cardputer has **no compile step** (`build` only pins the worktree the
flash runs from) and **no frame stream** (its UI is drawn from Python), so
`flash_capture` judges the **serial console**: `benchd/cardputer.py` resets the
client and drains `payload.seconds` (default 60) into `console.log`. Verdict:
`BOOT`, `READY`, `ID_SHOWN` (the console's `ID: <16-hex>` against
`BENCH_CARDPUTER_IDENTITY_HASH` — the drift guard), `TELEMETRY` (a send cycle
reached), `NO_UNEXPECTED_STATUS`. A pre-send boot frame is `pending` and the
bench retries once with +60 s.

```bash
B=$(bench submit build --family cardputer --head "$SHA" --class dev)
bench wait "$B"
J=$(bench submit flash_capture --family cardputer --head "$SHA" --build "$B" \
        --class dev --payload '{"seconds":60}')
bench wait "$J" --on-artifacts "$ARTIFACTS_DIR/cardputer-console"
bench evidence "$J"        # inlines console.log
```

- `e2e` works for this family too (build + flash + console verdict, no voice
  gate).
- `capture` (live, no reset) only sees output on the client's send cycle, so
  give it `payload.seconds` above `INTERVAL_SECONDS` (300 s).
- `mpy-cross` must exist on the bench host: without it `flash.py` ships `.py`
  sources, which exhaust UIFlow2's heap and boot-loop the device. The bench
  refuses the flash before touching the board instead.
- RF delivery (Cardputer → RNode → server) is **not** covered — the RNode is on
  tp4. That remains the LMAO `test_cardputer_lora_e2e` gate.

## Job kinds

| Kind | What it does | Needs |
|---|---|---|
| `build` | fetch `head`, build in a worktree, record it | `--head` (+`--ref`) |
| `test` | run the repo's Bazel suite in that head's worktree (bazelisk honours `.bazelversion`); no board, shares the build-slot budget | `--head` (+`--ref`), `--targets` (default `//tests:all`) |
| `flash_capture` | flash that build's worktree, reset, capture N frames, vision-read, assert | `--build <id>` |
| `capture` | capture the **already-flashed** board (no reflash, no reset) | board free |
| `e2e` | build + flash + gate in ONE job/worktree — voice families (`claw`, `hermes`) and `cardputer` (console verdict, no voice gate) | `--head`, `--ref` |
| `voice_loop` | voice gate on an already-flashed board | `--build <id>` (else stale checkout, logged) |
| `stress` | multi-session soak (`claw` only) | `--build <id>` |
| `audio_play` | play a WAV into the board mic via the rig | `payload.wav` |
| `audio_capture` | record the rig mic | `payload.seconds` |

`calendar` and `home` have **no** gate script, so `e2e` rejects them: use
`build` then `flash_capture`.

## The canonical loop (calendar/home)

```bash
cd "$WT"                     # the tree holding the commit under test
HEAD_SHA=$(git rev-parse HEAD)

# 1. make the commit reachable from origin (see "Refs" below), then:
B=$(bench submit build --head "$HEAD_SHA" ${REF:+--ref "$REF"} \
        --family calendar --class dev --by "dev:<task>")
bench wait "$B" --timeout 1800          # ~3 min; exit 2 = compile error, do not flash

# 2. flash THAT build and capture the panel
J=$(bench submit flash_capture --head "$HEAD_SHA" --build "$B" \
        --family calendar --class dev --by "dev:<task>" \
        --payload '{"frames":6}')
bench wait "$J" --timeout 900 --on-artifacts /tmp/bench-shots   # ~3 min

# 3. evidence
bench evidence "$J" > "$ARTIFACTS_DIR/bench-evidence.md"
bench boards                             # proves which head the board now holds
```

`flash_capture` verdict (calendar): `HEADER_PRESENT`, `ROWS_RENDERED`,
`NO_UNEXPECTED_STATUS`, judged on the **last** frame only. A pre-sync boot
frame is `pending`, and the bench retries once with `frames+2` before failing —
that retry is normal, not a flake. `home` judges `NAME_SHOWN`, `READINGS`,
`WAITING`; `claw` judges `UI_RENDERED`/`WIFI_ON`/`STATE_VISIBLE`.

`capture` (no reflash) is for "does the **current** firmware still render" —
e.g. after an NVS-only change, or to prove a redraw cadence across two
frames. It reads the live RD_SHOT stream, so it only works while the firmware
emits frames (the calendar firmware gates RD_SHOT on a USB host being
attached — the bench always is).

## The software gate (`test`)

Hardware verdicts do not cover the Python/server half of a change, and a
worker host rarely has the pinned toolchain. The `test` kind closes that: it
runs the repo's **Bazel suite** in the head's own worktree, on the bench box.

```bash
T=$(bench submit test --head "$HEAD_SHA" ${REF:+--ref "$REF"} \
        --targets //tests:all --class dev --by "dev:<task>")
bench wait "$T" --timeout 5400          # first run also fetches Bazel + toolchain
bench evidence "$T" > "$ARTIFACTS_DIR/bazel-evidence.md"
```

- **bazelisk, not the host bazel.** The tree pins `.bazelversion`; a host
  Bazel of another major cannot build this workspace and would judge a
  different toolchain than CI. `bazelisk` fetches that exact release.
- **No board, so no lease.** A `test` job is schedulable while a board is
  flashed; it draws from `BENCH_BUILD_SLOTS` because a suite compiles as hard
  as a firmware build.
- `--nocache_test_results` and `--keep_going` are always passed: a cached
  `PASSED` is not evidence for a head, and one run should report every failing
  target instead of stopping at the first.
- Verdict is `N target(s): P passed[, F failed — //label, …]`; the full log is
  `bazel-test.log`. `payload.targets` and `payload.bazel_args` widen or narrow
  the run (e.g. `--bazel-arg=--test_timeout=900`).
- A `test` job is **not** a hardware gate: it says the suite passes, not that
  the board renders. A hardware change still needs `build` + `flash_capture`.


## Refs — how the bench gets your commit

The bench fetches `+refs/heads/*`, `+refs/pull/*/head`, and `+refs/bench/*`
from origin. So:

- **Already on origin** (branch or PR): pass `--ref refs/pull/<N>/head` or
  `--ref refs/heads/<branch>`, or nothing at all if the sha is on a fetched
  branch.
- **Local-only commit** (not pushed anywhere): force-push it as a transient
  ref, then prune it afterwards:

```bash
git push origin "HEAD:refs/bench/$(git rev-parse --short HEAD)"
bench submit build --head "$(git rev-parse HEAD)" --ref "refs/bench/<short>" ...
git push origin --delete "refs/bench/<short>"
```

A missing ref fails the build with an explicit message naming both options —
it does not silently build something else.

## Exit codes (branch on these, never on log text)

| Code | Meaning | Action |
|---|---|---|
| `0` | passed | use the artifacts |
| `2` | failed (build error, assertion, capture failure) | `bench result <id>` then fix the code |
| `75` | busy past the wait timeout | record `BENCH=BUSY`; do not claim a verdict |
| `78` | quarantined board | record `BENCH=QUARANTINED`; operator power-cycle + `bench probe` |

## Payload keys

| Key | Kind | Meaning |
|---|---|---|
| `frames` | flash_capture (default 6), capture (default 1) | frames to capture |
| `env` | any | extra environment (e.g. `INCLUDE_STRESS:"1"` for `e2e`) |
| `args` | voice_loop, stress | argv appended to the gate script |
| `device_name` | home | per-device identity + NVS name |
| `wav`, `volume`, `loop` | audio_play | corpus WAV, ALSA volume, repeats |
| `seconds` | audio_capture | capture length |
| `ref` | build, e2e | origin ref to fetch (normally pass `--ref`) |

## Debugging a job

```bash
bench result <id>                      # verdict + assertions + fault/symbol lines
bench result <id> --artifacts          # + artifact list
bench logs <id>                        # list artifacts
bench logs <id> --artifact python.log --tail 40
bench logs <id> --artifact python.log --grep 'RD_SHOT|discarded'
bench artifacts <id> --out DIR         # download everything
bench tail <id> --grep error           # follow a running job
```

`flash_capture` artifacts: `flash.sh.log`, `python.log`, and `shots/frameN.i1`
+ `shots/frameN_analysis.txt` + `shots/panel_landscape.png`. The
`*_analysis.txt` is the **vision model's transcription of the panel** — reason
from it, don't guess at the render.

## Gotchas learned the hard way

- **The board is a single resource.** Jobs are serialised by lease; `--class`
  only sets priority (`ci` 100 > `health` 80 > `dev` 40). A `dev` build on a
  head+family that already built is deduped, so re-running it is ~90 s.
- **A fresh worktree gets `sdkconfig` seeded from `sdkconfig.defaults`.**
  That means `build.sh`'s stale-config guard sees the *tracked* defaults, not
  your local gitignored `sdkconfig`. If you change a `CONFIG_*` default, the
  bench will exercise the new value — which is exactly what you want, and why
  the guard must be right before you submit.
- **USB-Serial/JTAG cannot survive light sleep.** If a firmware ever sleeps
  while a host is attached, the board stops enumerating and the next job sees
  a truncated frame then a missing port — the bench quarantines it. Firmware
  that sleeps must gate on `usb_serial_jtag_is_connected()`.
- **`capture` without `--reset` on a board that just booted** returns no
  frames if the firmware has not emitted RD_SHOT yet (the first render lands
  ~40 s post-boot). Use `flash_capture` for a from-scratch verification.
- **`e2e` is voice-only.** For `calendar`/`home`, chain `build` +
  `flash_capture`.
- **The gate scripts run from the worktree under test**, never the persistent
  clone — a stale gate script silently judges the wrong firmware.
- **Never run `bazel run //robodog_client:build_firmware` or `flash.sh` on
  this host when the bench path succeeded** — the bench already flashed it.

## Worked example (PR #310)

```bash
git push origin "HEAD:refs/bench/0894b70"
B=$(bench submit build --head 0894b704... --ref refs/bench/0894b70 \
        --family calendar --class dev --by "dev:battery-life")
bench wait "$B" --timeout 1800
# passed: built calendar 868128 bytes (robodog_native.bin)

J=$(bench submit flash_capture --head 0894b704... --build "$B" \
        --family calendar --class dev --by "dev:battery-life" \
        --payload '{"frames":6}')
bench wait "$J" --timeout 900 --on-artifacts /tmp/bench-final
# passed: HEADER_PRESENT=True ROWS_RENDERED=True NO_UNEXPECTED_STATUS=True frames=2

bench evidence "$J" | head -8
bench boards        # robodog-1  ready  calendar  /dev/bench/robodog-0  head=0894b704
```

The first attempt of that PR (automatic light sleep) failed exactly as
described above: `captured 0 frames` → `port missing` → `quarantined`. The
fix was firmware, then a power-cycle and `bench probe`, then a clean re-run.
That is the pattern to follow: a bench failure is a finding about the code,
not about the bench.

## Related

- `.archon/commands/robodog-hardware-e2e.md` — the gate phases (bench path is
  Phase 4; local fallback is Phase 5).
- `robodog_client/capture.py` — the frame assembler + vision readback the
  bench drives.
- `.omp/skills/robodog-ui/SKILL.md` — what the captured pixels must satisfy.

## Pinning a device identity (home / cardputer builds)

A `home` or `cardputer` build is provisioned with the server dest hash and the
**pinned per-device identity** from the bench config, keyed by the payload's
`device_name` (spaces fall back to the `study_room` slug):

```
BENCH_HOME_NODE_IDENTITIES={"main bedroom":"<128 hex>","kitchen":"<128 hex>"}
```

The hex is the raw private key of `~/.local/share/lmao_client/devs/<slug>/identity`
(the same bytes the repo's `lma_core/client_identity.py` stores — never commit
it). Missing key ⇒ **announce-only image**: the board mints a fresh NVS identity
on boot, the server auto-learns it as `<type>-<last4>` `pending`, and every
report is dropped (`Dropping LXMF from … (contact state=pending)`). The flash
log says `contact book: no baked identity` in exactly that case.

After editing the map: restart benchd, then resubmit the build — the dedupe key
now hashes the **resolved** build env, so the same head rebuilds instead of
handing back the stale announce-only binary. Confirm the new build's env line
lists `RH_NODE_IDENTITY_HEX`, and that the board's `lxmf/delivery` hash has an
`approved` contact in the book before expecting `READINGS=True`.

### The bench registers the contact

`flash.sh` publishes the device's public record as `approved` when the host
that runs it can `import RNS` (the delivery hash derives from the identity).
The bench host now can: `rns` is installed in `/opt/bench/venv`, which is first
on benchd's `PATH`, so the flash's `python3` is the venv one. Set the API it
posts to once, in `/etc/benchd.env`:

```
BENCH_CONTACTS_API="http://192.168.50.68:8081"
```

benchd injects it as `LMAO_CONTACTS_API` for `calendar`/`home` flashes (a
payload `env` value still wins), so the flash log reads
`contact book: registered '<name>' -> <hash>… approved` instead of the old
`RNS is not installed on this host — cannot derive the hash` approve hint.
Without the key the flash still succeeds and prints the hint — registration is
a convenience, never a flash precondition.

## Reviving a board that dropped off the USB bus

`quarantined / not on the bus` with **no** `0x303A` device in `lsusb` is a port
state problem, not a benchd one — the hub still reports the port connected but
not enabled (`usb 3-1-port3: disabled by hub (EMI?)`). A software reset of the
port re-enumerates it without touching the cable:

```python
# on op4, as root: CLEAR_PORT_FEATURE(PORT_POWER) then SET_PORT_FEATURE(PORT_POWER)
hub.ctrl_transfer(0x23, 1, 8, port, 0)   # power off
hub.ctrl_transfer(0x23, 3, 8, port, 0)   # power on
```

The board then shows up as `303a:1001` and `/dev/bench/robodog-0` reappears;
finish with `bench probe robodog-1`. If a plain `PORT_RESET` leaves it
`connected but not enabled`, the power cycle above is the reliable step.
