"""
Reticulum configuration for Human Client (laptop/desktop CLI).

Provides WiFi AutoInterface (always enabled) and an optional RNode LoRa
interface.  The RNode is optional — if the port is not found the client
starts in WiFi-only mode with a warning, unlike the server which requires
an RNode for its primary LoRa interface.

Reticulum reads its configuration from a directory containing a 'config' file.
Use get_configdir() to create a temporary config directory with this content.
"""

import os
import tempfile

from lma_core.config_utils import resolve_rnode_port, dict_to_ini

# Backward-compatible re-exports (used by tests)
_resolve_rnode_port = resolve_rnode_port
_dict_to_ini = dict_to_ini


# Single source of truth for all config values
_SECTIONS = {
    "logging": {
        "loglevel": 4,
    },
    "transport": {
        "path": "/tmp/lmao_human_client_rns_state",
    },
}

# Resolve RNode port once at module load time so it's visible in the startup banner
_RNODE_PORT = resolve_rnode_port()

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
CONFIG_CONTENT = dict_to_ini(_SECTIONS, _INTERFACES)


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
