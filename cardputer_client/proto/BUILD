load("@rules_python//python:defs.bzl", "py_library")

# Hand-coded MicroPython protobuf encoder, exposed as a py_library for
# host-side test targets.  cardputer_client/{main,config}.py are excluded
# from Bazel via .bazelignore, but proto/ remains visible so that this
# target is available to //tests:test_lma_encoder.
py_library(
    name = "lma_encoder",
    srcs = ["lma_encoder.py"],
    visibility = ["//visibility:public"],
)
