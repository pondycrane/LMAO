set(SRCS
    "main.cpp"
    "uart_at_interface.cpp"
    "path_find.cpp"
    "sht30.cpp"
    "lma_encoder.cpp"
    "lxmf_send.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES
        rtreticulum
        driver
        esp_driver_uart
        esp_driver_i2c
        esp_timer
        esp_hw_support
        nvs_flash
        log
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
