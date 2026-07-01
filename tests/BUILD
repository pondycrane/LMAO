load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "test_lma_encoder",
    srcs = ["test_lma_encoder.py"],
    deps = [
        "//cardputer_client/proto:lma_encoder",
        "//lma_core",
        "@lmao_pip//protobuf",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_server_handler",
    srcs = ["test_server_handler.py"],
    deps = [
        "//lmao_server:server",
        "@lmao_pip//pytest",
    ],
)
