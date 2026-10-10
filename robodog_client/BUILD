# RoboDog — Waveshare ESP32-S3 RLCD-4.2 family-calendar display client
# (MicroPython: urns RNS-TCP to the LMAO server + ST7305 300x400 render).
#
# main.py/config.py use MicroPython-only modules (network, machine) behind lazy
# try/except imports, so this py_library builds host-side; the host test only
# imports ui.py (which is hardware-free).  Flashing stages these sources plus
# the shared cardputer_client/{lib,proto} stack (see tools/install_all.py
# --robodog), so the on-device `from proto import lma_encoder` path resolves.

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
