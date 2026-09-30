# LXMF Cardputer-Client Handoff (Rust no_std)

Status snapshot: **2026-09-30**. This is the resume doc for the Rust LMAO
client on the M5Stack Cardputer (ESP32-S3): production text-message send +
chart-data reply receive. Read `RUST-STACK-HANDOFF.md` for the earlier
milestones (beacons, RNS link, duty-cycle, LXMF client foundation); this doc
supersedes its "pending" section with the current, evidence-corrected state.

## Goal
A no_std Rust client on the Cardputer does the real LMAO round-trip **for a
long period**: periodically send a message to the production LMAO server and
receive + parse the server's chart-data reply. Exactly like the stable
MicroPython cardputer client, but from Rust + over the duty-cycled LoRa link.

## Topology (corrected — get this right)
- **selfhost** (`/home/pondycrane`) wires three **client** leaf devices on the
  LoRa mesh: the **cardputer** (our Rust firmware, `/dev/ttyACM0`), the
  **sprout**, and **sprout-light** (native-C soil-moisture clients; sprout UART
  `/dev/ttyUSB1` logged by `/tmp/sprout_mon.py` → `/tmp/sprout_monitor.log`).
- The **LMAO server runs in the k8s cluster** (`kubectl`, namespace `default`,
  pod `lmao-server-79d87655fc-w974g`) and **owns the separate RNode bridge**
  that joins the RF mesh. The sprout is a **peer client, NOT the bridge** and
  NOT a relay we control; its log is only a passive view of the shared RF
  channel.
- Endpoint authority for "did the server get it": **server pod logs**
  (`kubectl logs`), NOT the sprout log. The server allow-list gates on the
  sender's `lxmf/delivery` hash.

## Acceptance criteria (the bar)
Run ≥ 6 h at the 30 s send cadence; all checked against the two authoritative
endpoints (device serial + server pod logs):

### A. Outbound — message actually reaches the server
- [ ] Device serial shows `Hello msg … sid=8a17668171337ca80177c7464f6c9020`
      every ~30 s (correct whitelisted identity).
- [ ] **Server pod log**: `Message received — From: 99ce32311dc37193eff4951a912f8f1b  Title: p:Envelope  Content length: N bytes`
      at the same cadence, for ≥99% of beats.
- [ ] Server replies per message: `Reply: ACK from LMAO Server — …` + `Reply sent.`

### B. Inbound — chart data received + parsed on the cardputer
- [ ] Device serial shows `INBOUND lxmf … sig_valid=True` (decrypt + LXMF
      unpack + server-sig verify).
- [ ] Device serial shows `reply: ACK from LMAO Server — …`
- [ ] Device serial shows `CHART node=<node8> dry=.. wet=.. temp=[..] humidity=[..] samples=[..] water_mask=..`
      — the piggybacked `DATA …` line parsed on-device (replicates
      `cardputer_client/chart.parse_data_line`).
- [ ] Reply-decode success rate ≥95% of the cycles whose `Message received`
      the server logged.

### C. Durability ("long period")
- [ ] **No reset** across the whole window (watchdog/OOM history → must show
      continuous serial uptime/beat; no `TX FAILED — recovering radio`
      cascade, no heap-exhaustion halt, no silent radio wedge).
- [ ] No cumulative memory-growth halt (fragmentation; heap is 256 KiB —
      see `firmware/src/main.rs HEAP_SIZE`).
- [ ] ≥6 h of continuous bidirectional flow meeting A+B above.

