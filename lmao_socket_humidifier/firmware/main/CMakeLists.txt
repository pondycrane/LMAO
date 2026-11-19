set(SRCS
    "main.cpp"
    "hlw8032.cpp"
    "relay_lease.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES
        lma_wifi
        lma_led
        driver
        esp_driver_uart
        esp_driver_gpio
        esp_driver_rmt
        esp_http_server
        mdns
        esp_timer
        nvs_flash
        log
)
