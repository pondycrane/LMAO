load("@rules_python//python:defs.bzl", "py_binary", "py_library")

py_library(
    name = "server_lib",
    srcs = [
        "server.py",
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
    name = "server",
    srcs = [
        "server.py",
        "config.py",
    ],
    main = "server.py",
    visibility = ["//visibility:public"],
    deps = [":server_lib"],
)
