# Bundles the family-calendar line-drawing pack as runfiles for the agent
# skill tool (`//tools:calendar_pics`), which renders them to 1-bit B&W tiles
# and stores them in Postgres (`cal_pics`).  The SVGs are client/display art
# too — this filegroup only wires them into Bazel so an agent can rasterize
# any pack key with `bazel run //tools:calendar_pics -- --key <key>`.

filegroup(
    name = "files",
    srcs = glob(["*.svg"]),
    visibility = ["//visibility:public"],
)
