# Shared ESP-IDF component: SHT30 air temperature + humidity (M5Stack ENV III)
# for the Sprout (DTU Port A) and the humidifier Atom Socket (ENV III on the
# Atom Lite's I2C).  Single canonical copy; each device build.sh stages this
# directory into $RTR/firmware/<name>/components/lma_sensor.
idf_component_register(
    SRCS "sht30.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        esp_driver_i2c
        log
)

# The codec header is pure C++ (host-tested under Bazel); nothing to compile.
