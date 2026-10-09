idf_component_register(
    SRCS "es8311.c" "audio.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_i2c esp_driver_i2s log
)
