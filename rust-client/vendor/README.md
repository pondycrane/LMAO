# Vendored crates

## rns-crypto 0.1.10 (no_std patch)

Copied from crates.io; **one change**: `[features] default = ["std"]` → `default = []`.

Why: `rns-core`'s default features enable its `std` feature, which turns on
`rns-crypto/std`. That pulls `std` into what must be a no_std crate for the
xtensa firmware target (esp build-std has no std for this chip). `rns-crypto`
is genuinely no_std behind its optional `std` feature, so the firmware
workspace patches it to `default = []` (see `firmware/Cargo.toml`
`[patch.crates-io]`). The `radio-interface` crate also depends on `rns-core`
with `default-features = false`.
