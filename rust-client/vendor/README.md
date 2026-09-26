# vendor/ — protocol-core source-of-record mirror

These directories are **checked-in mirrors of the exact pins in `../Cargo.lock`
and `../UPSTREAM.md`** (the Rust equivalent of the C side's `.rtreticulum`
vendoring). They are *not* build inputs: `cargo` resolves versions from
`Cargo.lock` against the registry cache. They exist so the borrowed protocol
core is reviewable and diffable in-repo, matching the repo's existing vendoring
convention.

| Directory | Crate / version | Source |
|---|---|---|
| `rns-core/` | rns-core 0.1.17 | [lelloman/rns-rs](https://github.com/lelloman/rns-rs) |
| `rns-crypto/` | rns-crypto 0.1.10 | lelloman/rns-rs |
| `rns-net/` | rns-net 0.7.2 | lelloman/rns-rs |
| `lxmf-core/` | lxmf-core 0.1.5 | [lelloman/lxmf-rs](https://github.com/lelloman/lxmf-rs) |
| `lxmf/` | lxmf 0.11.0 | lelloman/lxmf-rs |

Refresh procedure: `cargo update -p <crate>` → re-`cargo vendor` → copy the new
crate dir in place → update the SHA-256 row in `UPSTREAM.md`. See `UPSTREAM.md`.
