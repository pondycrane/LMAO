load("@rules_python//python:defs.bzl", "py_library")

# Hand-coded MicroPython protobuf encoder, exposed as a py_library for
# host-side test targets.  The raw source file is also exported so that
# the Cardputer flash tool and e2e tests can reference it as a data
# dependency.

py_library(
    name = "lma_encoder",
    srcs = ["lma_encoder.py"],
    visibility = ["//visibility:public"],
)

exports_files(["lma_encoder.py"])
