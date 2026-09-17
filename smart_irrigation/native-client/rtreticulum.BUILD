# Bazel build file for the pinned @rtreticulum http_archive
# (RTReticulum dab4362, the RNS wire-compatible C++ core used by the Sprout
# native client). Host (posix) HAL only — mirrors CMakeLists.txt
# RTRETICULUM_PORT=posix. Issue #133, work item 4.

package(default_visibility = ["//visibility:public"])

# Monocypher is vendored inside the rtreticulum tree.
cc_library(
    name = "monocypher",
    srcs = [
        "third_party/monocypher/monocypher.c",
        "third_party/monocypher/monocypher-ed25519.c",
    ],
    hdrs = [
        "third_party/monocypher/monocypher.h",
        "third_party/monocypher/monocypher-ed25519.h",
    ],
    includes = ["third_party/monocypher"],
)

# Posix HAL (mutex/millis-time/firmware primitives) — RTReticulum's own
# thread primitives map to pthread here.
cc_library(
    name = "rtreticulum_hal",
    srcs = ["port/posix/hal_posix.c"],
    hdrs = glob(["port/**", "include/rtreticulum/**"]),
    includes = ["include"],
    linkopts = ["-pthread"],
)

cc_library(
    name = "rtreticulum",
    srcs = [
        "src/log.cpp",
        "src/os.cpp",
        "src/crc.cpp",
        "src/memory.cpp",
        "src/bytes.cpp",
        "src/cryptography/hashes.cpp",
        "src/cryptography/hmac.cpp",
        "src/cryptography/hkdf.cpp",
        "src/cryptography/aes.cpp",
        "src/cryptography/ed25519.cpp",
        "src/cryptography/x25519.cpp",
        "src/cryptography/fernet.cpp",
        "src/cryptography/token.cpp",
        "src/identity.cpp",
        "src/destination.cpp",
        "src/packet.cpp",
        "src/interface.cpp",
        "src/transport.cpp",
        "src/link.cpp",
        "src/resource.cpp",
        "src/msgpack.cpp",
        "src/reticulum.cpp",
        "src/filesystem.cpp",
        "src/filesystems/posix.cpp",
        "src/interfaces/loopback.cpp",
        "third_party/tlsf/tlsf.c",
    ],
    hdrs = glob([
        "include/rtreticulum/**/*.h",
        "third_party/tlsf/*.h",
        "third_party/monocypher/*.h",
    ]),
    includes = [
        "include",
        "third_party/tlsf",
        "third_party/monocypher",
    ],
    copts = ["-std=c++17", "-fexceptions", "-Wno-unused-parameter"],
    deps = [
        ":monocypher",
        ":rtreticulum_hal",
        "@mbedtls//:crypto",
    ],
    linkopts = ["-pthread"],
)

# Host unit tests from the upstream tree (doctest), kept for parity with the
# "61 cases / 1208 assertions" baseline referenced in host/README.md. Run
# with: bazel test //smart_irrigation/native-client:rtreticulum_upstream_tests
cc_library(
    name = "doctest",
    hdrs = ["third_party/doctest/doctest.h"],
    includes = ["third_party/doctest"],
)

cc_test(
    name = "rtreticulum_upstream_tests",
    srcs = [
        "test/test_main.cpp",
        "test/test_crc.cpp",
        "test/test_bytes.cpp",
        "test/test_pkcs7.cpp",
        "test/test_tlsf.cpp",
        "test/test_crypto.cpp",
        "test/test_identity.cpp",
        "test/test_loopback.cpp",
        "test/test_transport.cpp",
        "test/test_link.cpp",
        "test/test_filesystem.cpp",
        "test/test_reticulum_task.cpp",
        "test/test_transport_concurrency.cpp",
        "test/test_resource.cpp",
    ],
    deps = [":rtreticulum", ":doctest"],
)
