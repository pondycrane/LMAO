# Cardputer native firmware project (ESP32-S3). Build inside a RTReticulum
# checkout via build.sh (the rtreticulum component wrapper is copied in).
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/components)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cardputer_native)
