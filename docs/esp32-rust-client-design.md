# ESP32 Rust LMAO Client — Design Doc & Ticket Plan

Status: DRAFT (decision gate T0 not yet run) · Owner: pondycrane
Target: a lean LMAO *leaf* client in Rust (`no_std`).
**E2E test rig: M5Stack Cardputer ADV** (ESP32-S3, Cap LoRa-1262 SX1262, display);
the code stays portable to the Atom Lite (ESP32-PICO-D4).
Sources of truth this defers to: `README.md`, `AGENTS.md`, `proto/lma_messages.proto`,
`firmware_common/`, the Sprout native client (`smart_irrigation/native-client`).

> **2026 directive (supersedes prior draft):** *Forget LMAF.* The current transfer
> framing will be **redone after we have stable RNS resource transfer on the Rust
> stack.** Therefore the critical feature below is now **RNS native `Resource`
> over `Link`**, and LMAF / any successor framing is a **post-stabilization
> rebuild** (Ticket 8), not a near-term deliverable.

---

## 1. Goal & non-goals

**Goal.** Get **stable RNS `Resource` transfer on the Rust stack** — the new
transfer substrate for LMAO's clients — running from a Rust `no_std` leaf that
stays **wire-compatible with the existing LMAO mesh** (production RNode +
Python RNS/LXMF `lmao-server`). Milestone order:
(1) prove Resource transfer interops with Python RNS on a host, (2) port link +
resource receive to the ESP32 leaf (**E2E-tested on the Cardputer**), (3) rebuild
LMAF / attachment framing **on top of** the stable Rust Resource path (Ticket 8)
— explicitly not in scope until the resource substrate is stable.

**Standing mandate — E2E rig = Cardputer; MicroPython stays until Rust is E2E:**
All end-to-end gates run on the **M5Stack Cardputer** (ESP32-S3 + SX1262 + display).
The Rust client is **additive / opt-in** (same shape as the existing
`--native-cardputer` opt-in) and the **working MicroPython `urns` stack must be
kept untouched** — the default MicroPython Cardputer path keeps passing its own
gates until the Rust stack is working E2E. Never delete/alter
`cardputer_client/*.py` µReticulum code in service of the Rust port.

**Non-goals.**
- Not running the full Python Reticulum on the ESP32 (too heavy).
- Not a from-scratch C++ client (tried, didn't work — the cost of custom porting
  is the reason we borrow a Rust protocol core, §7).
- Not rebuilding LMAF now. Deferred to T8, on the Resource substrate.
- Not a full LXMF router / propagation node on the leaf.
- Not replacing the existing native C Sprout/Cardputer client.

---

## 2. The critical feature now: RNS native Resource over Link

This is the milestone that everything else waits on.

An **RNS native `Resource`** is a chunked, integrity-checked payload transfer
carried **over an established `Link`** — the reference Reticulum primitive for
attachments (the C++ RTReticulum `Resource` in the existing tree is a partial,
non-wire-matching stand-in; the Rust port is verified against the real wire).
"Stable" means: reliable TX and RX over a link, with the reference behavior —
chunk sequencing, per-chunk hash map, retransmission of missing parts,
completion verification — and demonstrable **interop with the Python RNS
server**, plus **resume/persistence** so a mostly-awake leaf can complete a
transfer across duty-cycled gaps.

On the Rust side this maps to **`rns-core`'s Link + Resource machinery**
(`rns-core` is `no_std`, zero-dep, and ships `Resource` per §3). The leaf work is
therefore:
1. **Link capability on the leaf** — establish links, serve inbound link
   requests from the gateway, hold link state (ratchet/transport) over the
   half-duplex radio.
2. **Resource RX** — receive a resource over that link; reassemble
   hash-verified to flash (streamed, never assembled in RAM); ACK completion.
3. **Resource TX** — send a leaf-originated resource (e.g. sensor history,
   firmware-style payload) to a gateway link peer.
