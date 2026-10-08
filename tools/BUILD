load("@rules_python//python:defs.bzl", "py_binary", "py_library")

py_library(
    name = "install_services_lib",
    srcs = ["install_services.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
    ],
)

py_library(
    name = "install_all_lib",
    srcs = ["install_all.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":install_services_lib",
        "//cardputer_client:flash_lib",
        "//lma_core",
        "//tests:e2e_helpers_lib",
        "@lmao_pip//pyserial",
        "@lmao_pip//rns",
    ],
)

py_binary(
    name = "install_all",
    srcs = ["install_all.py"],
    main = "install_all.py",
    data = [
        "//cardputer_client:client_files",
    ],
    visibility = ["//visibility:public"],
    deps = [":install_all_lib"],
)

# The repeatable foundation target for the native voice Cardputer: replicate
# `install_all --native-cardputer --device-name <name>` (build firmware with
# identity + DEST_HASH, flash, register in contact book) as one `bazel run`.
# The native firmware tree is read from the workspace via
# BUILD_WORKSPACE_DIRECTORY (see install_all._flash_cardputer_native).
py_binary(
    name = "flash_voice_cardputer",
    srcs = ["flash_voice_cardputer.py"],
    main = "flash_voice_cardputer.py",
    visibility = ["//visibility:public"],
    deps = [":install_all_lib"],
)
