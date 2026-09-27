# T2 — RNS identity + persistence (leaf-rns): evidence record

**Ticket:** T2 — "RNS identity + persistence". Port `lma_identity` to Rust:
mint/store identity in NVS, print the `lxmf.delivery` hash; **accept:** host
unit test derives `DEST` == the hash the C client prints; identity survives
reset; `cargo-size` snapshot.

## Result: HOST PASS

`crates/lma-identity` (the §5 `leaf-rns` identity core) is ported and the
delivery-DEST derivation is **verified byte-for-byte against Python RNS 1.3.5**
— the exact version the production `lmao-server` runs, and the same RNS wire
hash the C client (`lma_identity`, RTReticulum) emits. All 5 host tests pass
(also via `bazel run //rust-client:test_lma_identity`).

## Key finding: the DEST is *not* the raw identity hash

`lma_identity::delivery_hash` (C: `Destination(identity, OUT, SINGLE,
"lxmf","delivery").hash`) is the RNS rule
`sha256(name_hash("lxmf.delivery") || identity_hash)[:16]` — **not**
`identity.hash` directly. Reproduced via `rns_core::destination::destination_hash`.

Cross-language goldens (generated from RNS 1.3.5, pinned in tests):

| fixed 64-byte private key | identity.hash | lxmf/delivery DEST (golden) |
|---|---|---|
| `00..3f` | `aca31af0441d81dbec71e82da0b4b5f5` | `fae321c442e3c9bdcd7a3e79d850e03c` |
| `32×AB ‖ 32×CD` | — | `ef969713c140949ad3d1af36d64e963c` |

## Deliverable

`rust-client/crates/lma-identity/` (workspace member; `no_std`-friendly core,
`alloc`-only):
- `mint` / `load_identity` / `private_key` — RNS Identity via `rns_crypto`.
- `delivery_hash` / `delivery_hash_hex` — the OUT `lxmf/delivery` DEST logged
  for server `ALLOWED_CLIENTS`.
- `IdentityStore` trait + `load_or_create` — persistence abstraction mirroring
  C `lma_identity::load_or_create(ns)`; host tests use an in-memory backend,
  the firmware plugs an NVS (`esp-storage`) backend behind the same trait.
- Tests (5): two cross-language DEST goldens, DEST≠identity-hash sanity,
  load_or_create persists, **identity survives reset** (fresh store over the
  same persisted bytes → same DEST, no re-mint), distinct mint.

Bazel-first: `//rust-client:test_lma_identity` (manual sh_binary) +
`//rust-client:identity_sources`.

## cargo-size

Deferred to firmware bring-up (T3/T4): the T2 crate is host-only here; the
firmware NVS-backed identity + its on-device `cargo-size` row need the flash
path the T1 gate holds (see below). The identity math itself (SHA-256 + the
destination hash) is tiny; the §8 row will be measured when the firmware wires
it.

## Hardware honesty

The firmware NVS-backed `IdentityStore` and an on-device "identity survives
reset" power-cycle test are **pending-hardware**: the only attached Cardputer
is the live production node, so the flash path stays held (T1-GATE.md §hardware
honesty). Host-side persistence semantics are fully exercised via the injected
store; NVS wiring lands with radio/link bring-up (T3/T4).
