# RoboDog — Waveshare ESP32-S3 RLCD-4.2 family-calendar display client.
#
# Two runtimes (mirroring the Cardputer split):
#   DEFAULT  MicroPython (`robodog_client/*.py`): urns RNS-TCP + ST7305 render.
#   OPT-IN   native ESP-IDF + LVGL (`robodog_client/firmware/`): declarative
#            XML+subjects UI; built via Docker ESP-IDF by build.sh (DEST_HASH /
#            identity / WiFi baked in at build time via LMAO_* env vars).
#
# Host-testable C++ units live under Bazel (the same code the device firmware
# compiles); the ESP-IDF-only sources are consumed by the firmware build.

load("@rules_python//python:defs.bzl", "py_library")

py_library(
    name = "robodog_client",
    srcs = [
        "__init__.py",
        "boot.py",
        "config.py",
        "main.py",
        "ui.py",
        "st7305.py",
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//cardputer_client/proto:lma_encoder",
    ],
)

# ── Host-testable: calendar event-selection/formatting model (pure C++) ──
cc_library(
    name = "calendar_model",
    srcs = ["firmware/main/calendar_model.cpp"],
    hdrs = ["firmware/main/calendar_model.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
    deps = ["//firmware_common:lma_calendar"],
)

# ── Host-testable: RNS-TCP interface + calendar request/reply glue ──
# Portable POSIX-socket C++ (compiles on the host AND under ESP-IDF/lwIP) —
# the same code the device firmware compiles, exercised on the host by the
# interop binary below.
cc_library(
    name = "robodog_net",
    srcs = [
        "firmware/main/calendar_net.cpp",
        "firmware/main/tcp_interface.cpp",
    ],
    hdrs = [
        "firmware/main/calendar_net.h",
        "firmware/main/tcp_interface.h",
    ],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
    deps = [
        "//firmware_common:lma_calendar",
        "//firmware_common:lxmf_send",
        "@rtreticulum//:rtreticulum",
    ],
)

# Live interop smoke (`//robodog_client/firmware/tests:calendar_fetch`) fetches
# the family calendar from a real LMAO server over RNS-TCP — manual, needs a
# reachable server (production :4246 or a local WiFi-only server).

# ── Device/ESP-IDF firmware sources (built out-of-band by build.sh) ──
filegroup(
    name = "firmware_sources",
    srcs = glob([
        "firmware/**/*.cpp",
        "firmware/**/*.h",
        "firmware/**/*.sh",
        "firmware/**/*.yml",
        "firmware/**/*.xml",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]) + ["//firmware_common:shared_firmware_sources"],
    visibility = ["//visibility:public"],
)

# ── Firmware build / flash (manual — Docker + ESP-IDF, device-attached) ──
#   bazel run //robodog_client:build_firmware
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM1]
# DEST_HASH / identity / WiFi are baked at build time via LMAO_* env vars;
# bazel run //tools:install_all -- --robodog-port ... does the full flow.
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
