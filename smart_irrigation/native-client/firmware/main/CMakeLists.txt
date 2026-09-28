set(SRCS
    "main.cpp"
    "uart_at_interface.cpp"
    "sht30.cpp"
    "moisture.cpp"
    "control.cpp"
    "pump.cpp"
    "button_led.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES
        lma_common
        rtreticulum
        driver
        esp_driver_uart
        esp_driver_i2c
        esp_driver_rmt
        esp_timer
        esp_hw_support
        nvs_flash
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
