# T4 — RNS leaf transport + LINK capability (duty-cycle link-window): evidence

**Ticket:** T4 (#162). Register the interface + rns-core transport; inbound
processing; then **Link support on the leaf**: establish a link and **serve
inbound link requests**, holding link state. **Accept:** with a host std peer
the leaf announces, answers a path request, and establishes + sustains a Link;
**link window scheduling in place**.

## Result: HOST PASS (duty-cycle link mechanism) · no_std transport/Link driver PENDING-HARDWARE

T4's new design content — **how a duty-cycled leaf holds a Link** (design §11
##1, the "decide before T4" open decision) — is resolved and implemented as a
host-tested scheduler in `crates/leaf-rns`. The no_std rns-core transport/Link
driver that sends real RNS frames through it is the on-device milestone (pairs
with T6; needs the production flash path).

## Open decision §11 #1 — RESOLVED (chosen: scheduled link windows + resume queue)

A half-duplex, duty-cycled leaf holding an RNS Link must stay awake to sustain
it. Chosen (the design doc's recommended option, for power):

**Scheduled gateway→leaf link windows with a resource resume queue.** The radio
RX is awake only during periodic link windows; an established Link is **held
across the dormant gap** (not torn down — sustaining is cheaper than
re-establishing every window); frames produced while dormant are **queued and
flushed at the next window open** (the seed of T6's resource resume queue for
uninterrupted transfers across gaps).

## `crates/leaf-rns` (design §5 `leaf-rns`)

`no_std` + `alloc` link-window scheduler (`LinkWindowScheduler`):
- `is_rx_awake(now)` — radio RX gate over the window schedule (window/gap).
- duty-cycle (`rx_duty_cycle`) — the honest power cost of being link-capable
  (persistent-link alternative = 1.0 duty; scheduled window = e.g. 0.2).
- **sustain**: an established link is held across dormant gaps, never
  auto-torn-down by duty cycling (`is_linked()` stays true while dormant).
- **resume queue**: `queue_tx` holds frames while dormant; `drain_tx` flushes
  only at a window open (frames survive the gap — the T6 resume-queue seed).
- keepalive pacing (`keepalive_due`) — a keepalive fires at a window open after
  one full silent period so the gateway sees the leaf alive each window.

5 host tests (via `bazel run //rust-client:test_leaf_rns`): window gating,
duty-cycle cost, link held across a gap, resume-queue flush-at-open-only,
keepalive-in-window-after-stale.

Also depends on `lma-identity` (T2, merged) for the leaf identity lifecycle;
`radio-interface` (T3, PR #175) integration folds in on merge (framing is
independent of the scheduler).

## Host link-capability proof (design T4 accept)
Establish + sustain of an RNS Link to a host std peer (Python RNS 1.3.5) is
demonstrated by the T0 interop harness (link establish, both-way link data);
the leaf-rns scheduler adds the duty-cycle "sustain across gaps" rule on top.
Driving real announciamento/path/Link RNS frames through the no_std leaf driver
over the radio is the on-device hardware leg (pending).

## Bazel-first
`crates/leaf-rns` + `//rust-client:{leaf_rns_sources, test_leaf_rns}` (manual).

## On-device hardware leg (held)
rns-core transport + Link manager wired into the no_std leaf over the radio, and
the §T4 accept exercised live with a Cardputer, is pending-hardware (production
flash path, T1-GATE.md mechanics; pairs with T6 Resource-over-Link).
