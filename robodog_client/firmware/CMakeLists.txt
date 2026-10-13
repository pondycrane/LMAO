# RoboDog LVGL UI framework project (ESP32-S3, Waveshare ESP32-S3 RLCD-4.2).
# PR 1 — UI framework build (no external components: LVGL comes from the
# component registry via main/idf_component.yml).
cmake_minimum_required(VERSION 3.16)
# No EXTRA_COMPONENT_DIRS for this build; LVGL is fetched by the component
# manager (managed_components/, gitignored).
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(robodog_lvgl)