4. **Persistence/resume** — RFCNS resource buffers survive sleep so the leaf
   finishes large transfers in pieces (the ECG of "stable" here).

**LXMF** (via `lxmf-core`) is still on the leaf for the *control* messages
(`SensorReport`, discovery, command ack) — LMAO's existing `LMAOEnvelope`
"other" payloads — but **transfer of large payloads moves off LXMF-opportunistic
and onto Link-Resource.** That is the whole point of the directive.

**Hardware honesty (risk, decided in T0/T3):** a half-duplex, duty-cycled LoRa
leaf must now **hold a Link**, which requires sustained bidirectional traffic at
the gateway/leaf boundary (link keepalive + resource chunks) — unlike the
sleep-when-idle Leaf-opportunistic model. This raises the leaf's RX duty and
power budget. It is a real engineering constraint, not an impossibility (links
over LoRa are used in the wild), but the design must address it explicitly:
scheduled link windows, the radio never fully sleeping while serving, and a
resource resume queue so interrupted transfers continue. Flagged as the #1 open
decision (§11).

---

## 3. Stack selection

The stated stack — **`no_std` + `rns_core` + `lxmf_core`** — maps exactly to
**lelloman `rns-rs` + `lxmf-rs`** (crates.io crates are literally `rns-core` /
`lxmf-core`, both `no_std`). Recommended **primary**.

| Crate | no_std | Provides (what LMAO needs) |
|---|---|---|
| `rns-crypto` | ✅ zero deps | X25519, Ed25519, AES-128/256-CBC, SHA-256/512, HMAC, HKDF, Identity |
| `rns-core` | ✅ zero deps | wire protocol, transport routing, **Link/Channel/Buffer**, **Resource transfer**, holepunch |
| `rns-net` | ❌ (std) | reference host interfaces (Serial/KISS/**RNode**/TCP/UDP) — **porting reference only** |
| `lxmf-core` | ✅ | `message::pack/unpack` (LXMF wire + signing), stamp validation — **control messages only** |
| `lxmf` | ❌ (std) | full router/delivery — **do not pull onto the leaf** |

