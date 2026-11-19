# Shared ESP-IDF component: one-wire addressable RGB LED (SK6812 / WS2812)
# for the Atom Lite family — the Sprout status LED and the humidifier Atom
# Socket's relay indicator.  Single canonical copy; each device build.sh
# stages this directory into $RTR/firmware/<name>/components/lma_led.
idf_component_register(
    SRCS "led_rgb.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        esp_driver_rmt
        esp_driver_gpio
        log
)
