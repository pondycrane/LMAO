# Bazel view of the fetched upstream u8g2 source (WORKSPACE `u8g2` http_archive).
# NOTHING here is compiled by Bazel: the RoboDog firmware is built by ESP-IDF
# via build.sh, which stages the fetched csrc/ into the gitignored
# components/u8g2/csrc at build time.  Exposing csrc as a filegroup puts the
# extracted upstream tree in the sh_binary runfiles so `bazel run
# //robodog_native:build_firmware` needs no further network round-trip.
package(default_visibility = ["//visibility:public"])

filegroup(
    name = "src",
    srcs = glob(["csrc/**"]),
)
