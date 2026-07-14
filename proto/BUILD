load("@rules_proto//proto:defs.bzl", "proto_library")
load("@protobuf//bazel:py_proto_library.bzl", "py_proto_library")

# ── Core mesh messages (required by everything) ──────────────────

proto_library(
    name = "lma_messages_proto",
    srcs = ["lma_messages.proto"],
    visibility = ["//visibility:public"],
)

py_proto_library(
    name = "lma_messages_py_proto",
    deps = [":lma_messages_proto"],
    visibility = ["//visibility:public"],
)

# ── gRPC service types (only lmao_server + k8s-app) ──────────────

proto_library(
    name = "lma_grpc_proto",
    srcs = ["lma_grpc.proto"],
    visibility = ["//visibility:public"],
)

py_proto_library(
    name = "lma_grpc_py_proto",
    deps = [":lma_grpc_proto"],
    visibility = ["//visibility:public"],
)

# ── Placeholder for future multi-language stubs ─────────────────────

# go_proto_library(name = "lma_messages_go_proto", ...)
# kt_jvm_proto_library(name = "lma_messages_kt_proto", ...)
# nanopb_proto_library(name = "lma_messages_nanopb", ...)