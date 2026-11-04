"""Shared Bazel macro for the RoboDog firmware tool entry points.

Every firmware build/flash/E2E entry point is a host script that execs the
source-tree copy (via BUILD_WORKSPACE_DIRECTORY on `bazel run`) and needs its
build inputs in runfiles.  They were six near-identical `sh_binary` blocks;
this is the one definition.
"""

def firmware_tool(name, script, data = []):
    """Declare a `manual` sh_binary host tool for the RoboDog firmware."""
    native.sh_binary(
        name = name,
        srcs = [script],
        data = data,
        tags = ["manual"],
    )
