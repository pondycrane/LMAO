load("@rules_python//python:defs.bzl", "py_library")

# Host-side wrapper around the hand-coded MicroPython encoder.
# The encoder itself lives in cardputer_client/ (excluded from Bazel
# via .bazelignore), but is exposed as a py_library for test deps.
py_library(
    name = "lma_encoder",
    srcs = ["lma_encoder.py"],
    visibility = ["//visibility:public"],
)
