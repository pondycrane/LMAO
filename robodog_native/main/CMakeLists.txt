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
