load("@rules_python//python:defs.bzl", "py_binary", "py_library")

# Cluster watchdog: deterministic health checks + agentic recovery.
#   bazel run //tools/cluster_watchdog:check
#   bazel run //tools/cluster_watchdog:recover -- restart-deployment default/lmao-server
#   sudo bash tools/cluster_watchdog/install.sh     # install the timer on selfhost
py_library(
    name = "watchdog_lib",
    srcs = ["watchdog.py"],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "cluster_watchdog",
    srcs = ["watchdog.py"],
    main = "watchdog.py",
    data = ["runbook.md"],
    visibility = ["//visibility:public"],
    deps = [":watchdog_lib"],
)

sh_binary(
    name = "install",
    srcs = ["install.sh"],
    data = [
        "runbook.md",
        "watchdog.env.example",
        ":cluster_watchdog",
    ],
    visibility = ["//visibility:public"],
)
