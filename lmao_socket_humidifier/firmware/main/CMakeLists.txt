set(SRCS
    "main.cpp"
    "hlw8032.cpp"
    "relay_lease.cpp"
    "net_report.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES
        lma_wifi
        lma_led
        lma_sensor
        lma_common
        tcp_interface
        rtreticulum
        driver
        esp_driver_uart
        esp_driver_gpio
        esp_driver_i2c
        esp_driver_rmt
        esp_http_server
        mdns
        esp_timer
        esp_event
        esp_netif
        esp_wifi
        lwip
        nvs_flash
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
