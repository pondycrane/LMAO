"""Tests for human_client config — no RNode required.

Tests for human_client/config.py functions using the shared
lma_core.config_utils module. Covers dict_to_ini, get_configdir,
get_config_dict, and resolve_rnode_port.

Run with::

    bazel test //tests:test_human_client_config --test_output=all
"""

import os
import tempfile
from unittest.mock import patch

import pytest


class TestDictToINI:
    """Tests for _dict_to_ini() — converts Python dicts to INI format."""

    @pytest.fixture
    def config_module(self):
        """Import the human_client config module."""
        from human_client import config
        return config

    def test_simple_section(self, config_module):
        """Single section with one key-value pair."""
        sections = {"logging": {"loglevel": 4}}
        result = config_module._dict_to_ini(sections, {})
        assert result == "[logging]\nloglevel = 4\n"

    def test_multiple_sections(self, config_module):
        """Multiple top-level sections."""
        sections = {
            "logging": {"loglevel": 4},
            "transport": {"path": "/tmp/rns"},
        }
        result = config_module._dict_to_ini(sections, {})
        assert "[logging]\nloglevel = 4\n" in result
        assert "[transport]\npath = /tmp/rns\n" in result

    def test_simple_interface(self, config_module):
        """Single interface with one setting."""
        result = config_module._dict_to_ini({}, {"RNode LoRa": {"type": "RNodeInterface"}})
        assert result == "[[RNode LoRa]]\ntype = RNodeInterface\n"

    def test_multiple_interfaces(self, config_module):
        """Multiple interfaces."""
        interfaces = {
            "RNode LoRa": {"type": "RNodeInterface", "port": "/dev/ttyUSB0"},
            "WiFi": {"type": "AutoInterface", "enabled": True},
        }
        result = config_module._dict_to_ini({}, interfaces)
        assert "[[RNode LoRa]]\n" in result
        assert "type = RNodeInterface\n" in result
        assert "port = /dev/ttyUSB0\n" in result
        assert "[[WiFi]]\n" in result
        assert "type = AutoInterface\n" in result
        assert "enabled = yes\n" in result

    def test_boolean_true(self, config_module):
        """Boolean True becomes 'yes'."""
        sections = {"logging": {"debug": True}}
        result = config_module._dict_to_ini(sections, {})
        assert "debug = yes\n" in result

    def test_boolean_false(self, config_module):
        """Boolean False becomes 'no'."""
        sections = {"logging": {"debug": False}}
        result = config_module._dict_to_ini(sections, {})
        assert "debug = no\n" in result

    def test_empty_sections_and_interfaces(self, config_module):
        """Empty dicts produce a single trailing newline."""
        result = config_module._dict_to_ini({}, {})
        assert result == "\n"

    def test_mixed_sections_and_interfaces(self, config_module):
        """Sections appear before interfaces."""
        sections = {"logging": {"loglevel": 4}}
        interfaces = {"WiFi": {"type": "AutoInterface"}}
        result = config_module._dict_to_ini(sections, interfaces)
        # Sections must appear before interfaces
        assert result.index("[logging]") < result.index("[[WiFi]]")

    def test_trailing_newline(self, config_module):
        """Output always ends with a newline."""
        sections = {"logging": {"loglevel": 4}}
        result = config_module._dict_to_ini(sections, {})
        assert result.endswith("\n")


class TestGetConfigDir:
    """Tests for get_configdir() — creates temp config directory."""

    @pytest.fixture
    def config_module(self):
        """Import the human_client config module."""
        from human_client import config
        return config

    def test_returns_valid_path(self, config_module, tmp_path):
        """get_configdir() returns a path that is a directory."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path):
            configdir = config_module.get_configdir()
        assert configdir.startswith(str(tmp_path))
        assert os.path.isdir(configdir)

    def test_creates_config_file(self, config_module, tmp_path):
        """The returned directory contains a 'config' file."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path):
            result = config_module.get_configdir()
        config_file = os.path.join(result, "config")
        assert os.path.isfile(config_file), f"Expected config file at {config_file}"

    def test_config_content_is_ini_format(self, config_module, tmp_path):
        """The config file content matches _dict_to_ini output."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path), \
             patch.object(config_module, "CONFIG_CONTENT", "[test]\nkey = value\n"):
            result = config_module.get_configdir()
        config_file = os.path.join(result, "config")
        with open(config_file) as f:
            content = f.read()
        assert "[test]\nkey = value\n" == content

    def test_prefix_is_lmao_rns(self, config_module):
        """The temp directory uses the 'lmao_rns_' prefix."""
        with patch("tempfile.mkdtemp") as mock_mkdtemp:
            mock_mkdtemp.return_value = "/tmp/lmao_rns_abc123"
            with patch("builtins.open"):
                config_module.get_configdir()
        mock_mkdtemp.assert_called_once_with(prefix="lmao_rns_")

    def test_transport_path_is_human_client(self, config_module, tmp_path):
        """Verify the config file contains the human_client transport path."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path):
            result = config_module.get_configdir()
        config_file = os.path.join(result, "config")
        with open(config_file) as f:
            content = f.read()
        assert "lmao_human_client_rns_state" in content, (
            "Config should contain human_client-specific transport path"
        )


