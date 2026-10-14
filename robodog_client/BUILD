# RoboDog — Waveshare ESP32-S3 RLCD-4.2 LVGL UI framework (PR 1).
#
# PR 1 scope: the LVGL 9 declarative firmware for the family-calendar panel —
# st7305 display driver, calendar XML screen + subjects, and the pure-C++
# event-selection model — validated on-device with a demo dataset. The stable
# robodog_native client remains the board's working build; the MicroPython +
# urns RNS-TCP data path lands in the follow-up PR.

# ── Host-testable: calendar event-selection/formatting model (pure C++) ──
cc_library(
    name = "calendar_model",
    srcs = ["firmware/main/calendar_model.cpp"],
    hdrs = [
        "firmware/main/calendar_model.h",
        "firmware/main/config.h",
    ],
    strip_include_prefix = "firmware/main",
    visibility = ["//visibility:public"],
)

# ── Device/ESP-IDF firmware sources (built out-of-band by build.sh) ──
filegroup(
    name = "firmware_sources",
    srcs = glob([
        "firmware/main/*.cpp",
        "firmware/main/*.h",
        "firmware/main/*.yml",
        "firmware/main/ui_xml/*.xml",
        "firmware/*.sh",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]),
    visibility = ["//visibility:public"],
)

# ── Firmware build / flash (manual — Docker + ESP-IDF, device-attached) ──
#   bazel run //robodog_client:build_firmware
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM1]
# Both scripts run esptool on the HOST (button-free reset can't run through
# Docker's ttyACM passthrough — RTS/DTR is dropped there).
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
