"""Tests for config modules — no RNode required.

Tests cover:
  * lmao_server.config: dict_to_ini(), get_configdir(), get_config_dict(),
    and resolve_rnode_port()
  * cardputer_client.config: structural validation (importable, expected keys)

All tests mock the filesystem and environment so they run without real hardware.

Run with::

    bazel test //tests:test_config --test_output=all
"""

import ast
import os
from unittest.mock import patch



# Import the config module (available when running under Bazel via deps)
try:
    from lmao_server import config
except ImportError:
    config = None

# Import shared config utilities directly (replacing the old _-prefixed aliases)
from lma_core.config_utils import dict_to_ini, resolve_rnode_port


class TestDictToINI:
    """Tests for dict_to_ini() — converts Python dicts to Reticulum INI format."""

    def test_simple_section(self):
        """Single section with one key-value pair."""
        sections = {"logging": {"loglevel": 4}}
        result = dict_to_ini(sections, {})
        assert result == "[logging]\nloglevel = 4\n"

    def test_multiple_sections(self):
        """Multiple top-level sections."""
        sections = {
            "logging": {"loglevel": 4},
            "transport": {"path": "/tmp/rns"},
        }
        result = dict_to_ini(sections, {})
        assert "[logging]\nloglevel = 4\n" in result
        assert "[transport]\npath = /tmp/rns\n" in result

    def test_simple_interface(self):
        """Single interface with one setting."""
        result = dict_to_ini({}, {"RNode LoRa": {"type": "RNodeInterface"}})
        assert result == "[[RNode LoRa]]\ntype = RNodeInterface\n"

    def test_multiple_interfaces(self):
        """Multiple interfaces."""
        interfaces = {
            "RNode LoRa": {"type": "RNodeInterface", "port": "/dev/ttyUSB0"},
            "WiFi": {"type": "AutoInterface", "enabled": True},
        }
        result = dict_to_ini({}, interfaces)
        assert "[[RNode LoRa]]\n" in result
        assert "type = RNodeInterface\n" in result
        assert "port = /dev/ttyUSB0\n" in result
        assert "[[WiFi]]\n" in result
        assert "type = AutoInterface\n" in result
        assert "enabled = yes\n" in result

    def test_boolean_true(self):
        """Boolean True becomes 'yes'."""
        sections = {"logging": {"debug": True}}
        result = dict_to_ini(sections, {})
        assert "debug = yes\n" in result

    def test_boolean_false(self):
        """Boolean False becomes 'no'."""
        sections = {"logging": {"debug": False}}
        result = dict_to_ini(sections, {})
        assert "debug = no\n" in result

    def test_empty_sections_and_interfaces(self):
        """Empty dicts produce a single trailing newline."""
        result = dict_to_ini({}, {})
        assert result == "\n"

    def test_mixed_sections_and_interfaces(self):
        """Sections appear before interfaces."""
        sections = {"logging": {"loglevel": 4}}
        interfaces = {"WiFi": {"type": "AutoInterface"}}
        result = dict_to_ini(sections, interfaces)
        # Sections must appear before interfaces
        assert result.index("[logging]") < result.index("[[WiFi]]")

    def test_trailing_newline(self):
        """Output always ends with a newline."""
        sections = {"logging": {"loglevel": 4}}
        result = dict_to_ini(sections, {})
        assert result.endswith("\n")


class TestGetConfigDir:
    """Tests for get_configdir() — creates temp config directory."""

    def test_returns_valid_path(self, tmp_path):
        """get_configdir() returns a path that is a directory."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path):
            configdir = config.get_configdir()
        assert configdir.startswith(str(tmp_path))
        assert os.path.isdir(configdir)

    def test_creates_config_file(self, tmp_path):
        """The returned directory contains a 'config' file."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path):
            result = config.get_configdir()
        config_file = os.path.join(result, "config")
        assert os.path.isfile(config_file), f"Expected config file at {config_file}"

    def test_config_content_is_ini_format(self, tmp_path):
        """The config file content matches dict_to_ini output."""
        configdir_path = str(tmp_path / "lmao_config")
        os.makedirs(configdir_path, exist_ok=True)
        with patch("tempfile.mkdtemp", return_value=configdir_path), \
             patch.object(config._cfg, "CONFIG_CONTENT", "[test]\nkey = value\n"):
            result = config.get_configdir()
        config_file = os.path.join(result, "config")
        with open(config_file) as f:
            content = f.read()
        assert "[test]\nkey = value\n" == content

    def test_prefix_is_lmao_rns(self):
        """The temp directory uses the 'lmao_rns_' prefix."""
        with patch("tempfile.mkdtemp") as mock_mkdtemp:
            mock_mkdtemp.return_value = "/tmp/lmao_rns_abc123"
            # get_configdir tries to write a file; mock open so it doesn't fail
            with patch("builtins.open"):
                config.get_configdir()
        mock_mkdtemp.assert_called_once_with(prefix="lmao_rns_")


