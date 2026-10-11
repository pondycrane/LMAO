# Build/flash targets for the robodog_native firmware (Waveshare ESP32-S3
# RLCD, family-calendar display).  Build runs ESP-IDF in Docker; flash runs
# esptool on the HOST because Docker's ttyACM RTS passthrough breaks the
# button-free reset (EPROTO), see flash.sh.
#
#   bazel run //robodog_native:build_firmware
#   bazel run //robodog_native:flash_firmware [-- --port /dev/ttyACM0]

sh_binary(
    name = "build_firmware",
    srcs = ["build.sh"],
    data = glob([
        "main/*",
        "components/**/*",
        "sdkconfig.defaults",
        "sdkconfig",
        "partitions.csv",
        "CMakeLists.txt",
    ]),
    tags = ["manual"],
)

sh_binary(
    name = "flash_firmware",
    srcs = ["flash.sh"],
    data = glob(["build/flash_args", "build/robodog_native.bin"]),
    tags = ["manual"],
)