## Current status (evidence-corrected)
Done / committed (branch `feat/rust-client-text-message`, **PR #195**, mergeable):
- 67f2cb2 — outbound text send: `LMAOEnvelope{text}` (field 20),
  `p:Envelope`, addressed to server `lxmf.delivery` (dad35b80…), LXMF-packed +
  server-pubkey-encrypted + signed, over SX1262 via RNode RF framing.
- 55d256b — inbound decode: decrypt (X25519 our prv), `lxmf_core::message::unpack`
  + server-sig verify, extract `LMAOEnvelope{text}`, parse `DATA …` line
  (replicated `chart.parse_data_line` incl. tolerance + watering mask),
  print `[rns] INBOUND`/`reply`/`CHART`. Plus Bazel: vendored no_std
  `rns-crypto` exposed via `vendor_sources` → `firmware_sources`; graph
  `bazel query //rust-client/...` = 25 targets; filegroup builds +
  `test_leaf_rns` gate run under Bazel; `cargo test --workspace` green.
- 161f7a8 — identity fixed: `NODE_IDENTITY_SEED` = the 64 bytes of
  `~/.local/share/lmao_client/lxmf/identity` ⇒ lxmf.delivery `99ce32…`
  (allow-listed in k8s `LMAO_ALLOWED_CLIENTS`), identity hash `8a1766…`.
  On-device `sid=8a1766…` verified.
- 6d180d3 — LXMF `source_hash` = the sender's `lxmf.delivery` hash
  (not raw identity hash), matching urns wire format.

### Ruled out with host/cluster evidence (do NOT reinvestigate these)
- **Identity not whitelisted** — fixed; 99ce32… is in the k8s allow-list.
- **Ratchet** — the lib DOES support ratchet (`rns_crypto::Identity::encrypt_with_ratchet`);
  NOT the blocker: server logs **zero** `Decryption with ratchets failed` at our
  30 s cadence (the 26 errors are old bursts ending 01:38 UTC).
- **LXMF source hash** — fixed; server still logs no cardputer message.

### The blocker — RESOLVED BY DIAGNOSIS (2026-09-29), fix tracked as L1/L2

**The handoff's earlier "implement RNS transport/routing" theory was wrong.**
Single-hop delivery to the server's local IN destination needs no path and no
transport: RNS 1.3.5 `Transport.inbound` delivers any well-formed DATA packet
whose destination hash matches a registered IN destination
(`_handle_data` → `destinations_map`). There is no transport node between the
Cardputer and the server's RNode; a HEADER_2/transport frame would be dropped
as transit-for-someone-else.

**Actual root cause — the LXMF opportunistic wire format.** Python LXMF sends
`RNS.Packet(dest, packed[LXMessage.DESTINATION_LENGTH:])`: the encrypted RNS
payload carries the LXMF wire message **without the leading 16-byte
destination hash**, and the receiver re-prepends it from the packet header
(`LXMRouter.delivery_packet`: `lxmf_data = packet.destination.hash + data`).
The Rust firmware encrypted the **full** packed message (dest included), so
the server's `LXMessage.unpack_from_bytes` misaligned (src parsed as the dest
hash, msgpack garbage) and LXMF dropped it with
`Could not assemble LXMF message from received data` (NOTICE) — the line the
earlier log greps never looked for. RNS-layer decryption succeeded all along
(hence zero ratchet errors).

**Evidence (offline sim, stock Python RNS 1.3.5 + LXMF 1.0.1, real server
identity, exact Rust packet bytes — `host/lxmf-sim/`):**
- Rust announce → **valid**: `Identity.recall(99ce32…)` + path installed.
- Old-format DATA → `Could not assemble LXMF message from received data`, no
  delivery.
- Fixed-format DATA (encrypt `packed[16:]`) → **delivery callback fires**:
  `From: 99ce32311dc37193eff4951a912f8f1b  Title: p:Envelope  Content length: 86`
  — the exact production "Message received" line.

The inbound reply path has the **mirror bug** (decrypt → must prepend our
`lxmf.delivery` hash before `lxmf_core::message::unpack`) — fixed under L2.

## Next steps (in priority order — the ticket plan is `LXMF-CLIENT-PLAN.md`)

