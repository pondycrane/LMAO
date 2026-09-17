# Sprout native client — Bazel targets (issue #131).
#
# Everything in this package is either a host-testable C++ unit (fully under
# Bazel) or device/ESP-IDF sources that build out-of-band (via firmware/build.sh
# + the RTReticulum clone in ../.rtreticulum) and are represented here as
# filegroups so the whole tree is in the Bazel graph.

# ── Host-testable core: the wire-compatible SensorReport/ LMAOEnvelope encoder ──
# Pure C++ (std::string/vector only) — no ESP-IDF, no RTReticulum deps.
cc_library(
    name = "sprout_encoder",
    srcs = ["firmware/main/lma_encoder.cpp"],
    hdrs = ["firmware/main/lma_encoder.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "sprout_encoder_test",
    srcs = ["tests/lma_encoder_test.cpp"],
    deps = [":sprout_encoder"],
    # Golden bytes generated from cardputer_client/proto/lma_encoder.py
    # (Python) so the C++ port must produce byte-identical LMAOEnvelope output.
    tags = ["unit"],
)

# ── Device/ESP-IDF firmware sources (built out-of-band by firmware/build.sh) ──
# RTReticulum-dependent (lxmf_send, path_find) and ESP-IDF-only (uart_at,
# sht30, main) code cannot compile on the host under Bazel yet; once
# RTReticulum is vendored/http_archive'd these get real cc_library targets
# (tracked in the native-client next-step issue).
filegroup(
    name = "firmware_sources",
    srcs = glob([
        "firmware/**/*.cpp",
        "firmware/**/*.h",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
        "firmware/build.sh",
        "firmware/README.md",
    ]),
    visibility = ["//visibility:public"],
)

# Host RNS-interop harness (RTReticulum-dependent; built out-of-band — see
# native-client/host/README.md).
filegroup(
    name = "host_interop_sources",
    srcs = glob(["host/**"]),
    visibility = ["//visibility:public"],
)