class TestGetConfigDict:
    """Tests for get_config_dict() — returns config as dict."""

    def test_returns_expected_top_level_keys(self):
        """Config dict has 'interfaces', 'transport', and 'logging' keys."""
        result = config.get_config_dict()
        assert "interfaces" in result
        assert "transport" in result
        assert "logging" in result

    def test_rnode_interface_present(self):
        """RNode LoRa interface is in the dict."""
        result = config.get_config_dict()
        assert "RNode LoRa" in result["interfaces"]
        assert "WiFi" in result["interfaces"]

    def test_rnode_interface_has_port(self):
        """RNode LoRa interface includes port key."""
        result = config.get_config_dict()
        assert "port" in result["interfaces"]["RNode LoRa"]

    def test_transport_has_path(self):
        """Transport section includes path."""
        result = config.get_config_dict()
        assert "path" in result["transport"]

    def test_logging_has_loglevel(self):
        """Logging section includes loglevel."""
        result = config.get_config_dict()
        assert "loglevel" in result["logging"]

    def test_returns_deep_copied_dict(self):
        """Modifying the returned dict does not mutate module internals."""
        result1 = config.get_config_dict()
        result2 = config.get_config_dict()
        # Modify result1
        result1["interfaces"]["RNode LoRa"]["port"] = "/dev/FAKE"
        # result2 should be unchanged
        assert result2["interfaces"]["RNode LoRa"]["port"] != "/dev/FAKE"


class TestResolveRNodePort:
    """Tests for resolve_rnode_port() — port detection logic."""

    def test_env_var_overrides(self):
        """LMAO_RNODE_PORT env var takes priority."""
        with patch.dict(os.environ, {"LMAO_RNODE_PORT": "/dev/ttySpecial"}, clear=False):
            result = resolve_rnode_port()
        assert result == "/dev/ttySpecial"

    def test_auto_detect_first_match(self):
        """First existing port in common_ports list is returned."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=lambda p: p == "/dev/ttyUSB0"):
            result = resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_auto_detect_second_match(self):
        """Second port returned when first doesn't exist."""
        def fake_exists(p):
            return p == "/dev/ttyACM0"
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=fake_exists):
            result = resolve_rnode_port()
        assert result == "/dev/ttyACM0"

    def test_auto_detect_multiple_ports(self):
        """First existing port wins when multiple exist."""
        def fake_exists(p):
            return p in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1")
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", side_effect=fake_exists):
            result = resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_fallback_to_default(self):
        """When no port exists, fall back to /dev/ttyUSB0."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("os.path.exists", return_value=False):
            result = resolve_rnode_port()
        assert result == "/dev/ttyUSB0"

    def test_env_var_empty_string(self):
        """Empty LMAO_RNODE_PORT is treated as unset (falls through)."""
        with patch.dict(os.environ, {"LMAO_RNODE_PORT": ""}, clear=False):
            # Empty string is falsy, so should fall through to auto-detect
            with patch("os.path.exists", return_value=False):
                result = resolve_rnode_port()
            assert result == "/dev/ttyUSB0"


class TestCardputerConfigStructure:
    """Verify cardputer_client/config.py compiles and exports expected keys.

    A typo in config.py is only caught during hardware E2E tests currently.
    These unit tests catch import/structural errors immediately.
    """

    def test_config_module_compile(self):
        """config.py must be syntactically valid and compilable."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "cardputer_client", "config.py"
        )
        with open(config_path, encoding="utf-8") as f:
            code = f.read()
        # Must compile cleanly
        compile(code, config_path, "exec")

    def test_config_exports_dest_hash(self):
        """config.py must define DEST_HASH at module level."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "cardputer_client", "config.py"
        )
        with open(config_path, encoding="utf-8") as f:
            code = f.read()
        tree = ast.parse(code)
        top_level_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top_level_names.add(target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name)
        assert "DEST_HASH" in top_level_names, (
            "config.py does not define DEST_HASH"
        )

    def test_config_exports_expected_keys(self):
        """config.py must define all expected module-level names."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "cardputer_client", "config.py"
        )
        with open(config_path, encoding="utf-8") as f:
            code = f.read()
        tree = ast.parse(code)
        top_level_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top_level_names.add(target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name)
        expected = {"WIFI_SSID", "WIFI_PASS", "NODE_NAME", "DEBUG",
                     "DEST_HASH", "CONFIG"}
        missing = expected - top_level_names
        assert not missing, (
            f"config.py is missing expected names: {missing}"
        )

    def test_dest_hash_default_is_none(self):
        """DEST_HASH must default to None (no server target until configured)."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "cardputer_client", "config.py"
        )
        with open(config_path, encoding="utf-8") as f:
            code = f.read()
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name) and
                            target.id == "DEST_HASH"):
                        # The value should be None (ast.NameConstant or
                        # ast.Constant depending on Python version)
                        if isinstance(node.value, ast.Constant):
                            assert node.value.value is None, (
                                f"DEST_HASH should default to None, "
                                f"got {node.value.value!r}"
                            )
                        elif hasattr(ast, "NameConstant"):
                            assert isinstance(node.value, ast.NameConstant), (
                                f"Unexpected DEST_HASH value: {ast.dump(node.value)}"
                            )
                            assert node.value.value is None
                        return
        raise AssertionError("DEST_HASH assignment not found in config.py")


if __name__ == "__main__":
    import pytest as _pytest
    import sys as _sys
    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