1. **L1 (#197)** — fix the wire format: encrypt `packed[16..]`; reference
   `random_hash` layout (`urandom(5)‖time(5,BE)`); extract the pure wire
   logic into `crates/leaf-lxmf` (no_std, on rns-core/lxmf-core — no
   hand-ported protocol) + host regression tests.
2. **L2 (#198)** — inbound mirror fix + reply-decode host test.
3. **L3 (#199)** — re-flash + verify **A** live (server pod
   `Message received — From: 99ce3231…`; older logs should show the
   `Could not assemble` NOTICEs at pre-fix beats — production confirmation
   of the diagnosis).
4. **L4 (#200)** — verify **B** on device serial (`INBOUND`/`reply`/`CHART`).
5. **L5 (#201)** — run the ≥6 h durability window (**C**); check the boxes
   below with evidence.

## Key facts / commands / constants
- Server identity: `~/.local/share/lmao_server/lxmf/identity` (64 B).
  delivery dest `dad35b80164b25f7b1474be86e443702`; pubkey
  `1985ac0ef98f17d26671f2f9ea31c0593a90fec1a30579fa68545a0cc0159420789018213f3115239d0ed1036cf3375ee75cd410318eda87fb9420ce7a4ca788`
  (consts `SERVER_LXMF_DELIVERY_HASH` / `SERVER_PUBLIC_KEY` in
  `firmware/src/rns_link.rs`).
- Client (cardputer) canonical identity: `~/.local/share/lmao_client/lxmf/identity`
  (64 B) ⇒ lxmf.delivery `99ce32311dc37193eff4951a912f8f1b`, identity hash
  `8a17668171337ca80177c7464f6c9020`; `NODE_IDENTITY_SEED` const carries those
  64 bytes. Whitelist (k8s `LMAO_ALLOWED_CLIENTS`):
  `7b38fa21…,f5f05952…,277bf9fb…,99ce3231…`.
- Our announces alternate `lmao.leaf` / `lxmf.delivery` every ~20 s
  (`beat % 100`/200), Hello text every 30 s (`beat % 300`), radio awake
  200 ms/1 s (`LinkWindowConfig::new(200,800)`).
- Build: `source ~/export-esp.sh && cd rust-client/firmware && cargo build --release`.
- Flash: `espflash flash --before usb-reset --port /dev/ttyACM0 --chip esp32s3
  --after hard-reset target/xtensa-esp32s3-none-elf/release/lmao-firmware-t1`.
- Host tests: `cargo test --workspace` with
  `PROTOC=$HOME/.cache/bazel/_bazel_pondycrane/2e2a7dbb4bcc6a44b4cc7a6c45e57d4d/execroot/_main/bazel-out/aarch64-opt-exec-ST-d57f47055a04/bin/external/protobuf~/protoc`.
- Server logs: `kubectl logs -n default lmao-server-79d87655fc-w974g [--since=10m|--tail=N]`.
- Device serial: `cat /dev/ttyACM0`; mesh view (secondary): tail
  `/tmp/sprout_monitor.log`.
- LMAO envelope: `LMAOEnvelope{text: TextMessage{1:node_id,2:content,3:ts}}`;
  server reply content = `LMAOEnvelope{text}` with content
  `"ACK from LMAO Server — received your message (N bytes)\nDATA …"`.
  `DATA …` format: `node dry wet ct temp… ch humidity… cm samples… [mask]`
  (emitter: `lma_core/sprout_history.data_line`). Sensor envelope uses field 10.
- Honesty rule: never claim server-side delivery/reply unless observed in the
  **server pod logs**. On-air (sprout RXP2P) ≠ delivered.

## Open risks
- **Inbound duty window**: the 200 ms/1 s RX duty may clip the server's reply
  (half-duplex turnaround, cf #151 on the native client) — L4 measures the
  real rate; `LinkWindowConfig::new(200,800)` in `firmware/src/main.rs` is
  the knob.
- **Heap churn**: 128 KiB static heap (`HEAP_SIZE`); the earlier 32 KiB
  build fragmented to exhaustion in ~8 min. L5 watches this.
- Transport is OFF the table (see blocker section): do not re-add it as a
  fix attempt.
