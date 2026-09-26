# Shared build_file for the Bazel-fetched, pinned RNS/LXMF protocol crates.
#
# Each http_archive (rust-client/repositories.bzl) extracts one crates.io
# tarball under <repo>-<version>/ (strip_prefix) and this file exposes the
# whole extracted tree — src/, tests/, Cargo.* — as `:sources`, so the
# rust-client BUILD filegroup can show the pinned protocol core in the Bazel
# graph exactly as the removed committed `vendor/` mirror did, without
# committing any upstream source to LMAO.
package(default_visibility = ["//visibility:public"])

filegroup(
    name = "sources",
    srcs = glob(["**"]),
)
