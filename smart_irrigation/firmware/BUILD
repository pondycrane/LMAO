# Smart-irrigation Atom Lite device files.
#
# MicroPython sources are transferred to the device (mpremote / flash tool)
# and are never executed by host Python — expose them as files/filegroups
# instead of py_library targets, mirroring cardputer_client/BUILD.

exports_files([
    "tools/probe_hardware.py",
    "tools/dtu_p2p_probe.py",
])

filegroup(
    name = "dtu_lib",
    srcs = [
        "lib/dtu/__init__.py",
        "lib/dtu/dtu_at.py",
    ],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "firmware_files",
    srcs = glob(["**/*.py"]),
    visibility = ["//visibility:public"],
)
