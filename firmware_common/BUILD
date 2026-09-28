# Shared firmware component (DRY): the protocol core + RNS utilities used by
# BOTH native client firmware trees (Sprite's smart_irrigation/native-client and
# the Cardputer cardputer_client).  Single canonical copy:
#   lma_common/lma_encoder.*   wire-compatible SensorReport/LMAOEnvelope encoder
#   lma_common/lxmf_send.*     send-only LXM body builder (RTReticulum)
#   lma_common/lma_identity.*  NVS identity persistence + hex/delivery helpers
#   lma_common/path_find.*     on-demand RNS path request (ESP-IDF only)
#   rtreticulum/CMakeLists.txt shared RNS lib wrapper for the ESP-IDF builds
#
# The pure-C++ units are host-testable here under Bazel (same code the device
# firmware compiles via ESP-IDF).  path_find + lma_identity pull ESP-IDF
# headers (esp_timer / nvs_flash), so they stay in the source graph only.

# ── Host-testable: SensorReport / LMAOEnvelope encoder (pure C++) ──
cc_library(
    name = "lma_encoder",
    srcs = ["lma_common/lma_encoder.cpp"],
    hdrs = ["lma_common/lma_encoder.h"],
    strip_include_prefix = "lma_common",
    visibility = ["//visibility:public"],
)

# ── Host-testable: send-only LXMF (RTReticulum-dependent) ──
cc_library(
    name = "lxmf_send",
    srcs = ["lma_common/lxmf_send.cpp"],
    hdrs = ["lma_common/lxmf_send.h"],
    strip_include_prefix = "lma_common",
    deps = ["@rtreticulum//:rtreticulum"],
    visibility = ["//visibility:public"],
)

# ── ESP-IDF-only sources (esp_timer/nvs_flash) — kept in the graph, built by
#    the firmware build.sh (Docker ESP-IDF), not under Bazel on the host. ──
filegroup(
    name = "shared_firmware_sources",
    srcs = glob([
        "lma_common/**/*.cpp",
        "lma_common/**/*.h",
        "rtreticulum/CMakeLists.txt",
        "lma_common/CMakeLists.txt",
    ]),
    visibility = ["//visibility:public"],
)
