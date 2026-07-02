load("@rules_python//python:defs.bzl", "py_binary", "py_library")

py_library(
    name = "client_lib",
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
    name = "client",
    srcs = [
        "client.py",
        "config.py",
    ],
    main = "client.py",
    visibility = ["//visibility:public"],
    deps = [":client_lib"],
)
