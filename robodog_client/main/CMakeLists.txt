idf_component_register(
    SRCS
        "main.c"
        "robodog.c"
        "lma_proto.c"
        # lma_proto_selftest.c is a HOST-only test binary (it defines its own
        # main()); it is intentionally NOT part of the IDF build.
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_netif
        esp_event
        esp_hw_support
        esp_pm
        esp_timer
        lwip
        u8g2_st7305
)

# Opt-in (build with `-DRD_SHOT_CAPTURE=ON`): enable the u8g2 frame-dump hook
# that streams the exact panel buffer over the USB-JTAG console for host pixel
# capture (robodog_native/capture.py). Off by default — normal builds are
# unaffected.
option(RD_SHOT_CAPTURE "Enable u8g2 frame-dump capture hook" OFF)
if(RD_SHOT_CAPTURE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE RD_SHOT_CAPTURE=1)
endif()
