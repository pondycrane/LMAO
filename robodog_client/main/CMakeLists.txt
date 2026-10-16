idf_component_register(
    SRCS
        "main.c"
        "robodog.c"
        "lma_proto.c"
        "rd_clip.c"
        "rd_datetime.c"
        # lma_proto_selftest.c and rd_datetime_selftest.c are HOST-only test
        # binaries (each defines its own main()); they are intentionally NOT
        # part of the IDF build.
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

# The u8g2 frame-dump capture hook is ALWAYS compiled in (rd_shot_dump in
# robodog.c): capture.py is how a host confirms what the current production
# firmware rendered, so it must not be a separate opt-in build.
