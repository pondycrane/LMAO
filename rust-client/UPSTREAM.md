# UPSTREAM — pinned protocol sources & interop baseline

This is the equivalent of the C side's `.rtreticulum` vendoring for the Rust
client: exact, recorded pins of the protocol core so wire-compatible builds are
reproducible. The **authoritative version selection** lives in `Cargo.lock`
(committed); this file records *why/where* each protocol crate comes from, and
the **checked-in copies in `vendor/` are the source-of-record mirror** of those
pinned versions.

## Direct protocol crate pins (T0)

These five crates are the borrowed protocol core (design §3 primary = lelloman
`rns-rs` / `lxmf-rs`). Source-of-record copies are checked in under `vendor/`.
`Cargo.lock` pins every transitive dependency at the same resolutions.

| Crate | Version | Role | SHA-256 (`.crate` tarball) | Vendor path |
|---|---|---|---|---|
| `rns-core` | 0.1.17 | wire protocol, transport, **Link / Resource**, holepunch (`no_std`, zero-dep) | `4da568d23a40b8a59a25ffa080e9fdef5d381143eb8b219dd1beb0e57bf20848` | `vendor/rns-core/` |
| `rns-crypto` | 0.1.10 | X25519/Ed25519/AES/SHA/HMAC/HKDF/Identity | `9b7e1dea60e8fb459df3d7f0a018c62f868480d4159ba48d28d6f4c37a0a1200` | `vendor/rns-crypto/` |
| `rns-net` | 0.7.2 | reference host interfaces (RNode/KISS/TCP/serial); porting + T0 host node | `3593611d6d7472694306170ba5e8a552467a57140688c6ff75b9f62c8817a270` | `vendor/rns-net/` |
| `lxmf-core` | 0.1.5 | LXMF control message pack/unpack + stamping (`no_std`) — T5 control path | `e15e5558bf39a437777d114458cf961590a2479d6a44398549374e9ad6ca3d42` | `vendor/lxmf-core/` |
| `lxmf` | 0.11.0 | full `std` LXMF router — **host T0/T5 harness only**, never on the leaf | `bfb5dd3d40e5b8cd9c0b02f7aec9e780cfcdd788bdfa8d04b60003bd4c1f74ca` | `vendor/lxmf/` |

Upstream: `https://github.com/lelloman/rns-rs` (and its `lelloman/lxmf-rs`
sibling). Versions above match the published crates.io releases; re-vendor via:

```bash
# update a pin, then refresh the vendor mirror + checksums:
cargo update -p <crate>
cargo vendor /tmp/vendor-tmp   # confirm the five crates' versions unchanged/desired
rm -rf vendor/<crate> && cp -R /tmp/vendor-tmp/<crate> vendor/<crate>
```

## Interop wire baseline (T0 DECISION GATE)

T0's Link + Resource interop gate is validated against **Python RNS 1.3.5** —
the *exact* `RNS` version the production `lmao-server` runs (verified in the
K8s `lmao-server` pod). The gate harness spawns a Python RNS peer from
`$LMAO_PYTHON` and connects the Rust `rns-net` node over a shared TCP mesh.

Protocol scope validated by the gate (design §2): announce → path-request →
normal packet both ways → **Link** (serve inbound, hold state) → link data both
ways → **Resource RX** (Python→Rust, 100000 B, chunk/hash-map reassembly +
content verify) → **Resource TX** (Rust→Python, digest verified on the Python
side + Rust receives the completion proof).

## Toolchain

- Host (T0): `rustup` `stable` (recorded in `rust-toolchain.toml`).
- Firmware (T1+): espup **nightly** + `xtensa-esp32s3-none-elf`; pinned in
  `rust-toolchain.toml` when the decision gate passes.

## How to run the gate

```bash
python3 -m venv /tmp/t0-venv && /tmp/t0-venv/bin/pip install "rns==1.3.5"
cd rust-client
LMAO_PYTHON=/tmp/t0-venv/bin/python cargo run -p lmao-t0-interop   # exit 0 = PASS
# or via Bazel (manual tag — needs a reachable Python RNS):
bazel run //rust-client:run_t0_gate
```
