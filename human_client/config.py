"""
Reticulum configuration for Human Client (laptop/desktop CLI).

Provides WiFi AutoInterface (always enabled) and an optional RNode LoRa
interface.  The RNode is optional — if the port is not found the client
starts in WiFi-only mode with a warning, unlike the server which requires
an RNode for its primary LoRa interface.

Reticulum reads its configuration from a directory containing a 'config' file.
Use get_configdir() to create a temporary config directory with this content.
"""

from lma_core.config_utils import RnsConfig

# Build config from the shared factory — only the transport path differs
_cfg = RnsConfig(
    transport_path="/tmp/lmao_human_client_rns_state",
    tempdir_prefix="lmao_rns_",
)

# Export the same names as before so callers are unaffected
get_configdir = _cfg.get_configdir
get_config_dict = _cfg.get_config_dict
CONFIG_CONTENT = _cfg.CONFIG_CONTENT
_RNODE_PORT = _cfg._RNODE_PORT
