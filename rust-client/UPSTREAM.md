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

## Interop vector fixtures (git-only — vendored separately)

Each pinned crate's `tests/interop.rs`-style suites consume a shared JSON
vector corpus (`announce_vectors.json`, `packet_vectors.json`,
`resource/*_vectors.json`, `crypto/*_vectors.json`, `conformance_*/*.json`, …)
that **crates.io tarballs do not ship** — `{*.crate}` strips `tests/fixtures/`,
so the corpus is **git-only** upstream. A bare `cargo vendor` of the five crates
therefore cannot run its own wire-compliance suites.

The corpus is checked in at **`vendor/tests/fixtures/`** (fetched from upstream
at the recorded publish commits — the rns-rs set at `70deb22`, the lxmf-rs set
at `0f18526`; both are the same fixture snapshot rns-core 0.1.17 / rns-crypto
0.1.10 were published against, verified passing):

| Crate | Upstream publish commit (`.cargo_vcs_info.json`) |
|---|---|
| rns-core `0.1.17`, rns-crypto `0.1.10` | `c0f9110e6…` (rns-rs) |
| rns-net `0.7.2` | `70deb224b…` (rns-rs) |
| lxmf-core `0.1.5` | `0f1852694…` (lxmf-rs) |
| lxmf `0.11.0` | `346deedac…` (lxmf-rs; note this SHA is not a GitHub API-resolvable commit — fixtures sourced from the lxmf-core commit) |

Run the vector suites (all pass with the vendored corpus):

```bash
cd rust-client
cargo test --manifest-path vendor/rns-core/Cargo.toml    # 655 unit + 12+9+9+26 interop/integration
cargo test --manifest-path vendor/rns-crypto/Cargo.toml  # 73 + 11 + 11
cargo test --manifest-path vendor/lxmf-core/Cargo.toml   # 62 interop
# rns-net fixture suites (self-contained; skip its live-mesh integration tests):
cargo test --manifest-path vendor/rns-net/Cargo.toml \
  --test resource_streaming_vectors --test reticulum_138_fixtures \
  --test reticulum_139_fixtures --test reticulum_140_fixtures
```

`vendor/` is excluded from the workspace root `Cargo.toml` (`exclude = […]`) so
each crate builds/tests standalone with its own `Cargo.lock`. When bumping a
pin, re-fetch the matching fixture snapshot from the crate's recorded commit
(`git archive --remote` / raw file download of `tests/fixtures/`) and re-verify
the suites above.

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
