"""
Reticulum configuration for LMAO Server (Raspberry Pi + ESP32 RNode).

The RNode is connected via USB serial and provides a transparent LoRa bridge.
WiFi AutoInterface is also enabled for local human-node communication.

Reticulum reads its configuration from a directory containing a 'config' file.
Use get_configdir() to create a temporary config directory with this content.
"""

import os
import tempfile

# The config content as a string (Reticulum config file format is plain text)
CONFIG_CONTENT = """\
# LMAO Server — Reticulum Configuration

[logging]
loglevel = 4

[transport]
path = /tmp/lmao_server_rns_state

[[RNode LoRa]]
type = RNodeInterface
port = /dev/ttyUSB0
frequency = 868000000
bandwidth = 125000
spreadingfactor = 7
codingrate = 5
txpower = 17

[[WiFi]]
type = AutoInterface
enabled = yes
"""


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
            "RNode LoRa": {
                "type": "RNodeInterface",
                "port": "/dev/ttyUSB0",
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
        },
        "transport": {
            "path": "/tmp/lmao_server_rns_state",
        },
        "logging": {
            "loglevel": 4,
        },
    }
