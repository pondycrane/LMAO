cmake_minimum_required(VERSION 3.16)

# RoboDog home client — ESP32-S3 + Waveshare RLCD-4.2 (ST7305) air
# temperature/humidity reporter + chart display.  Built inside the pinned
# RTReticulum checkout by build.sh (see README).
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(robodog_home_native)
