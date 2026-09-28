# T9 — Power / duty-cycle polish: deep sleep, watchdog, heap guard, RSSI: evidence

**Ticket:** T9 (#167, stretch). Deep sleep between link windows, watchdog +
heap-fragmentation guard (from `cardputer_client` #71/#74), RSSI reporting.

## Result: HOST PASS (power/health/polsish + RSSI wire)

`crates/leaf-power` adds the duty-cycled leaf's power + self-recovery polish as
`no_std` (no alloc) guards, composed with the T4 `leaf-rns` scheduler; RSSI
reporting rides the T5 control wire (sensor_id 9 per the proto registry). The
on-device deep-sleep/RSSI (physical radio + RTC sleep) is firmware-hardware.

## `crates/leaf-power` (no_std, alloc-free)
- **`SleepPolicy`** — deep sleep **between link windows**: computes how long the
  leaf may sleep until the next gateway rendezvous window opens (0 in-window;
  time-to-next in the gap), **capped by `max_sleep_ms`** so a sparse schedule
  cannot starve link-sustain/keepalive. Composes with `leaf-rns`
  `LinkWindowConfig`, so the duty-cycle math matches the scheduler it gates.
- **`Watchdog`** — detects a wedged/live-locked device (no link/radio progress
  for `threshold_ms`) — the #74 "stuck beaconing" recovery motivation.
- **`HeapGuard`** — heap-fragmentation guard (the #71/#74 robustness): reports
  fragmentation = 1 − largest_free/total_free and flags when a framing/transfer
  stage is at risk of a fragmented-RAM stall.

6 host tests (`//rust-client:test_leaf_power`): sleep-zero-in-window,
sleep-to-next-window-in-gap, sleep-capped-by-max, **composes with leaf-rns
scheduler** (waking at the policy's next window lands the leaf in an open
scheduler window; duty-cycle matches), watchdog fires after threshold, heap-guard
flags fragmentation.

## RSSI reporting (lma-wire)
`sensor_id 9 = RSSI (dBm)` per the proto registry. `lma_wire::rssi_reading`
stamps the radio RSSI into a `SensorReport`; cross-language golden proves the
encoding byte-matches the Python `lma_encoder` reference (a `SensorReport` with
an RSSI reading, `-75.0` dBm). The on-device value comes from the SX1262's frame
RSSI (physical leg).

## Bazel-first
`crates/leaf-power` → `//rust-client:{leaf_power_sources, test_leaf_power}`
(manual). RSSI golden under `lma-wire` (needs PROTOC).

## On-device hardware leg (held)
Actual RTC deep-sleep between link windows and per-frame RSSI sampling are wired
in the firmware radio bring-up (pending RNode/firmware transport; the standing
production mandate holds).
