# T6 — Resource over Link on the leaf (CRITICAL milestone): evidence

**Ticket:** T6 (#164). RNS **Resource RX** over the T4 link — stream chunks to
flash, hash-map, verify, ACK completion — plus **Resource TX** from the leaf,
plus a **resume path** so an interrupted/large transfer completes across link
windows (the operative definition of "stable").
**Accept (host):** cross-language equivalence vs server-encoded resource vectors.
**Accept (E2E):** a payload sent as an RNS Resource from the production server is
reassembled on the Cardputer, digest-verified, completed; a Cardputer-originated
resource is received by the server — matching the reference wire.

## Result: HOST PASS (leaf Resource machinery + cross-language hashes) · live E2E-on-Cardputer PENDING-HARDWARE

`crates/leaf-resource` drives `rns-core`'s `ResourceReceiver`/`ResourceSender`
state machines the way the leaf must — RX through to hash-verified `assemble` +
completion proof, TX of a leaf-originated resource to a gateway, and a resume
path where a partial transfer survives a dormant link window (the T4 sustain
rule, now at the Resource layer). The X E2E (production server ↔ Cardputer over
LoRa) is the on-device hardware leg (T7 integrates transport + radio).

## `crates/leaf-resource` (design §5 `leaf-resource`)

`no_std` + `alloc`, on top of `rns-core`'s Resource machinery (the design §2/§3
primary stack; SHA-256 pins in `Cargo.lock` match `repositories.bzl`):
- **Rx** (`ResourceRx`): `from_advertisement` → `accept` → `feed_part` /
  `feed_hmu` → progress; `assemble_with(decrypt_fn, store)` decrypts the link
  cipher, hash-verifies against the advertisement (`HashMismatch` on tamper),
  streams the reassembled payload into the `Store`, and yields the completion
  proof to ACK back. Raw parts stay on the heap while inflight (rns-core's
  model); the `Store` trait is the flash-persistence seam for the on-device leg.
- **Tx** (`ResourceTx`): `new` → `advertise` → `handle_request` →
  `handle_proof`; a leaf pushes sensor-history/firmware-style payloads.
- **`Store`** trait = the flash boundary (`VecBackedStore` in host tests).

Host tests (via `bazel run //rust-client:test_leaf_resource`), 6:
- `resource_hash_matches_python_rns_135`, `map_hash_matches_python_rns_135`,
  `expected_proof_matches_python_rns_135` — **cross-language equivalence**: the
  resource hash / per-part map hash / completion proof are byte-identical to
  the Python RNS 1.3.5 server (goldens from `RNS.Identity`, RNS 1.3.5 — the T0
  interop baseline). Confirms the leaf verifies exactly what the server encodes
  (§6b).
- `leaf_receives_hashes_and_completes_resource` — multipart RX: receiver builds
  from the gateway advertisement, pulls all parts across windows, `assemble`
  re-matches the source bytes, produces the proof, status `Complete`.
- `leaf_resumes_across_dormant_gap` — the transfer is cut mid-parts; the same
  receiver (retained across the dormant link window) resumes and completes when
  the window reopens — the T4-sustain resume at the Resource layer.
- `leaf_originated_resource_received_and_verified_by_gateway` — leaf TX: the
  gateway receiver reassembles the leaf's payload; sender reaches `AwaitingProof`
  with all parts served.

## Cross-language note (RNS 1.3.5)
Verified in the T6 venv: RNS 1.3.5 `Identity.full_hash = SHA-256`, and the
resource `hash = full_hash(data + random_hash)`,
`map_hash = full_hash(part + random_hash)[:4]`,
`expected_proof = full_hash(data + hash)` — which is exactly `rns-core`'s
`compute_resource_hash` / `parts::map_hash` / `compute_expected_proof`. The
pinned goldens lock the Rust side to the server.

## Bazel-first
`crates/leaf-resource` → `//rust-client:{leaf_resource_sources, test_leaf_resource}`
(manual sh_binary).

## On-device hardware leg (held)
Carrying a real RNS Resource over the radio (production server ↔ Cardputer over
RNode/LoRa, digest-verified + completed both directions) is the T7 integration +
E2E milestone. The MicroPython Cardputer stays live until the Rust client is E2E.
