# Shared ESP-IDF component: WiFi STA connect + SNTP (one copy for every LMAO
# LAN client — the RoboDog home client and the humidifier Atom Socket).
# Each device build.sh stages this directory into
# $RTR/firmware/<name>/components/lma_wifi.
idf_component_register(
    SRCS "wifi_helper.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        esp_wifi
        esp_netif
        esp_event
        esp_timer
        lwip
        log
)
