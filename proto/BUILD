load("@rules_proto//proto:defs.bzl", "proto_library")
load("@protobuf//bazel:py_proto_library.bzl", "py_proto_library")

# ── Raw proto library (language-agnostic) ───────────────────────────

proto_library(
    name = "lma_proto",
    srcs = ["lma.proto"],
    visibility = ["//visibility:public"],
)

# ── Python generated stubs ──────────────────────────────────────────

py_proto_library(
    name = "lma_py_proto",
    deps = [":lma_proto"],
    visibility = ["//visibility:public"],
)

# ── Placeholder for future multi-language stubs ─────────────────────

# go_proto_library(name = "lma_go_proto", ...)
# kt_jvm_proto_library(name = "lma_kt_proto", ...)
# nanopb_proto_library(name = "lma_nanopb", ...)
