# Shared ESP-IDF component: wire-compatible LMAO protocol + RNS utilities
# used by BOTH firmware trees (Sprout native-client and the Cardputer native
# client).  build.sh in each tree stages this directory into
# $RTR/firmware/<name>/components/lma_common, so it compiles once per device
# build from a single canonical copy (no per-tree duplication).
#
# The three sources have no per-device pins (no SX1262/UART/TSENS/NVS — the
# radio/identity layer stays in each device's main/).  This is the same code
# the Bazel host tests exercise via //firmware_common targets.

idf_component_register(
    SRCS
        "lma_encoder.cpp"
        "lma_decode.cpp"
        "lxmf_send.cpp"
        "lxm_recv.cpp"
        "path_find.cpp"
        "lma_identity.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        rtreticulum
        esp_timer
        esp_hw_support
        nvs_flash
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
