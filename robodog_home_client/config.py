"""Reticulum config for the RoboDog home client.

The home client deliberately uses ONLY the Reticulum TCP interface to the LMAO
server (``LMAO_SERVER_HOST``:``LMAO_SERVER_PORT``, default ``tp4:4246``).  No
RNode and no AutoInterface: multicast is unreliable on the home AP, and this
host-side client has no LoRa hardware.  ``RnsConfig(role="tcp-client")`` is the
single definition of that interface set (``lma_core/config_utils.py``).

The factory is built per call (not at import) so CLI overrides
(``--server-host``/``--server-port``) take effect.
"""

import os

from lma_core.config_utils import RnsConfig

DEFAULT_TRANSPORT_PATH = "/tmp/lmao_home_client_rns_state"


def make_config(server_host=None, server_port=None):
    """Build the TCP-only Reticulum config for the current target.

    ``LMAO_HOME_CLIENT_STATE_PATH`` makes the Reticulum state dir persistent
    (known destinations survive restarts); unset keeps a throwaway temp dir.
    """
    state_dir = os.environ.get("LMAO_HOME_CLIENT_STATE_PATH")
    return RnsConfig(
        transport_path=state_dir or DEFAULT_TRANSPORT_PATH,
        tempdir_prefix="lmao_home_rns_",
        persist_state=bool(state_dir),
        role="tcp-client",
        tcp_host=server_host or os.environ.get("LMAO_SERVER_HOST"),
        tcp_port=server_port or os.environ.get("LMAO_SERVER_PORT"),
    )


def get_configdir(server_host=None, server_port=None):
    return make_config(server_host, server_port).get_configdir()


def get_config_dict(server_host=None, server_port=None):
    return make_config(server_host, server_port).get_config_dict()


def get_config_content(server_host=None, server_port=None):
    return make_config(server_host, server_port).CONFIG_CONTENT
