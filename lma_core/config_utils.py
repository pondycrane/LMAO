"""Shared Reticulum config utilities.

Functions used by both the server and human client to resolve RNode ports
and generate Reticulum INI-format configurations. Extracted from the
originally duplicated config modules to eliminate maintenance drift.

Module-level functions:
    - resolve_rnode_port() — env var, auto-detect, or default RNode port
    - dict_to_ini() — Python dict to Reticulum INI format

Both are imported by component-specific config.py files that keep their
own section/interface definitions (transport path, radio params, etc.).
"""

import os


def resolve_rnode_port():
    """Return the RNode serial port.

    Priority:
    1. LMAO_RNODE_PORT environment variable
    2. Auto-detect common ports (first found)
    3. Default /dev/ttyUSB0

    Returns:
        str: Path to the RNode device (e.g., "/dev/ttyUSB0").
    """
    env_port = os.environ.get("LMAO_RNODE_PORT")
    if env_port:
        return env_port

    # Auto-detect: check common ports
    common_ports = ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyACM1"]
    for port in common_ports:
        if os.path.exists(port):
            return port

    return "/dev/ttyUSB0"


def dict_to_ini(sections, interfaces):
    """Convert sections and interfaces dicts to Reticulum INI format.

    Top-level sections use [bracket] syntax.
    Interface sections use [[double-bracket]] syntax.

    Args:
        sections: Dict of top-level section name -> {key: value}.
        interfaces: Dict of interface name -> {key: value}.

    Returns:
        str: INI-formatted config string with trailing newline.
    """
    lines = []
    for section, settings in sections.items():
        lines.append(f"[{section}]")
        for key, value in settings.items():
            if isinstance(value, bool):
                value = "yes" if value else "no"
            lines.append(f"{key} = {value}")
    for name, settings in interfaces.items():
        lines.append(f"[[{name}]]")
        for key, value in settings.items():
            if isinstance(value, bool):
                value = "yes" if value else "no"
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"
