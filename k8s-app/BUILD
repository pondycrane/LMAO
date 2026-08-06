load("@rules_python//python:defs.bzl", "py_library")

exports_files(["iot_ingest.py", "iot_ingest_consumer.py", "pipeline_monitor.py"])

py_library(
    name = "iot_ingest_consumer_lib",
    srcs = ["iot_ingest_consumer.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
        "//proto:lma_grpc_py_proto",
        "@lmao_pip//aiohttp",
        "@lmao_pip//sqlparse",
    ],
)

py_library(
    name = "pipeline_monitor_lib",
    srcs = ["pipeline_monitor.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
    ],
)
