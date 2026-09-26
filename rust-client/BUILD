# ESP32 Rust LMAO client — Bazel mirror (design §5 / ticket T0).
#
# No rules_rust in this repo; the Rust host toolchain builds out-of-band (as
# does the native-client ESP-IDF firmware — same convention). This package
# keeps the whole Rust tree in the Bazel graph (filegroup) so *all* new code is
# Bazel-reachable, and wraps the fetch/build/gate flows as manual sh_binary
# targets so everything is driven from Bazel. All are `manual`: the T0 gate
# spawns a live Python RNS peer, and the Rust build shells out to `cargo`.
# Nothing here is pulled into `bazel test //tests:...`.
#
#   bazel build  //rust-client:build                        # cargo build (all)
#   bazel run    //rust-client:run_t0_gate -- --python <py> # T0 DECISION GATE
#   bazel build  //rust-client:rust_sources                 # source tree in graph
#
# Stable MicroPython revert (hardware, later): the Rust firmware is flashed to
# the Cardputer only from T1+; restoring the old MicroPython urns path is the
# existing canonical `bazel run //cardputer_client:flash` (raw REPL, re-injects
# the server DEST_HASH) — see UPSTREAM.md / T0-GATE.md.

filegroup(
    name = "rust_sources",
    srcs = glob(
        ["**"],
        exclude = [
            "target/**",  # cargo build output — never part of the Bazel graph
        ],
    ),
    visibility = ["//visibility:public"],
)

sh_binary(
    name = "build",
    srcs = ["build.sh"],
    data = [":rust_sources"],
    tags = ["manual"],
)

sh_binary(
    name = "run_t0_gate",
    srcs = ["run_t0_gate.sh"],
    data = [":rust_sources"],
    tags = ["manual"],
)
