# Bazel view of the fetched upstream codec2 source (WORKSPACE `codec2`).
# NOTHING here is compiled by Bazel: the firmware build (build.sh ->
# codec2_prepare.sh) copies src/ into the ESP-IDF component at build time.
# Exposing src/ as a filegroup puts the extracted upstream tree in the
# sh_binary's runfiles, so `bazel run //cardputer_client:build_firmware` needs
# no further network round-trip.
package(default_visibility = ["//visibility:public"])

filegroup(
    name = "src",
    srcs = glob(["src/**"]),
)
