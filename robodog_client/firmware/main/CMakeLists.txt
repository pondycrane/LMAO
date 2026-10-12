set(SRCS
    "main.cpp"
    "wifi_helper.cpp"
    "tcp_interface.cpp"
    "calendar_net.cpp"
    "st7305_disp.cpp"
    "calendar_ui.cpp"
    "calendar_model.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    EMBED_TXTFILES "ui_xml/calendar_screen.xml"
    REQUIRES
        lma_common
        rtreticulum
        lvgl
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
        esp_timer
        esp_driver_spi
        esp_driver_gpio
        lwip
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
