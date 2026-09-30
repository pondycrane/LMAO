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

### The blocker (real next milestone)
The server **never receives the packet at the RNS layer** — no `Message
received`, no decrypt attempt. Cause: the client does `RawPacket::pack` + a
raw air TX with **no RNS transport/routing**, so the mesh does not route the
frame to the server's RNode bridge. The stable urns client ran
`Packet(dest).send()` through the RNS Transport/router (path via announces,
hop-by-hop). We deferred transport as "client-only scope" (handoff noted
`rns-core::transport` is NOT std-gated — viable); without it delivery cannot
happen.

## Next steps (in priority order)
1. **Implement RNS transport/routing on the client** (the outbound deliver
   path) so the packed DATA packet routes to `dad35b80…` across the mesh.
   This is the only way to get criterion A green. Substantial, multi-step.
2. Re-flash + verify **A** (server pod `Message received — From: 99ce3231…`).
3. Then **B** should fire (inbound decoder already in place) — confirm
   `INBOUND`/`CHART` on device serial.
4. Run the ≥6 h durability window (**C**); watch for resets/OOM/radio wedge.
5. Update this doc's checkboxes as each criterion passes with evidence.

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

## Open risk
Transport integration may pull in more of rns-core (routing table, path
announces, hop ACK) than the current minimal build carries; heap headroom at
256 KiB is the constraint to watch (earlier fragmentation OOM'd the LXMF
path). Keep the change incremental and re-run the durability criteria.
