load("@rules_python//python:defs.bzl", "py_binary", "py_library")

# Export MicroPython source files so the e2e test can reference them as
# data dependencies.  These files are NOT compiled by host Python — they
# are only copied to the Cardputer device at flash time.
exports_files([
    "main.py",
    "config.py",
    "lora_boards.py",
])

# ── µReticulum library files (urns port + native modules) ──────────
# Uploaded to /lib/ on the Cardputer.  Not compiled — only transferred.
filegroup(
    name = "lib_files",
    srcs = glob([
        "lib/**/*.py",
        "lib/**/*.mpy",
    ]),
    visibility = ["//visibility:public"],
)

# ── All client files (convenience alias) ──────────────────────────

filegroup(
    name = "client_files",
    srcs = [
        "config.py",
        "lora_boards.py",
        "main.py",
        "//cardputer_client/proto:lma_encoder.py",
        ":lib_files",
    ],
    visibility = ["//visibility:public"],
)

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
# Uploads the LMAO client + µReticulum library to the Cardputer.

py_binary(
    name = "flash",
    srcs = ["flash.py"],
    main = "flash.py",
    data = [":client_files"],
    visibility = ["//visibility:public"],
    deps = [":flash_lib"],
)
