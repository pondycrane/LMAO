load("@rules_python//python:defs.bzl", "py_binary", "py_library")

# Export MicroPython source files so the e2e test can reference them as
# data dependencies.  These files are NOT compiled by host Python — they
# are only copied to the Cardputer device at flash time.
exports_files([
    "boot.py",
    "main.py",
    "chart.py",
    "config.py",
    "lora_boards.py",
])

# ── µReticulum library files (urns port + native modules) ──────────
# Uploaded to /lib/ on the Cardputer.  Not compiled — only transferred.
filegroup(
    name = "lib_files",
    srcs = glob([
        "lib/**/*.py",
        "lib/**/*.mpy",
    ]),
    visibility = ["//visibility:public"],
)

# ── All client files (convenience alias) ──────────────────────────

filegroup(
    name = "client_files",
    srcs = [
        "boot.py",
        "chart.py",
        "config.py",
        "lora_boards.py",
        "main.py",
        "//cardputer_client/proto:lma_encoder.py",
        ":lib_files",
    ],
    visibility = ["//visibility:public"],
)

# ── Chart renderer (reusable by unit tests) ────────────────────────
# Pure MicroPython: the DATA-line parser and the geometry are exercised on the
# host with a fake display (see //tests:test_chart).
py_library(
    name = "chart_lib",
    srcs = ["chart.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
)

# ── Client library (reusable by unit tests) ────────────────────────
# Exposes ``cardputer_client.main`` for host-side unit testing of
# pure-logic helpers (``_needs_wifi``, ``handle_reply``, ``log``).
# MicroPython-only imports are wrapped in try/except at module level
# so the module loads cleanly on CPython.

py_library(
    name = "main_lib",
    srcs = ["main.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
)

# ── Flash library (reusable by e2e tests) ──────────────────────────

py_library(
    name = "flash_lib",
    srcs = ["flash.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [
        "@lmao_pip//pyserial",
    ],
)

# ── Flash binary (``bazel run //cardputer_client:flash``) ──────────
# Uploads the LMAO client + µReticulum library to the Cardputer.

py_binary(
    name = "flash",
    srcs = ["flash.py"],
    main = "flash.py",
    data = [":client_files"],
    visibility = ["//visibility:public"],
    deps = [":flash_lib"],
)

# ═══════════════════════════════════════════════════════════════════
# Native C firmware (PR1 — sensor node).  See cardputer_client/firmware/.
# ESP-IDF-only sources build via Docker (firmware/build.sh → idf.py);
# the host-testable core (lma_encoder, dht20_math, lxmf_send) is under Bazel
# so the same code the device compiles is exercised on the host.
# ═══════════════════════════════════════════════════════════════════

# ── Host-testable core: the wire-compatible SensorReport encoder ──
# Pure C++ (std::string/vector only) — no ESP-IDF, no RTReticulum deps.
# Lives in firmware_common/ (shared with the Sprout client — DRY).
cc_library(
    name = "cardputer_encoder",
    deps = ["//firmware_common:lma_encoder"],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "cardputer_encoder_test",
    srcs = ["firmware/tests/lma_encoder_test.cpp"],
    deps = [":cardputer_encoder"],
    # Golden bytes verified from cardputer_client/proto/lma_encoder.py
    # (Python) so the C++ port must produce byte-identical output.
    tags = ["unit"],
)

# ── DHT20 raw→unit math (pure inline header; the I2C driver builds
#    out-of-band with the device firmware) ──
cc_library(
    name = "dht20_math",
    hdrs = ["firmware/main/dht20_math.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "dht20_math_test",
    srcs = ["firmware/tests/dht20_math_test.cpp"],
    deps = [":dht20_math"],
    tags = ["unit"],
)

# ── LXMF send (RTReticulum-dependent) — host-testable like Sprout's.
# Lives in firmware_common/ (shared with the Sprout client — DRY).
cc_library(
    name = "lxmf_send",
    deps = ["//firmware_common:lxmf_send"],
    visibility = ["//visibility:public"],
)

# ── PR2 receive: inbound LXM parse + signature verify + envelope decode ──
# Shared with the Sprout client (firmware_common — DRY), host-testable.
cc_library(
    name = "lxm_recv",
    deps = ["//firmware_common:lxm_recv"],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lxm_recv_test",
    srcs = ["firmware/tests/lxm_recv_test.cpp"],
    deps = [":lxm_recv"],
    tags = ["unit"],
)

cc_library(
    name = "lma_decode",
    deps = ["//firmware_common:lma_decode"],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lma_decode_test",
    srcs = ["firmware/tests/lma_decode_test.cpp"],
    deps = [":lma_decode"],
    tags = ["unit"],
)

# ── PR2 chart: DATA-line parser + geometry + renderer (pure C++, no ESP-IDF).
# Host-testable via a fake ChartDisplay; the same code the device compiles. ──
cc_library(
    name = "chart",
    srcs = ["firmware/main/chart.cpp"],
    hdrs = ["firmware/main/chart.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "chart_test",
    srcs = ["firmware/tests/chart_test.cpp"],
    deps = [":chart"],
    tags = ["unit"],
)

# ── Device/ESP-IDF firmware sources (built out-of-band by build.sh) ──
filegroup(
    name = "firmware_sources",
    srcs = glob([
        "firmware/**/*.cpp",
        "firmware/**/*.h",
        "firmware/**/*.sh",
        "firmware/**/*.yml",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]) + ["//firmware_common:shared_firmware_sources"],
    visibility = ["//visibility:public"],
)

# ── Firmware build / flash (manual — Docker + ESP-IDF, device-attached) ──
#   bazel run //cardputer_client:build_firmware
#   bazel run //cardputer_client:flash_firmware [-- --port /dev/ttyACM0]
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
