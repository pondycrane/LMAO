# Build/flash targets for the robodog_client firmware (Waveshare ESP32-S3
# RLCD, family-calendar display).  Build runs ESP-IDF in Docker; flash runs
# esptool on the HOST because Docker's ttyACM RTS passthrough breaks the
# button-free reset (EPROTO), see flash.sh.
#
#   bazel run //robodog_client:build_firmware
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM0]

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
    ]) + [
        # u8g2 upstream source is fetched (never vendored); carried in runfiles
        # so `bazel run //robodog_client:build_firmware` needs no network.
        "@u8g2//:src",
    ],
    tags = ["manual"],
)

sh_binary(
    name = "flash_firmware",
    srcs = ["flash.sh"],
    data = glob(["build/flash_args", "build/robodog_client.bin"]),
    tags = ["manual"],
)
