"""Shared Reticulum config utilities.

Functions used by both the server and human client to resolve RNode ports
and generate Reticulum INI-format configurations.  Also provides a config
factory (``RnsConfig``) so that the nearly-identical server and client
config modules only differ in their transport path.

Module-level functions:
    - resolve_rnode_port() — env var, auto-detect, or default RNode port
    - dict_to_ini() — Python dict to Reticulum INI format

Factory class:
    - RnsConfig — produces a namespace object with get_configdir(),
      get_config_dict(), CONFIG_CONTENT, and _RNODE_PORT.
"""

import os
import tempfile


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
    def _render(value):
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (list, tuple)):
            # configobj as_list() parses comma-separated values
            return ", ".join(str(v) for v in value)
        return value

    lines = []
    for section, settings in sections.items():
        lines.append(f"[{section}]")
        for key, value in settings.items():
            lines.append(f"{key} = {_render(value)}")
    lines.append("[interfaces]")
    for name, settings in interfaces.items():
        lines.append(f"[[{name}]]")
        for key, value in settings.items():
            lines.append(f"{key} = {_render(value)}")
    return "\n".join(lines) + "\n"


class RnsConfig:
    """Factory for Reticulum config namespaces.

    Encapsulates the common config-building logic shared by
    lmao_server/config.py and human_client/config.py.  Each
    component creates one instance with its own transport path
    and exports the instance attributes as module-level names.

    Usage:
        _cfg = RnsConfig(transport_path="/tmp/my_state")
        get_configdir = _cfg.get_configdir
        CONFIG_CONTENT = _cfg.CONFIG_CONTENT
    """

    def __init__(self, transport_path, tempdir_prefix="lmao_rns_", persist_state=False):
        self._transport_path = transport_path
        self._tempdir_prefix = tempdir_prefix
        self._persist_state = persist_state
        self._rnode_port = resolve_rnode_port()

        self._sections = {
            "logging": {"loglevel": 4},
            "transport": {"path": transport_path},
        }
        # AutoInterface normally binds every multicast-capable interface.
        # On K8s nodes that includes flannel/veth CNI interfaces, producing
        # "No multicast echoes" errors and leaking announces into the pod
        # network.  LMAO_AUTOIFACE_DEVICES (comma-separated) pins it to the
        # physical LAN interface(s) — e.g. "wlan0" on the RK1 nodes.
        wifi_interface = {
            "type": "AutoInterface",
            "enabled": True,
        }
        autoiface_devices = os.environ.get("LMAO_AUTOIFACE_DEVICES", "").strip()
        if autoiface_devices:
            wifi_interface["devices"] = [
                d.strip() for d in autoiface_devices.split(",") if d.strip()
            ]

        self._interfaces = {
            "RNode LoRa": {
                "type": "RNodeInterface",
                "port": self._rnode_port,
                "frequency": 868000000,
                "bandwidth": 125000,
                "spreadingfactor": 7,
                "codingrate": 5,
                "txpower": 17,
                "enabled": True,
            },
            "WiFi": wifi_interface,
        }
        self.CONFIG_CONTENT = dict_to_ini(self._sections, self._interfaces)

    def get_configdir(self):
        """Create a config directory for Reticulum.

        When ``persist_state`` is enabled (via ``LMAO_RNS_TRANSPORT_PATH``),
        the transport path itself is used as the Reticulum configdir, so
        RNS state (known destinations/identities under ``<dir>/storage``)
        survives restarts — without this, every server restart loses peer
        identities until their next announce and replies fail with "no
        source destination" (issue #93).  Callers must NOT delete the
        returned directory in that case.

        Otherwise a temporary directory is created; the caller is
        responsible for its cleanup.

        Returns the path to the directory.
        """
        if self._persist_state:
            configdir = self._transport_path
            os.makedirs(configdir, exist_ok=True)
        else:
            configdir = tempfile.mkdtemp(prefix=self._tempdir_prefix)
        config_path = os.path.join(configdir, "config")
        with open(config_path, "w") as f:
            f.write(self.CONFIG_CONTENT)
        return configdir

    @property
    def persist_state(self):
        """Whether the configdir (and RNS state) should persist on disk."""
        return self._persist_state

    def get_config_dict(self):
        """Return the config as a dict for introspection."""
        return {
            "interfaces": {
                "RNode LoRa": dict(self._interfaces["RNode LoRa"]),
                "WiFi": dict(self._interfaces["WiFi"]),
            },
            "transport": dict(self._sections["transport"]),
            "logging": dict(self._sections["logging"]),
        }

    @property
    def _RNODE_PORT(self):
        return self._rnode_port
