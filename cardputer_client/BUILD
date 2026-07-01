load("@rules_python//python:defs.bzl", "py_binary", "py_library")

# Export MicroPython source files so the e2e test can reference them as
# data dependencies.  These files are NOT compiled by host Python — they
# are only copied to the Cardputer device at flash time.
exports_files([
    "main.py",
    "config.py",
])

# ── Flash library (reusable by e2e tests) ──────────────────────────

py_library(
    name = "flash_lib",
    srcs = ["flash.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [
        "@lmao_pip//pyserial",
    ],
)

# ── Flash binary (``bazel run //cardputer_client:flash``) ──────────

py_binary(
    name = "flash",
    srcs = ["flash.py"],
    main = "flash.py",
    visibility = ["//visibility:public"],
    deps = [":flash_lib"],
)
