"""
Reticulum configuration for LMAO Server (Raspberry Pi + ESP32 RNode).

The RNode is connected via USB serial and provides a transparent LoRa bridge.
WiFi AutoInterface is also enabled for local human-node communication.

Reticulum reads its configuration from a directory containing a 'config' file.
Use get_configdir() to create a temporary config directory with this content.
"""

import os
import sys
import tempfile


def _resolve_rnode_port():
    """Return the RNode serial port.

    Priority:
    1. LMAO_RNODE_PORT environment variable
    2. Auto-detect common ports (first found)
    3. Default /dev/ttyUSB0
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


def _dict_to_ini(sections, interfaces):
    """Convert sections and interfaces dicts to Reticulum INI format.

    Top-level sections use [bracket] syntax.
    Interface sections use [[double-bracket]] syntax.
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


# Single source of truth for all config values
_SECTIONS = {
    "logging": {
        "loglevel": 4,
    },
    "transport": {
        "path": "/tmp/lmao_server_rns_state",
    },
}

# Resolve RNode port once at module load time so it's visible in the startup banner
_RNODE_PORT = _resolve_rnode_port()

_INTERFACES = {
    "RNode LoRa": {
        "type": "RNodeInterface",
        "port": _RNODE_PORT,
        "frequency": 868000000,
        "bandwidth": 125000,
        "spreadingfactor": 7,
        "codingrate": 5,
        "txpower": 17,
    },
    "WiFi": {
        "type": "AutoInterface",
        "enabled": True,
    },
}

# Generate the INI-format config string from the single-source dicts
CONFIG_CONTENT = _dict_to_ini(_SECTIONS, _INTERFACES)


def get_configdir():
    """Create a temporary config directory for Reticulum.

    Returns the path to the directory. Caller is responsible for cleanup.
    """
    configdir = tempfile.mkdtemp(prefix="lmao_rns_")
    config_path = os.path.join(configdir, "config")
    with open(config_path, "w") as f:
        f.write(CONFIG_CONTENT)
    return configdir


# For direct dict access (some utilities may need it)
def get_config_dict():
    """Return the config as a dict for introspection."""
    return {
        "interfaces": {
            "RNode LoRa": dict(_INTERFACES["RNode LoRa"]),
            "WiFi": dict(_INTERFACES["WiFi"]),
        },
        "transport": dict(_SECTIONS["transport"]),
        "logging": dict(_SECTIONS["logging"]),
    }
