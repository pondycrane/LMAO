load("@rules_python//python:defs.bzl", "py_binary", "py_library")

py_library(
    name = "install_services_lib",
    srcs = ["install_services.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
    ],
)

py_library(
    name = "install_all_lib",
    srcs = ["install_all.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":install_services_lib",
        "//cardputer_client:flash_lib",
        "//lma_core",
        "//tests:e2e_helpers_lib",
        "@lmao_pip//pyserial",
        "@lmao_pip//rns",
    ],
)

py_binary(
    name = "install_all",
    srcs = ["install_all.py"],
    main = "install_all.py",
    data = [
        "//cardputer_client:client_files",
    ],
    visibility = ["//visibility:public"],
    deps = [":install_all_lib"],
)

# The repeatable foundation target for the native voice Cardputer: replicate
# `install_all --native-cardputer --device-name <name>` (build firmware with
# identity + DEST_HASH, flash, register in contact book) as one `bazel run`.
# The native firmware tree is read from the workspace via
# BUILD_WORKSPACE_DIRECTORY (see install_all._flash_cardputer_native).
py_binary(
    name = "flash_voice_cardputer",
    srcs = ["flash_voice_cardputer.py"],
    main = "flash_voice_cardputer.py",
    visibility = ["//visibility:public"],
    deps = [":install_all_lib"],
)

# The family-calendar line-drawing skill: an agent (Hermes) rasterizes an SVG
# or PNG source (a local file, or a small line drawing fetched from an http(s)
# URL) to a 1-bit B&W tile (RLCD 300x400-friendly, default 144x144) and
# upserts the PNG bytes into the in-cluster Postgres `cal_pics` table via
# lma_core.calendar_pics.PicStore.
#   bazel run //tools:calendar_pics -- --key swim
#   bazel run //tools:calendar_pics -- --key robodog --source https://…/dog.svg
#   bazel run //tools:calendar_pics -- --key shopping --source art.svg --out check.png
py_binary(
    name = "calendar_pics",
    srcs = ["calendar_pics.py"],
    main = "calendar_pics.py",
    data = ["//assets/calendar-icons:files"],
    deps = [
        "//lma_core",
        "@lmao_pip//cairosvg",
        "@lmao_pip//pillow",
        "@lmao_pip//psycopg",
        "@lmao_pip//psycopg_binary",
    ],
)

# Calendar CronJob stage 2: for each upcoming kid event without a picture,
# pick a seeded theme subject (Juliet: random pokemon; Wesley: alternating
# random PAW Patrol character / coherent sea animal), generate or fetch line
# art, and upsert the 1-bit RLCD tile into `cal_pics`.
#   bazel run //tools:calendar_pics_sync
#   bazel run //tools:calendar_pics_sync -- --dry-run
#   bazel run //tools:calendar_pics_sync -- --force --since-days 14   # backfill
py_library(
    name = "calendar_pics_sync_lib",
    srcs = ["calendar_pics_sync.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//lma_core",
        "@lmao_pip//pillow",
        "@lmao_pip//psycopg",
        "@lmao_pip//psycopg_binary",
    ],
)

py_binary(
    name = "calendar_pics_sync",
    srcs = ["calendar_pics_sync.py"],
    main = "calendar_pics_sync.py",
    visibility = ["//visibility:public"],
    deps = [":calendar_pics_sync_lib"],
)

# Drift guard: fails if a Turing-Pi LAN address (DHCP, has moved before) is
# hardcoded in a runtime path.  Address nodes by hostname instead.
#   bazel run //tools:check_node_addresses
py_library(
    name = "check_node_addresses_lib",
    srcs = ["check_node_addresses.py"],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "check_node_addresses",
    srcs = ["check_node_addresses.py"],
    main = "check_node_addresses.py",
    visibility = ["//visibility:public"],
    deps = [":check_node_addresses_lib"],
)

# dsh-superagent host deployment (issue #303): the bundle runs on selfhost as a
# systemd service under the dedicated `dsh-agent` account. The installer is a
# host script (it needs root for the unit + env file); the settings tool talks
# to the loopback Management API of that profile.
#   sudo bash tools/dsh_superagent_install.sh
#   python3 tools/dsh_superagent_settings.py
sh_binary(
    name = "dsh_superagent_install",
    srcs = ["dsh_superagent_install.sh"],
    data = ["dsh_superagent_settings.py"],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "dsh_superagent_settings",
    srcs = ["dsh_superagent_settings.py"],
    main = "dsh_superagent_settings.py",
    visibility = ["//visibility:public"],
)
