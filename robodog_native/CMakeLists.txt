cmake_minimum_required(VERSION 3.16)

# RoboDog — native ESP-IDF family-calendar display (ESP32-S3 + Waveshare
# RLCD-4.2 / ST7305).  The ST7305 and u8g2 drivers are vendored third-party
# components under components/ and are auto-discovered by IDF.
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(robodog_native)
