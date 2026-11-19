cmake_minimum_required(VERSION 3.16)

# Humidifier Atom Socket controller — M5Stack Atom Socket (Atom Lite, ESP32)
# relay + HLW8032 metering actuator with a leased HTTP control API.
# Built inside the pinned RTReticulum checkout by build.sh (see README).
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(humidifier_socket)
