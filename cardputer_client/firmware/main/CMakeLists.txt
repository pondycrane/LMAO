set(SRCS
    "main.cpp"
    "lora_interface.cpp"
    "radiolib_esp_idf_hal.cpp"
    "die_temp.cpp"
    "dht20.cpp"
    "chart.cpp"
    "st7789.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES
        lma_common
        rtreticulum
        driver
        esp_driver_i2c
        esp_driver_spi
        esp_driver_tsens
        esp_lcd
        esp_timer
        esp_hw_support
        nvs_flash
        log
        jgromes__RadioLib
)

# RTReticulum headers throw (Bytes bounds checks etc.); the default
# -fno-exceptions would break including them.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
