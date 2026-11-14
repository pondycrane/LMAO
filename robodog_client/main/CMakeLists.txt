idf_component_register(
    SRCS
        "main.c"
        "robodog.c"
        "rd_store.c"
        "rd_net.c"
        "rd_render.c"
        "rd_layout.c"
        "lma_proto.c"
        "rd_clip.c"
        "rd_datetime.c"
        "rd_weather.c"
        # lma_proto_selftest.c / rd_layout_selftest.c / rd_clip_selftest.c /
        # rd_datetime_selftest.c / rd_weather_selftest.c are HOST-only test
        # binaries (each defines its own main()); they are intentionally NOT
        # part of the IDF build (Bazel cc_test runs them, see BUILD).
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_netif
        esp_event
        esp_hw_support
        esp_pm
        esp_timer
        esp_system   # esp_task_wdt.h lives here in IDF 5.x (no separate component)
        esp_driver_usb_serial_jtag  # usb_serial_jtag_is_connected() gates RD_SHOT
        lwip
        u8g2_st7305
)

# The u8g2 frame-dump capture hook is ALWAYS compiled in (rd_shot_dump in
# rd_render.c): capture.py is how a host confirms what the current production
# firmware rendered, so it must not be a separate opt-in build.
