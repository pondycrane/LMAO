# UPSTREAM — pinned protocol sources & interop baseline

This is the Rust client's record of where the protocol core comes from and how
it is pinned — the equivalent of the C side's `.rtreticulum` vendoring and of
the Sprout client's `http_archive` pins (`mbedtls`, `rtreticulum` in
`WORKSPACE`). The **authoritative version selection** lives in `Cargo.lock`
(committed; `#[crates.io]`-resolved by the out-of-band `cargo` build). The five
protocol crates are **not committed to this repo**: `WORKSPACE` +
`rust-client/repositories.bzl` fetch each crates.io tarball at Bazel analyze
time, pinned by SHA-256 below, and `rust-client/crate.BUILD` exposes each as
`@<name>//:sources` in the Bazel graph. The sha256 rows below were validated
against `static.crates.io` on 2026-09-26.

## Direct protocol crate pins (T0)

| Crate | Version | Role | SHA-256 (`.crate` tarball) | Bazel repo |
|---|---|---|---|---|
| `rns-core` | 0.1.17 | wire protocol, transport, **Link / Resource**, holepunch (`no_std`, zero-dep) | `4da568d23a40b8a59a25ffa080e9fdef5d381143eb8b219dd1beb0e57bf20848` | `@rns_core` |
| `rns-crypto` | 0.1.10 | X25519/Ed25519/AES/SHA/HMAC/HKDF/Identity | `9b7e1dea60e8fb459df3d7f0a018c62f868480d4159ba48d28d6f4c37a0a1200` | `@rns_crypto` |
| `rns-net` | 0.7.2 | reference host interfaces (RNode/KISS/TCP/serial); porting + T0 host node | `3593611d6d7472694306170ba5e8a552467a57140688c6ff75b9f62c8817a270` | `@rns_net` |
| `lxmf-core` | 0.1.5 | LXMF control message pack/unpack + stamping (`no_std`) — T5 control path | `e15e5558bf39a437777d114458cf961590a2479d6a44398549374e9ad6ca3d42` | `@lxmf_core` |
| `lxmf` | 0.11.0 | full `std` LXMF router — **host T0/T5 harness only**, never on the leaf | `bfb5dd3d40e5b8cd9c0b02f7aec9e780cfcdd788bdfa8d04b60003bd4c1f74ca` | `@lxmf` |

Upstream: `https://github.com/lelloman/rns-rs` (and its `lelloman/lxmf-rs`
sibling). Pinning + fetch:

```bash
# bump a crate, record the new checksum, and verify the Bazel fetch is stable:
cargo update -p <crate>
# update the version + sha256 row in repositories.bzl and this table
# (get the new sha256:  sha256sum <downloaded .crate>  or  crates.io API)
bazel build //rust-client:rust_sources   # re-fetches by the new pin
```

## Vector fixtures (git-only — fetch on demand)

The pinned crates' `tests/interop.rs`-style suites consume a shared JSON vector
corpus (`protocol/*.json`, `resource/*.json`, `crypto/*.json`,
`conformance_*/runtime_vectors.json`, …) that **crates.io tarballs do not ship**
— `{*.crate}` strips `tests/fixtures/`, so the corpus is **git-only** upstream
(lelloman/rns-rs at repo-root `tests/fixtures/`; lelloman/lxmf-rs likewise).
Neither the committed tree nor the Bazel-fetched crates carry it, so the
wire-compliance vector suites are **fetch-on-demand**, not part of the build or
the T0 gate (the gate generates its own payloads and does not need them).

The corpus was validated against the pinned crates on 2026-09-26 and passing:
rns-core 0.1.17 (655 unit + 12 interop + 9 link + 9 resource + 26 transport),
rns-crypto 0.1.10 (73 + 11 + 11), lxmf-core 0.1.5 (62), rns-net 0.7.2 fixture
bins. Recorded upstream publish commits (`.cargo_vcs_info.json`): rns-core /
rns-crypto `c0f9110e6…`, rns-net `70deb224b…`, lxmf-core `0f1852694…`,
lxmf `346deedac…` (not a GitHub-API-resolvable commit; use the lxmf-core
commit's fixtures).

```bash
# fetch the corpus for the recorded commit into a scratch dir, then test:
git clone --depth 1 https://github.com/lelloman/rns-rs /tmp/rns-rs   # or the commit above
# copy /tmp/rns-rs/tests/fixtures -> <crate-dir>/../tests/fixtures  (crate dir = Bazel
# external repo or a fresh `cargo registry` extract), then:
cargo test --manifest-path vendor/rns-core/Cargo.toml   # inside a local crate checkout
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
- Firmware (T1): `espup install --targets esp32s3` → the Xtensa esp nightly
  fork exposed as the rustup toolchain **`esp`** (Xtensa Rust 1.97.0.0;
  xtensa-esp-elf 15.2.0 + esp-clang). Pinned in
  `rust-client/firmware/rust-toolchain.toml` (`channel = "esp"`). The host and
  firmware are separate Cargo workspaces so each carries its own toolchain.
  esp-hal 1.2.2 is cross-compiled with `build-std = ["core","alloc"]` and the
  chip linker script (`-Wl,-Tlinkall.x -nostartfiles`; scripts exposed by
  esp-hal's build.rs via `OUT_DIR` link-search). `cargo`/`espflash` env from
  `source ~/export-esp.sh`. Build + size snapshot recorded in `T1-GATE.md`.

## How to run the gate

```bash
python3 -m venv /tmp/t0-venv && /tmp/t0-venv/bin/pip install "rns==1.3.5"
cd rust-client
LMAO_PYTHON=/tmp/t0-venv/bin/python cargo run -p lmao-t0-interop   # exit 0 = PASS
# or via Bazel (manual tag — needs a reachable Python RNS):
bazel run //rust-client:run_t0_gate
```
