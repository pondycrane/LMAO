"""Bazel repo rules that fetch + assemble the pinned RNS/LXMF protocol crates.

This replaces the committed `vendor/` mirror (removed in T0 review): the
crates.io tarballs are pinned by SHA-256 and fetched at Bazel analyze time,
matching the repo's existing `http_archive` convention for external third-party
source (see smart_irrigation's mbedtls / rtreticulum pins in WORKSPACE). No
upstream source is committed to LMAO; the pin record (versions + checksums +
role) lives in `rust-client/UPSTREAM.md` and in `rust-client/Cargo.lock`.

Caveat (same as the removed mirror): these fetched sources are *not* what
`cargo` compiles — the host build is out-of-band `cargo build`/`run` and
resolves the same pinned versions from the registry via Cargo.lock. The
http_archive pins are the repo's own, independently-verifiable provenance
(audit trail in UPSTREAM.md), and they put the protocol core in the Bazel
graph for review/diff against the registry.
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

# name | crates.io crate | version | sha256 (validated against static.crates.io 2026-09-26)
_RNS_CRATES = [
    ("rns_core", "rns-core", "0.1.17", "4da568d23a40b8a59a25ffa080e9fdef5d381143eb8b219dd1beb0e57bf20848"),
    ("rns_crypto", "rns-crypto", "0.1.10", "9b7e1dea60e8fb459df3d7f0a018c62f868480d4159ba48d28d6f4c37a0a1200"),
    ("rns_net", "rns-net", "0.7.2", "3593611d6d7472694306170ba5e8a552467a57140688c6ff75b9f62c8817a270"),
    ("lxmf_core", "lxmf-core", "0.1.5", "e15e5558bf39a437777d114458cf961590a2479d6a44398549374e9ad6ca3d42"),
    ("lxmf", "lxmf", "0.11.0", "bfb5dd3d40e5b8cd9c0b02f7aec9e780cfcdd788bdfa8d04b60003bd4c1f74ca"),
]

def lmao_rust_client_repositories():
    """Fetch the five pinned protocol crates from crates.io (sha256-checked)."""
    for name, crate, version, sha256 in _RNS_CRATES:
        http_archive(
            name = name,
            build_file = "//rust-client:crate.BUILD",
            sha256 = sha256,
            strip_prefix = "%s-%s" % (crate, version),
            type = "tar.gz",  # crates.io tarballs ship as gzip under a .crate extension
            urls = ["https://static.crates.io/crates/%s/%s-%s.crate" % (crate, crate, version)],
        )
