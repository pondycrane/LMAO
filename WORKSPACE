# Empty file required by Bazel launcher to detect workspace root.
# All module configuration lives in MODULE.bazel (Bzlmod).

# ── External C/C++ deps for the Sprout native client (issue #133, item 4) ──
# RTReticulum (the RNS wire-compatible core) and Mbed TLS (the crypto its host
# port links against) are pinned by commit so the host Bazel build is hermetic:
#   //smart_irrigation/native-client:all covers the lxmf_send / announce-decode
#   units and the upstream doctest suite with no system-only deps.
load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

http_archive(
    name = "mbedtls",
    build_file = "//smart_irrigation/native-client:mbedtls.BUILD",
    patch_args = ["-p1"],
    patches = ["//smart_irrigation/native-client:mbedtls_min_config.patch"],
    sha256 = "b7cbbfb717f0c38521192d7954a2eb6b7ada5a10a44f72dadbae6655e7300023",
    strip_prefix = "mbedtls-3.6.5",
    urls = ["https://github.com/Mbed-TLS/mbedtls/archive/refs/tags/v3.6.5.tar.gz"],
)

http_archive(
    name = "rtreticulum",
    build_file = "//smart_irrigation/native-client:rtreticulum.BUILD",
    patch_args = ["-p1"],
    # rtreticulum_fixes.patch (issue #133 + #135):
    #  - Identity::has_key() + Destination ctors accept a keyless identity for
    #    PLAIN destinations (rnstransport.path.request, work item 2).
    #  - Packet::context_flag() + Transport::process_announce reads the optional
    #    32-byte ratchet an announce carries when the context flag is set — the
    #    live radio-path announce-decode fix (work item 1; validated against a
    #    real server announce captured from the rig).
    #  - RNS on-demand path discovery (issue #135): Transport answers a DATA
    #    path request to the PLAIN rnstransport.path.request destination for
    #    its own IN/SINGLE destinations by announcing them with the
    #    PATH_RESPONSE context byte; inbound PATH_RESPONSE announces are
    #    recognised in process_announce.
    patches = ["//smart_irrigation/native-client:rtreticulum_fixes.patch"],
    sha256 = "5a7e8af49b58bea8c36ae386f64ca1944d14b30cf18ab3d7c8b39ece19f92b92",
    strip_prefix = "RTReticulum-dab4362cf3577e464e98e85b71abc5cb26185224",
    urls = ["https://github.com/0xSeren/RTReticulum/archive/dab4362cf3577e464e98e85b71abc5cb26185224.tar.gz"],
)