class TestGetConfigDict:
    """Tests for get_config_dict() — returns config as dict."""

    @pytest.fixture
    def config_module(self):
        """Import the human_client config module."""
        from human_client import config
        return config

    def test_returns_expected_top_level_keys(self, config_module):
        """Config dict has 'interfaces', 'transport', and 'logging' keys."""
        result = config_module.get_config_dict()
        assert "interfaces" in result
        assert "transport" in result
        assert "logging" in result

    def test_rnode_interface_present(self, config_module):
        """RNode LoRa interface is in the dict."""
        result = config_module.get_config_dict()
        assert "RNode LoRa" in result["interfaces"]
        assert "WiFi" in result["interfaces"]

    def test_rnode_interface_has_port(self, config_module):
        """RNode LoRa interface includes port key."""
        result = config_module.get_config_dict()
        assert "port" in result["interfaces"]["RNode LoRa"]

    def test_transport_has_path(self, config_module):
        """Transport section includes path."""
        result = config_module.get_config_dict()
        assert "path" in result["transport"]

    def test_logging_has_loglevel(self, config_module):
        """Logging section includes loglevel."""
        result = config_module.get_config_dict()
        assert "loglevel" in result["logging"]

    def test_returns_deep_copied_dict(self, config_module):
        """Modifying the returned dict does not mutate module internals."""
        result1 = config_module.get_config_dict()
        result2 = config_module.get_config_dict()
        # Modify result1
        result1["interfaces"]["RNode LoRa"]["port"] = "/dev/FAKE"
        # result2 should be unchanged
        assert result2["interfaces"]["RNode LoRa"]["port"] != "/dev/FAKE"

    def test_transport_path_is_human_client(self, config_module):
        """Transport path should be the human_client-specific path."""
        result = config_module.get_config_dict()
        assert result["transport"]["path"] == "/tmp/lmao_human_client_rns_state"


class TestResolveRNodePort:
    """Tests for _resolve_rnode_port() — port detection logic."""

    @pytest.fixture
    def config_module(self):
        """Import the human_client config module."""
        from human_client import config
        return config

    def test_env_var_overrides(self, config_module):
        """LMAO_RNODE_PORT env var takes priority."""
        with patch.dict(os.environ, {"LMAO_RNODE_PORT": "/dev/ttySpecial"}, clear=False):
            result = config_module._resolve_rnode_port()
        assert result == "/dev/ttySpecial"

    def test_auto_detect_first_match(self, config_module):
        """First existing port in common_ports list is returned."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=lambda p: p == "/dev/ttyUSB0"):
            result = config_module._resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_auto_detect_second_match(self, config_module):
        """Second port returned when first doesn't exist."""
        def fake_exists(p):
            return p == "/dev/ttyACM0"
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=fake_exists):
            result = config_module._resolve_rnode_port()
        assert result == "/dev/ttyACM0"

    def test_auto_detect_multiple_ports(self, config_module):
        """First existing port wins when multiple exist."""
        def fake_exists(p):
            return p in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1")
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=fake_exists):
            result = config_module._resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_fallback_to_default(self, config_module):
        """When no port exists, fall back to /dev/ttyUSB0."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", return_value=False):
            result = config_module._resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_env_var_empty_string(self, config_module):
        """Empty LMAO_RNODE_PORT is treated as unset (falls through)."""
        with patch.dict(os.environ, {"LMAO_RNODE_PORT": ""}, clear=False):
            # Empty string is falsy, so should fall through to auto-detect
            with patch("os.path.exists", return_value=False):
                result = config_module._resolve_rnode_port()
            assert result == "/dev/ttyUSB0"


if __name__ == "__main__":
    import pytest as _pytest
    import sys as _sys
    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
