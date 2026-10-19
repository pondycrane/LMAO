"""Bazel fetch macro for the RoboDog voice-firmware flash.

The XiaoZhi AI voice firmware for the Waveshare ESP32-S3-RLCD-4.2 is a
prebuilt release package published by Waveshare (https://github.com/waveshareteam/ESP32-AIChats).
Per the repo no-vendoring rule we never commit the upstream binary: it is
fetched through Bazel's http_file (cached in the Bazel repo cache) and carried
in the flash target's runfiles by `//robodog_client:flash_robodog_hermes`.

The URL is pinned to the immutable commit of the day (not the moving
`master` branch) AND the file is sha256-pinned, so any upstream change fails
loudly at fetch time instead of silently shipping a different image.
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")

# waveshareteam/ESP32-AIChats commit that carries the v2.4.2 package.
_XIAOZHI_COMMIT = "1ab61f11d3394067020d71ed0759cf965d0447b5"

# sha256 of v2.4.2_waveshare-esp32-s3-rlcd-4.2.zip (verified 2026-10-18).
_XIAOZHI_SHA256 = "61171a7c2f53abb4f91e16ce1ba7844fb3e00e289ff701128819e44bd5c3b92b"

_XIAOZHI_URL = (
    "https://raw.githubusercontent.com/waveshareteam/ESP32-AIChats/"
    + _XIAOZHI_COMMIT
    + "/xiaozhi-esp32/v2.4.2/ESP32-S3/"
    + "v2.4.2_waveshare-esp32-s3-rlcd-4.2.zip"
)


def robodog_xiaozhi_fetch():
    """Declare the XiaoZhi RLCD-4.2 voice-firmware download repository.

    Call from WORKSPACE. The resulting single-file repo exposes target
    `@xiaozhi_rlcd_firmware//file` (used by the genrule in robodog_client/BUILD
    to extract merged-binary.bin for the flash target).
    """
    http_file(
        name = "xiaozhi_rlcd_firmware",
        urls = [_XIAOZHI_URL],
        sha256 = _XIAOZHI_SHA256,
        downloaded_file_path = "xiaozhi_rlcd_firmware.zip",
    )