**Why primary = lelloman:** oldest faithful port of the Python references,
900+ Python-generated interop vectors, claims live announce/link/**resource**
interop with Python RNS, and `rns-core` is zero-dep — the smallest no_std core
for a leaf, and it is the one that bundles **Link + Resource** in `no_std`.
`lxmf-core` is exactly the thin control-message layer LMAO wants (for the
`SensorReport` side, which stays LXMF).

**Alternative: `FreeTAKTeam/LXMF-rs` (~0.9.x).** Derived fork with "embedded"
crates (`rns-embedded-*`, `lxmf-embedded-mini`, C ABI FFI), but **disclaims
drop-in wire compatibility** with Python and carries a heavier, modified surface
(SDK, JSON-RPC, WASM hooks). **Fallback only** if T0 shows lelloman can't
interop (esp. its Link+Resource). Do not start there.

**Decision rule:** choose on T0 evidence against the live mesh, not READMEs.

---

## 4. Toolchain (E2E rig = ESP32-S3; code portable to classic ESP32)

**Primary E2E target: M5Stack Cardputer ADV** = ESP32-S3FN8 (Xtensa LX7, 512 KB
SRAM, 8 MB flash) + **Cap LoRa-1262 (SX1262)** via the rear EXT 14-pin header (see
README §5a pinout) + ST7789 display. This is what every E2E gate runs on.
Portable to the Atom Lite (ESP32-PICO-D4, Xtensa LX6) — both Xtensa no_std.
- esp-hal supports **ESP32-S3** (target `xtensa-esp32s3-none-elf`) and **classic
  ESP32** (`xtensa-esp32-none-elf`); GPIO, UART, SPI, I2C, timers, RNG, sleep.
- Requires **nightly** + pinned Xtensa target via **`espup`**
  (`esp-hal`, `esp-println`, `esp-alloc`, optional `embassy-executor`; storage via
  `esp-storage` NVS). Record toolchain in `rust-toolchain.toml`; pin the vendored
  crates in `Cargo.lock` + an `UPSTREAM.md` entry (mirror the C side's
  `.rtreticulum` vendoring).
- **Radio:** the Cardputer's SX1262 (SPI, HSPI host; TCXO 1.8 V on DIO3; DIO2 as
  RF switch — copy `cardputer_client/firmware/main/lora_interface.cpp` settings).
  RF params fixed: 868/BW125/SF7/CR4:5/pre24/syncword 0x1424 — match the server,
  **do not renegotiate**. For Link support the interface must sustain
  bidirectional traffic during link windows (§2). The existing MicroPython paths
  (`lora_boards.py`, µReticulum) are reference behavior only — not modified.

---

## 5. Architecture / module layout

New tree under the repo (mirrors `smart_irrigation/native-client` conventions):
`rust-client/` with `host/` (std, host-testable interop) + `firmware/` (no_std,
esp-hal) + pinned vendored deps + a Bazel build target.

```
rust-client/
├── Cargo.toml / rust-toolchain.toml        # espup nightly + xtensa target, pinned
├── UPSTREAM.md                             # rns-rs/lxmf-rs commit pins + protocol note
├── vendor/rns-rs, vendor/lxmf-rs           # pinned (cargo vendor / git submodule)
├── crates/
│   ├── lma-wire/                           # prost-generated LMAOEnvelope + SensorReport
│   │                                       #   from proto/lma_messages.proto (control msgs)
│   ├── leaf-rns/                           # rns-core wiring: transport, Link manager
│   │                                       #   (establish/serve/sustain), identity lifecycle
│   ├── leaf-resource/                      # RNS Resource RX/TX over Link; stream-to-flash
│   │                                       #   sink; retransmit/hash-map; resume queue
│   └── radio-interface/                    # impl rns_core::Interface over UART-AT DTU
│                                           #   or SPI SX1262 via esp-hal; frame pacing
├── firmware/app                            # no_std binary: main loop, link windows, sleep
├── host/interop                            # std harness: live-mesh interop (T0/T7)
└── BUILD                                   # Bazel mirror (host tests, hardware gates)
```

Data-flow on the leaf:
`radio (esp-hal UART/SPI)` → `radio-interface` (rns_core::Interface) →
`leaf-rns` (transport + **Link manager**: establish/serve/sustain) →
`leaf-resource` (Resource RX→flash, TX; hash-map verify; resume queue) →
`lma-wire` (control: `SensorReport` via LXMF; later: successor framing on
Resource at T8).

---

## 6. DRY across implementations (the real risk)

`firmware_common/` is C++, the server is Python(protobuf), this is Rust — sources
can't be shared. Two distinct DRY surfaces:

**a) RNS/LXMF wire (host, "protocol core").** Comes from the upstream Rust crates
grounded by their interop vectors — do not re-implement. The *stability* of Link
+ Resource on the Rust side *is* the milestone and is measured by T0/T7 interop.

**b) LMAO control wire (`SensorReport`, `LMAOEnvelope`).** `proto/lma_messages.proto`
is the single schema the server + C++ already pin to. Generate Rust types with
**prost** from the same `.proto` — not a hand-written second encoder — so a
schema change lands everywhere at once. Shared golden vectors (CRC-32
zlib/EDB88320, sha256 digest semantics, the existing 240 B opportunistic budget
for control frames) get cross-language equivalence tests (Rust ↔ server fixture
↔ firmware_common results).

**c) Transfer framing (LMAF / successor).** Explicitly **out of scope now**. When
it is rebuilt (T8) it sits *on* the stable Resource layer; keep its wire contract
in the `.proto` and protobuf-generated like everything else so it cannot drift
from the server or the C++ tree.

---

## 7. Why Rust here (and what "C++ didn't work" really was)

C++ wasn't the blocker — the C++ RTReticulum stack works on this class of board
(Sprout is field-proven, "RAM to spare"). The cost of a *from-scratch* C++
protocol client (RNS wire, Link/Resource, LXMF, crypto) is high because it's all
custom porting with no reusable foundation. Rust buys a reusable, maintained,
host-tested RNS+LXMF+**Resource** protocol library (`rns-core`/`lxmf-core`,
`no_std`, zero-dep, 900+ vectors), so LMAO only writes its thin leaf layers:
radio interface, identity persistence, link-window scheduling, and the
record-streaming to flash — the anti-drift machinery in §6 lives where LMAO
actually owns code. That's the point: **borrow the protocol core, own only the
leaf.**

---

## 8. Memory budget (estimate — verify in T1/T2/T6)

Honest number TBD by measurement. **Link + Resource machinery is RAM-heavier than
bare opportunistic LXMF**: link state/ratchet buffers, per-chunk hash maps, and
the reassembly window live in RAM, with payload streamed to flash. Expectation
is it still lands within ~320 KB usable SRAM (the C++ client "has RAM to spare"
with full RNS+LXMF), but a `cargo-size`/`espflash` measurement **per feature
(T1 minimal, T2 identity, T6 link+resource)** is a hard deliverable, not an
assumption. Deliberate omission keeping it small: **no `lxmf` router crate**
(std) on the leaf — only `lxmf-core` for control + `rns-core` for transport/link/
resource.

---

## 9. Risks

- **Young, self-reported upstream** (`rns-rs`/`lxmf-rs` sub-1.0; no published
  ESP32 integration; Resource stability claimed but is the thing we must prove).
  → **T0 is a hard gate**, now specifically exercising **Link + Resource** interop.
- **Link on a half-duplex duty-cycled leaf** — the direct consequence of moving
  transfers to Link-Resource: the leaf must stay awake/servicing during link
  windows. Requires scheduled RX windows + resource resume; power/duty impact is
  the #1 open decision (§2, §11).
- **Wire-compat drift** across C++/Python/Rust → §6 schema+vector controls.
- **Classic-ESP32 Xtensa no_std** needs nightly + pinned espup toolchain (friction,
  not a blocker).
- **Flashing discipline** — Atom Lite allows esptool only for the one-time
  install/backup (115200); routine flashes via the sanctioned Bazel target,
  supervised, like the Sprout native client (AGENTS.md).

---

## 10. Ticket plan (dependency graph + acceptance gates)

> **Tracked as GitHub issues.** Epic **#157**; T0–T9 = **#158–#167** (see
> `#157` for the full graph + the two standing mandates: Cardputer E2E rig, and
> keep the MicroPython `urns` stack until the Rust stack is working E2E).

Sequencing: **T0 gates everything device-related.** T1–T4 bring up the no_std
core + **link capability**. T5 is the LXMF control path (SensorReport). **T6 is
the new critical milestone: Resource over Link on the leaf.** T7 is
integration/E2E. T8 = the LMAF/successor rebuild (deferred per directive). T9 =
power polish.

```
T0 (host spike: announce/path + LINK + RESOURCE interop) ──► gates ─► T1 ─► T2 ─► T4 ─► T5 ─┐
                                                              └► T3 ─► T4 ─────────────► T6 ─► T7 ─► (milestone: stable RSS resource on Rust stack)
                                                                                            │
Stretch / deferred: T8 (rebuild LMAF on Resource), T9 (power/duty-cycle)
```

### T0 (#158) — Wire-compat + Link/Resource spike (HOST; DECISION GATE)
Vendor + pin `rns-rs`/`lxmf-rs`. Build a **std desktop** Rust node (`rns-net` +
`lxmf`) against the **live production mesh** (RNode + Python server): join +
announce + path-request; then **establish a Link to the Python server and run an
RNS Resource TX and RX both ways**, verifying chunk/hash-map/reassembly and
completion against the server.
**Gate (pass/fail):** RSS Resource transfers interop with the production Python
server in both directions, exactly like the reference implementation. **If fail:
stop — review the FreeTAKTeam fork, or terminate.** This is the fuse for
everything device-related.

### T1 (#159) — no_std toolchain + blink/boot (Cardputer)
espup nightly + `xtensa-esp32s3-none-elf`; `rust-toolchain.toml`; esp-hal no_std
binary boots on the **Cardputer**; `espflash` via a Bazel target.
**Accept:** boots, logs chip/mac; `cargo-size` snapshot (minimal).

### T2 (#160) — RNS identity + persistence
Port `lma_identity` to Rust: mint/store identity in NVS, print the
`lxmf.delivery` hash (add to server `ALLOWED_CLIENTS`).
**Accept:** host unit test derives `DEST` == the hash the C client prints;
identity survives reset; `cargo-size` snapshot.

### T3 (#161) — Radio Interface (esp-hal) + framing
Implement `rns_core::Interface` over the **Cardputer SX1262 (SPI/HSPI)** with
RF params (868/BW125/SF7/CR4:5/pre24/syncword 0x1424) + duty pacing; port stays
radio-generic for a future UART-AT DTU/Sprout profile.
**Accept:** host-test the frame RX/TX demux against `lma_rnode_framing` vectors;
on-device radio loopback.

### T4 (#162) — RNS leaf transport + LINK capability
Register the interface + rns-core transport; inbound transport processing; then
**Link support on the leaf**: establish a link, and **serve inbound link
requests** from the gateway, holding link state.
**Accept:** with a host std peer the leaf announces, answers a path request, and
**establishes + sustains a Link**; link window scheduling in place.

### T5 (#163) — SensorReport TX (LXMF control path)
prost-generate `SensorReport`; pack + sign with `lxmf-core` + `rns-crypto`
identity; send to the discovered delivery destination (path_find analog).
**Accept (E2E):** report lands in the server's DuckDB pipeline like the C client.

### T6 (#164) — Resource over Link on the leaf (CRITICAL MILESTONE)
`leaf-resource`: RNS **Resource RX** over the T4 link — stream chunks to flash,
hash-map, verify, ACK completion — plus **Resource TX** from the leaf, plus a
**resume queue** so an interrupted/large transfer completes across link windows
(the definition of "stable").
**Accept (host):** cross-language equivalence vs server-encoded resource vectors.
**Accept (E2E):** a payload sent as an RNS Resource from the production server is
reassembled on the leaf, digest-verified, and completed; a leaf-originated
resource is received by the server — both matching the reference wire/interop.

### T7 (#165) — Integration + packaging + E2E
Crate layout finalized per §5; `bazel test //rust-client:...` host + hardware
gate; flash + full **E2E on the Cardputer** against RNode + server.
**Accept:** **stable RSS Resource transfer on the Rust stack** (the named
milestone) demonstrated end-to-end on the Cardputer and documented in README.

### T8 (#166, deferred per directive) — rebuild LMAF / successor on Resource
Apply the new transfer framing **on top of** the stable T6/T7 Resource substrate
(still protobuf-schema'd + vector-tested per §6). Not started until T6/T7 gates
pass.

### T9 (#167, stretch) — Power/duty-cycle polish
Deep sleep between link windows, watchdog + heap-fragmentation guard (from
`cardputer_client` #71/#74), RSSI reporting.

---

## 11. Open decisions for the reader
1. **How does a link-capable leaf serve under duty-cycle?** [the #1 decision]
   Scheduled gateway→leaf link windows with a resource resume queue (recommended)
   vs a leaf that holds a persistent link (higher power). Decide before T4.
2. **Radio on the Atom Lite:** UART-AT RAK3172 DTU (Sprout-style) vs SPI SX1262 —
   affects T3 scope. Default: whichever the production board carries; pin in T3.
3. **Vendor method:** `cargo vendor` tree vs git submodule — pick in T0; record
   pin in `UPSTREAM.md`.
4. **async (embassy) or sync loop** on the leaf — decide in T1; sync loop is
   smaller and matches the C client.
