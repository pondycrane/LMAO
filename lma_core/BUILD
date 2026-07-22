load("@rules_python//python:defs.bzl", "py_library")

py_library(
    name = "lma_core",
    srcs = [
        "__init__.py",
        "config_utils.py",
        "device_detect.py",
        "grpc_types.py",
        "message_utils.py",
        "queue.py",
        "rns_di.py",
        "rns_init.py",
        "storage.py",
    ],
    visibility = ["//visibility:public"],
    deps = ["//proto:lma_messages_py_proto"],
)


