load("@rules_python//python:defs.bzl", "py_binary", "py_library")

py_library(
    name = "install_services_lib",
    srcs = ["install_services.py"],
    visibility = ["//visibility:public"],
    deps = [],
)

py_library(
    name = "install_all_lib",
    srcs = ["install_all.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":install_services_lib",
        "//cardputer_client:flash_lib",
        "//tests:e2e_helpers_lib",
        "@lmao_pip//pyserial",
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
