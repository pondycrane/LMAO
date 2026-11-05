idf_component_register(
    SRCS
        "main.cpp"
        "wifi_helper.cpp"
        "tcp_interface.cpp"
        "shtc3.cpp"
        "env_net.cpp"
        "ui.cpp"
        "rd_chart.c"
        # rd_chart_selftest.c is HOST-only (defines its own main) and is not
        # part of the IDF build; see the Bazel cc_test.
    INCLUDE_DIRS "."
    REQUIRES
        lma_common
        rtreticulum
        u8g2_st7305
        nvs_flash
        esp_wifi
        esp_netif
        esp_event
        esp_timer
        esp_system
        esp_hw_support
        driver
        lwip
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
