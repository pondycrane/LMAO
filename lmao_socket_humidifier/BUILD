load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")
load("//robodog_client:firmware_tools.bzl", "firmware_tool")

# ── LMAO socket humidifier: room humidity → Atom Socket ─────────────────
#
#   bazel run //lmao_socket_humidifier:controller -- \
#       --env-node "baby room" --socket humidifier-baby.local
#   bazel run //lmao_socket_humidifier:deploy        # K8s controller
#   bazel run //lmao_socket_humidifier:build_firmware
#   bazel run //lmao_socket_humidifier:flash_firmware -- --port /dev/ttyUSB0 ...
#
# One device per room: the socket carries an ENV III, so it is BOTH the sensor
# and the actuator.  The controller prefers the socket's own /status reading
# (direct, no server round trip) and falls back to the server's history for
# --env-node (the socket also reports SensorReport over Reticulum TCP, so the
# room keeps its air series).

# ── Host-testable firmware units (pure C++, no ESP-IDF) ────────────────
cc_library(
    name = "hlw8032",
    srcs = ["firmware/main/hlw8032.cpp"],
    hdrs = ["firmware/main/hlw8032.h"],
    strip_include_prefix = "firmware/main",
)

cc_library(
    name = "directive",
    srcs = ["firmware/main/directive.cpp"],
    hdrs = ["firmware/main/directive.h"],
    strip_include_prefix = "firmware/main",
)

cc_library(
    name = "relay_lease",
    srcs = ["firmware/main/relay_lease.cpp"],
    hdrs = ["firmware/main/relay_lease.h"],
    strip_include_prefix = "firmware/main",
)

cc_test(
    name = "socket_unit_test",
    srcs = ["firmware/tests/socket_unit_test.cpp"],
    deps = [
        ":directive",
        ":hlw8032",
        ":relay_lease",
        "//firmware_common:sht30_codec",
    ],
    tags = ["unit"],
)

# ── Controller (host Python) ───────────────────────────────────────────
py_library(
    name = "control",
    srcs = ["control.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "socket_client",
    srcs = ["socket_client.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "directive_pub",
    srcs = ["directive.py"],
    imports = [".."],
    visibility = ["//visibility:public"],
)

py_library(
    name = "controller_lib",
    srcs = [
        "__init__.py",
        "controller.py",
    ],
    imports = [".."],
    visibility = ["//visibility:public"],
    deps = [
        ":control",
        ":directive_pub",
        ":socket_client",
        "//robodog_home_client:home_client_lib",
    ],
)

py_binary(
    name = "controller",
    srcs = [
        "__init__.py",
        "controller.py",
    ],
    imports = [".."],
    main = "controller.py",
    visibility = ["//visibility:public"],
    deps = [":controller_lib"],
)

py_test(
    name = "control_test",
    srcs = ["tests/test_control.py"],
    main = "tests/test_control.py",
    deps = [
        ":control",
        "@lmao_pip//pytest",
    ],
    tags = ["unit"],
)

py_test(
    name = "socket_client_test",
    srcs = ["tests/test_socket_client.py"],
    main = "tests/test_socket_client.py",
    deps = [
        ":socket_client",
        "@lmao_pip//pytest",
    ],
    tags = ["unit"],
)

py_test(
    name = "directive_test",
    srcs = ["tests/test_directive.py"],
    main = "tests/test_directive.py",
    deps = [
        ":directive_pub",
        "@lmao_pip//pytest",
    ],
    tags = ["unit"],
)

py_test(
    name = "controller_test",
    srcs = ["tests/test_controller.py"],
    main = "tests/test_controller.py",
    deps = [
        ":controller_lib",
        "@lmao_pip//pytest",
    ],
    tags = ["unit"],
)

# ── Cluster deploy (build image + push + kubectl apply + rollout) ─────
# Same function install_all --include-services calls; this is the standalone
# entry point.  Manual: it needs docker, kubectl and the host secrets.
py_binary(
    name = "deploy",
    srcs = ["deploy.py"],
    main = "deploy.py",
    tags = ["manual"],
    visibility = ["//visibility:public"],
    deps = ["//tools:install_services_lib"],
)

# ── Firmware build / flash (manual — Docker ESP-IDF, device-attached) ──
firmware_tool(
    name = "build_firmware",
    script = "firmware/build.sh",
    data = glob([
        "firmware/main/*",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]) + [
        "//firmware_common:shared_firmware_sources",
        "//smart_irrigation/native-client:rtreticulum_fixes.patch",
    ],
)

firmware_tool(
    name = "flash_firmware",
    script = "firmware/flash.sh",
    data = glob([
        "firmware/main/*",
        "firmware/CMakeLists.txt",
        "firmware/sdkconfig.defaults",
        "firmware/partitions.csv",
    ]) + [
        "//firmware_common:shared_firmware_sources",
        "//smart_irrigation/native-client:rtreticulum_fixes.patch",
    ],
)
