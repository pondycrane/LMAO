load("@rules_python//python:defs.bzl", "py_library")

py_library(
    name = "lma_core",
    srcs = [
        "__init__.py",
        "config_utils.py",
        "message_utils.py",
        "queue.py",
        "storage.py",
        "rns_di.py",
    ],
    visibility = ["//visibility:public"],
    deps = ["//proto:lma_py_proto"],
)
