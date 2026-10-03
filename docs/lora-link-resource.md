# LoRa bidirectional link + Resource transmission (Sprout ↔ server)

How the Rust Sprout (RAK3172 DTU P2P LoRa) and the Rust LMAO server (RNode
interface) establish a two-way RNS Link and push the sensor `SensorReport` as a
Resource — the mechanism behind `RF RESOURCE received bytes=… sha256=…`.

## Roles

| End | Radio | Engine | Role in the link |
|-----|-------|--------|------------------|
| **Sprout** (`firmware-sprout`) | RAK3172 DTU, P2P LoRa over UART2 AT | `lma-link-resource` (initiator) | announces its identity, sends `LINKREQUEST`, pushes the Resource |
| **server** (`lmao-server-rs`) | RNode, returns `lmao.data` (dest `24a09704…`) | `rns-net` (responder) | receives the link, audits LRPROOF, receives/decodes the Resource |

Both radios are in **continuous RX** (`AT+PRECV=65535`, re-armed after every
TX), so a party is always listening; there is no duty-cycled sleep window to
schedule. The "sync" a peer needs is not a time slot but a **validated path +
the responder identity**, which arrives via **announces**.

---

## 1. Path discovery — who broadcasts what

- **Sprout** broadcasts two signed announces every `ANNOUNCE_INTERVAL_MS` (30 s):
  `lmao/sprout` and `lxmf/delivery` (its node identity).
- **server** broadcasts `lmao/data` on a cadence (`LMAO_ANNOUNCE_INTERVAL`,
  default 30 s) via `RnsNode::announce()`. The computed hash must equal the
  registered link destination — verified equal to `24a097043d6d7f8fe…`.
- Each announce signed by its identity + delivered over real RF lets both RNS
  cores record a **1-hop path** and cache the peer's **public key** (required to
  run the link-cipher key derivation).

> Why this is essential: the Rust server was TX-silent on the RNode
> (`announce=0`, `tx=0`) — no `lmao.data` path ever radiated, so the Sprout
> could not `LINKREQUEST`. The periodic announce restores that broadcast.

---

## 2. Bidirectional link handshake

Initiator = **Sprout**; responder = **server**.

1. **Uplink — `LINKREQUEST`.** `LinkResource::begin_link` builds a
   `PACKET_TYPE_LINKREQUEST` targeted at `lmao.data` (hops=1) via
   `LinkEngine::new_initiator(…, LinkMode::Aes256Cbc, …)`, sets `link_id` from
   the packet's hashable part, and moves to `Linking`. Retried on a cooldown
   (`LINK_RETRY_MS`); a stalled handshake is `reset()` by the lost-LRPROOF guard.

2. **Downlink — `LRPROOF`.** The server's rns-net responder answers. This is
   where the routing fix matters: responder-side `SendPacket` used to carry
   **no route hint** (only the initiator's `CreateLink` set one), so the
   transport dropped the LRPROOF. The fix attaches the **receiving interface**,
   so the server actually transmits it on the RNode.

3. **Uplink — `LRRTT`.** The Sprout's `pump_inbound` validates the LRPROOF
   (`handle_lrproof` against `SERVER_ED25519_PUB`), derives + holds the AES
   **session key**, sends `LRRTT` (link-encrypted), and enters `Active`.

Handshake is then 2-way and the link usable for a Resource.

---

## 3. Resource transmission (Sprout → server)

Triggered by the shared `ResourcePump`, which drives `LinkResource`.

1. **Queue.** `res_pump.set_payload(env)` arms the payload (the sensor
   `LMAOEnvelope`). `ResourcePump::tick` only does anything while a payload is
   set + a cooldown has elapsed.
2. **Start.** Once `established()`, `start_resource`:
   - derives the link session *token* + RTT;
   - encrypts the whole payload once with the link cipher (deterministic AES-CBC
     token path, no rng);
   - `ResourceTx::new` partitions it into `RESOURCE_SDU` (160-B) parts;
   - **advertises** (`CONTEXT_RESOURCE_ADV`, link-encrypted) with hash/size/
     metadata → `Advertised`.
3. **Serve parts.** The server opts in (`on_resource_accept_query` → true),
   then sends `CONTEXT_RESOURCE_REQ` for wanted parts. The Sprout's
   `pump_inbound` serves them as `CONTEXT_RESOURCE` link-DATA packets (carried
   raw — rns-net does not link-decrypt parts) → `Transferring`. Parts are
   re-advertised on `RES_POLL_MS` via `poll_resource` so a lost frame gets
   re-sent.
4. **Proof + completion.** The server sends a `CONTEXT_RESOURCE_PRF`; the Sprout
   `handle_proof` → `Completed`.
5. **Ingest.** Server `on_resource_received` logs
   `RF RESOURCE received bytes=N sha256=HASH` (the acceptance line) and folds
   the payload into `handle_delivery(…, "p:Envelope")`.

`ResourcePump` clears the queue on `Complete|Failed` so the next sensor bundle
can push.

---

## 4. Physical transport (RAK/RNode framing)

From `lma-dtu::at_dtu`:

- Every on-air frame = a **1-byte RNode header** (upper-nibble `seq` + bit0
  `SPLIT`) prepended to the RNS packet bytes — byte-identical to the server
  RNode / native `DtuInterface` framing.
- ≤ `DTU_FRAME_PAYLOAD` (254 B) → one `AT+PSEND`. Larger packets → two frames,
  both flagged `seq|0x01`.
- TX per frame: `AT+PRECV=0` → `AT+PSEND=<hex>` → `AT+PRECV=65535` (re-arm RX;
  the RAK is half-duplex, so it only hears again once RX is re-armed — the
  1200 ms post-PSEND drain that starved the LRPROOF must **not** be used).
- RX: `AtRx` parses `+EVT:RXP2P`, hex-decodes, and reassembles whole packets
  via the shared `SplitAssembler`.

---

## 5. Lifecycle + invariants (learned the hard way)

| # | Invariant | Failure if violated |
|---|-----------|---------------------|
| 1 | Server announces `lmao.data` on the RNode | Sprout has no path/identity → never `LINKREQUEST` |
| 2 | Responder LRPROOF carries the RX interface | Server TX-silent on the downlink; handshake never completes |
| 3 | `at_write` loops until the whole AT line (incl. CRLF) is written | esp-hal `UartTx::write` fills one 128-B FIFO/call; commands > 128 B silently truncated → the "≥160 B rejected" red herring |
| 4 | RAK RX re-armed immediately after each `PSEND` | Half-duplex radio deaf → LRPROOF / replies missed |
| 5 | Link retry cooldown slow enough for the half-duplex channel | A fast `LINKREQUEST` flood keeps the RAK toggling PRECV/PSEND and starves even the announces |

**Link states:** `Idle → Linking → Active → Advertised → Transferring →
AwaitingProof → Complete` (or `Failed`), with `reset()` on stalls.
