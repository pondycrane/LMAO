# RoboDog home client — host-side client (Reticulum TCP) + ESP32-S3 firmware.
#
#   bazel run //robodog_home_client:home_client      # read the env series
#   bazel run //robodog_home_client:build_firmware   # build the board firmware
#   bazel run //robodog_home_client:flash_firmware -- --device-name "main bedroom" ...
#
# Firmware build/flash are manual (Docker ESP-IDF / host esptool, device
# attached) and never pulled into `bazel test`.

load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("//robodog_client:firmware_tools.bzl", "firmware_tool")

# ── Host-side client: air temperature/humidity over the Reticulum TCP ──
py_library(
    name = "home_client_lib",
    srcs = [
        "client.py",
        "config.py",
    ],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
        "@lmao_pip//protobuf",
        "@lmao_pip//rns",
        "@lmao_pip//lxmf",
    ],
)

py_binary(
    name = "home_client",
    srcs = [
        "client.py",
        "config.py",
    ],
    main = "client.py",
    visibility = ["//visibility:public"],
    deps = [":home_client_lib"],
)

# ── Host-testable firmware units ──────────────────────────────────────
# Chart geometry (pure C11, no ESP-IDF): the same source the firmware draws.
cc_library(
    name = "rd_chart",
    srcs = ["firmware/main/rd_chart.c"],
    hdrs = ["firmware/main/rd_chart.h"],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "rd_chart_test",
    srcs = ["firmware/main/rd_chart_selftest.c"],
    deps = [":rd_chart"],
    tags = ["unit"],
)

# Air-series wire codec (HistoryRequest encoder + ChartBundle decoder) — the
# exact source the firmware compiles, checked against golden protobuf bytes.
cc_test(
    name = "lma_chart_test",
    srcs = ["tests/lma_chart_test.cpp"],
    deps = ["//firmware_common:lma_chart"],
    tags = ["unit"],
)

# ── Firmware build / flash (manual — Docker + ESP-IDF, device-attached) ──
firmware_tool(
    name = "build_firmware",
    script = "firmware/build.sh",
    data = glob([
        "firmware/main/*",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]) + [
        "//firmware_common:shared_firmware_sources",
        # u8g2 upstream csrc is fetched (never vendored); carried in runfiles
        # so `bazel run` needs no network.
        "@u8g2//:src",
        "//robodog_client:u8g2_prepare.sh",
        "//robodog_client:u8g2_components",
    ],
)

firmware_tool(
    name = "flash_firmware",
    script = "firmware/flash.sh",
    data = glob(["firmware/main/*"]),
)
