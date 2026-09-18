# Sprout native client — Bazel targets (issue #131).
#
# Everything in this package is either a host-testable C++ unit (fully under
# Bazel) or device/ESP-IDF sources that build via the Docker ESP-IDF toolchain
# (the RTReticulum clone in ../.rtreticulum).  A host-testable layer lives in
# Bazel as cc_library/cc_test; the ESP-IDF build+flash are wrapped as manual
# sh_binary targets (:build_firmware / :flash_firmware) and the sources are
# represented as filegroups so the whole tree is in the Bazel graph.

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

# Soil-moisture calibration math (pure inline function in moisture.h; the
# ESP-IDF ADC wrapper itself builds out-of-band with the device firmware).
cc_library(
    name = "moisture_math",
    hdrs = ["firmware/main/moisture.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "moisture_calib_test",
    srcs = ["tests/moisture_calib_test.cpp"],
    deps = [":moisture_math"],
    tags = ["unit"],
)

# ── Host-testable RTReticulum-dependent units (issue #133, work item 4) ──
# RTReticulum + Mbed TLS are vendored in WORKSPACE (pinned http_archive), so
# lxmf_send is now a real cc_library and the radio-path announce decode gets a
# cc_test — the same code the device firmware compiles, exercised on the host.

cc_library(
    name = "lxmf_send",
    srcs = ["firmware/main/lxmf_send.cpp"],
    hdrs = ["firmware/main/lxmf_send.h"],
    strip_include_prefix = "firmware/main",
    deps = ["@rtreticulum//:rtreticulum"],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "announce_decode_test",
    srcs = ["tests/announce_decode_test.cpp"],
    deps = ["@rtreticulum//:rtreticulum"],
    tags = ["unit"],
)

cc_test(
    name = "real_announce_test",
    srcs = ["tests/real_announce_test.cpp"],
    deps = ["@rtreticulum//:rtreticulum"],
    tags = ["unit"],
)

cc_test(
    name = "path_request_test",
    srcs = ["tests/path_request_test.cpp"],
    deps = ["@rtreticulum//:rtreticulum"],
    tags = ["unit"],
)

# Host RNS-interop harness (issue #133, work item 4): connects RTReticulum to a
# Python RNS TCPServerInterface peer (see host/README.md). Manual — needs a
# live Python peer on the network; buildable so the whole package is in Bazel.
cc_binary(
    name = "interop",
    srcs = [
        "host/tcp_interface.h",
        "host/interop.cpp",
        "host/tcp_interface.cpp",
    ],
    includes = ["host"],
    copts = ["-std=c++17"],
    deps = ["@rtreticulum//:rtreticulum"],
    tags = ["manual"],
)

# ── Device/ESP-IDF firmware sources (built out-of-band by firmware/build.sh) ──
# ESP-IDF-only code (uart_at, sht30, main, path_find) cannot compile on the
# host under Bazel (they pull esp_log/freertos/esp_timer); they are represented
# as filegroups so the whole tree stays in the Bazel graph.
filegroup(
    name = "firmware_sources",
    srcs = glob([
        "firmware/**/*.cpp",
        "firmware/**/*.h",
        "firmware/**/*.sh",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
        "firmware/README.md",
    ]),
    visibility = ["//visibility:public"],
)

# ── Firmware build / flash (manual — Docker + ESP-IDF, device-attached) ──
# The native firmware cannot build under pure Bazel (ESP-IDF headers/FreeRTOS);
# these wrap the documented Docker build (build.sh) and flash (README) so the
# whole loop is driven from Bazel:
#   bazel run //smart_irrigation/native-client:build_firmware
#   bazel run //smart_irrigation/native-client:flash_firmware [-- --port X]
# Both are manual: they require Docker (and the device for flash) and are never
# pulled into `bazel test` runs.

sh_binary(
    name = "build_firmware",
    srcs = ["firmware/build.sh"],
    data = [":firmware_sources"],
    tags = ["manual"],
)

sh_binary(
    name = "flash_firmware",
    srcs = ["firmware/flash.sh"],
    data = [":firmware_sources"],
    tags = ["manual"],
)

# Host RNS-interop harness (RTReticulum-dependent; built out-of-band — see
# native-client/host/README.md).
filegroup(
    name = "host_interop_sources",
    srcs = glob(["host/**"]),
    visibility = ["//visibility:public"],
)
