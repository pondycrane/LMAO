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

# ── Host-testable: LMAF attachment framing (pure C++) ──
# Split/reassemble framing for payloads larger than one LXMF packet; the
# canonical copy both device trees compile via components/lma_common.
cc_library(
    name = "lma_attachment",
    srcs = ["lma_common/lma_attachment.cpp"],
    hdrs = ["lma_common/lma_attachment.h"],
    strip_include_prefix = "lma_common",
    visibility = ["//visibility:public"],
)

# Golden bytes generated from proto/lma_messages.proto (see the test header):
# the MCU encoder and the protobuf-generated server stubs must never drift.
cc_test(
    name = "lma_attachment_test",
    srcs = ["tests/lma_attachment_test.cpp"],
    deps = [":lma_attachment"],
    tags = ["unit"],
)

# ── Host-testable: inbound LXMF payload reader (pure C++) ──
cc_library(
    name = "lma_lxm",
    srcs = ["lma_common/lma_lxm.cpp"],
    hdrs = ["lma_common/lma_lxm.h"],
    strip_include_prefix = "lma_common",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lma_lxm_test",
    srcs = ["tests/lma_lxm_test.cpp"],
    deps = [":lma_lxm"],
    tags = ["unit"],
)

# ── Host-testable: device-side LMAF receive glue ──
# Manifest/chunk handling, digest verification and ack construction — the
# device tree only feeds bytes in and transmits the returned envelope.
cc_library(
    name = "lma_lmaf_rx",
    srcs = ["lma_common/lma_lmaf_rx.cpp"],
    hdrs = ["lma_common/lma_lmaf_rx.h"],
    strip_include_prefix = "lma_common",
    deps = [
        ":lma_attachment",
        ":lma_lxm",
    ],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lma_lmaf_rx_test",
    srcs = ["tests/lma_lmaf_rx_test.cpp"],
    deps = [":lma_lmaf_rx"],
    tags = ["unit"],
)

# ── Host-testable: outbound dead-letter queue (pure C++) ──
# The sender's half of loss recovery: keep what was transmitted, re-send on the
# shared RetryPolicy schedule until something proves delivery, then give up
# loudly.  Used by the device clients and mirrored (same policy table) by the
# server.
cc_library(
    name = "lma_tx_queue",
    srcs = ["lma_common/lma_tx_queue.cpp"],
    hdrs = ["lma_common/lma_tx_queue.h"],
    strip_include_prefix = "lma_common",
    deps = [":lma_attachment"],
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lma_tx_queue_test",
    srcs = ["tests/lma_tx_queue_test.cpp"],
    deps = [":lma_tx_queue"],
    tags = ["unit"],
)

# ── Host-testable: RNode wire framing / split reassembly (pure C++) ──
# Keeps the fix for the measured regression: on a shared channel, fragments must
# be keyed by their tag, or an interleaved frame destroys a half-built packet.
cc_library(
    name = "lma_rnode_framing",
    srcs = ["lma_common/lma_rnode_framing.cpp"],
    hdrs = ["lma_common/lma_rnode_framing.h"],
    strip_include_prefix = "lma_common",
    visibility = ["//visibility:public"],
)

cc_test(
    name = "lma_rnode_framing_test",
    srcs = ["tests/lma_rnode_framing_test.cpp"],
    deps = [":lma_rnode_framing"],
    tags = ["unit"],
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
