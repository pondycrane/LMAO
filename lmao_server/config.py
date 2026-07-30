"""
Reticulum configuration for LMAO Server (Raspberry Pi + ESP32 RNode).

The RNode is connected via USB serial and provides a transparent LoRa bridge.
WiFi AutoInterface is also enabled for local human-node communication.

Reticulum reads its configuration from a directory containing a 'config' file.
Use get_configdir() to create a temporary config directory with this content.
"""

import os

from lma_core.config_utils import RnsConfig

# Build config from the shared factory — only the transport path differs.
# LMAO_RNS_TRANSPORT_PATH overrides the Reticulum state path AND makes it
# persistent: the directory becomes the Reticulum configdir, so known
# destinations/identities (<dir>/storage) survive restarts.  In the K8s
# Deployment it points at the identity PVC (/data/transport) — without it
# every pod restart loses the Cardputer's identity until its next
# announce and the server's replies fail with "no source destination"
# (issue #93).  When unset, state stays in a throwaway temp dir.
_state_dir = os.environ.get("LMAO_RNS_TRANSPORT_PATH")

_cfg = RnsConfig(
    transport_path=_state_dir or "/tmp/lmao_server_rns_state",
    tempdir_prefix="lmao_rns_",
    persist_state=bool(_state_dir),
)

# Export the same names as before so callers are unaffected
get_configdir = _cfg.get_configdir
get_config_dict = _cfg.get_config_dict
CONFIG_CONTENT = _cfg.CONFIG_CONTENT
PERSIST_STATE = _cfg.persist_state
_RNODE_PORT = _cfg._RNODE_PORT
