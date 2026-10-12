# RoboDog — Waveshare ESP32-S3 RLCD-4.2 family-calendar display client.
#
# ONE runtime, two layers — both pure MicroPython source staged via raw REPL
# (see tools/install_all.py --robodog / --native-robodog):
#   comms   urns µReticulum (cardputer lib symlink) — RNS-TCP + LXMF calendar
#           round-trip (ratchet-encrypted replies decrypt in Python).
#   UI      LVGL 9 via lvgl-micropython (image flashed button-free with HOST
#           esptool; install_all --native-robodog).  lv_display.py bridges
#           LVGL's I1 flush -> st7305.py; lvui.py builds the calendar screen.
# ui.py (own 5x7 bitfont over the bare framebuffer) stays as the fallback
# renderer for plain MicroPython images.

load("@rules_python//python:defs.bzl", "py_library")

py_library(
    name = "robodog_client",
    srcs = [
        "__init__.py",
        "boot.py",
        "config.py",
        "lv_display.py",
        "lvui.py",
        "main.py",
        "st7305.py",
        "ui.py",
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//cardputer_client/proto:lma_encoder",
    ],
)
