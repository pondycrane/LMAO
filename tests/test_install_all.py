"""Unit tests for tools/install_all.py — no hardware required.

Covers argument parsing, device result tracking, summary output, and
integration of the main pipeline with mocked hardware detection.

Run with::

    bazel test //tests:test_install_all --test_output=all
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

# Import the install_all module (available under Bazel via deps).
try:
    from tools import install_all, install_services
except ImportError:
    install_all = None  # type: ignore[assignment]
    install_services = None  # type: ignore[assignment]


# ── helpers ─────────────────────────────────────────────────────────


def _patch_imports():
    """Patch pyserial and install_all hardware-detection functions.

    Returns a dict of patches keyed by target string so callers can
    start/stop them independently.
    """
    patches = {
        "serial": patch("tools.install_all.serial", MagicMock()),
        "serial.tools": patch("tools.install_all.serial.tools", MagicMock()),
        "serial.tools.list_ports": patch("tools.install_all.serial.tools.list_ports", MagicMock()),
        "find_cardputer_port": patch.object(install_all, "find_cardputer_port", return_value=None),
        "find_rnode_port": patch.object(install_all, "find_rnode_port", return_value=None),
        "check_rnode_firmware": patch.object(
            install_all, "check_rnode_firmware", return_value=False
        ),
        "flash_rnode_firmware": patch.object(
            install_all, "flash_rnode_firmware", return_value=(True, "OK")
        ),
        "find_client_root": patch.object(
            install_all, "find_client_root", return_value="/fake/client_root"
        ),
        "enter_raw_repl": patch.object(
            install_all, "enter_raw_repl", return_value=True
        ),
    }
    return patches


def _start_patches(patches_dict):
    """Start all patches in *patches_dict* and return (mocks, patches)."""
    mocks = {}
    for key, p in patches_dict.items():
        mocks[key] = p.start()
    return mocks, patches_dict


def _stop_patches(patches_dict):
    """Stop all patches in *patches_dict*."""
    for p in patches_dict.values():
        p.stop()


# ── DeviceResult ─────────────────────────────────────────────────────


class TestDeviceResult:
    """Tests for the DeviceResult status tracker."""

    def test_default_status_is_skip(self):
        """New result should start as SKIP with no detail."""
        r = install_all.DeviceResult("Cardputer")
        assert r.name == "Cardputer"
        assert r.status == "SKIP"
        assert r.detail == ""

    def test_ok_sets_status(self):
        """ok() should set status to OK and record detail."""
        r = install_all.DeviceResult("RNode")
        r.ok("Flashed successfully")
        assert r.status == "OK"
        assert r.detail == "Flashed successfully"

    def test_fail_sets_status(self):
        """fail() should set status to FAIL and record detail."""
        r = install_all.DeviceResult("Cardputer")
        r.fail("Serial port error")
        assert r.status == "FAIL"
        assert r.detail == "Serial port error"

    def test_skip_sets_status(self):
        """skip() should set status to SKIP and record reason."""
        r = install_all.DeviceResult("RNode")
        r.skip("Not detected")
        assert r.status == "SKIP"
        assert r.detail == "Not detected"

    def test_multiple_calls_last_wins(self):
        """Last status call wins."""
        r = install_all.DeviceResult("Cardputer")
        r.ok("Good")
        r.fail("Actually bad")
        assert r.status == "FAIL"
        assert r.detail == "Actually bad"


# ── CLI argument parsing ─────────────────────────────────────────────


class TestParseArgs:
    """Tests for _parse_args()."""

    def test_defaults(self):
        """All arguments should have sensible defaults."""
        args = install_all._parse_args([])
        assert args.cardputer_port is None
        assert args.rnode_port is None
        assert args.skip_cardputer is False
        assert args.skip_rnode is False
        assert args.client_root is None

    def test_cardputer_port_flag(self):
        """--cardputer-port should be captured."""
        args = install_all._parse_args(["--cardputer-port", "/dev/ttyACM0"])
        assert args.cardputer_port == "/dev/ttyACM0"

    def test_rnode_port_flag(self):
        """--rnode-port should be captured."""
        args = install_all._parse_args(["--rnode-port", "/dev/ttyUSB0"])
        assert args.rnode_port == "/dev/ttyUSB0"

    def test_skip_cardputer_flag(self):
        """--skip-cardputer flag should be True."""
        args = install_all._parse_args(["--skip-cardputer"])
        assert args.skip_cardputer is True

    def test_skip_rnode_flag(self):
        """--skip-rnode flag should be True."""
        args = install_all._parse_args(["--skip-rnode"])
        assert args.skip_rnode is True

    def test_client_root_flag(self):
        """--client-root should be captured."""
        args = install_all._parse_args(["--client-root", "/custom/path"])
        assert args.client_root == "/custom/path"

    def test_combined_flags(self):
        """Multiple flags can be combined."""
        args = install_all._parse_args(["--cardputer-port", "/dev/ttyACM0", "--skip-rnode"])
        assert args.cardputer_port == "/dev/ttyACM0"
        assert args.skip_rnode is True
        assert args.skip_cardputer is False

    # ── include-services flags ──

    def test_include_services_default_false(self):
        """--include-services defaults to False."""
        args = install_all._parse_args([])
        assert args.include_services is False

    def test_include_services_flag(self):
        """--include-services sets the flag to True."""
        args = install_all._parse_args(["--include-services"])
        assert args.include_services is True

    def test_skip_server_flag(self):
        """--skip-server sets the flag to True."""
        args = install_all._parse_args(["--include-services", "--skip-server"])
        assert args.skip_server is True

    def test_skip_k8s_flag(self):
        """--skip-k8s sets the flag to True."""
        args = install_all._parse_args(["--include-services", "--skip-k8s"])
        assert args.skip_k8s is True

    def test_skip_flags_noop_without_include_services(self):
        """--skip-server and --skip-k8s can be set without --include-services."""
        args = install_all._parse_args(["--skip-server", "--skip-k8s"])
        assert args.skip_server is True
        assert args.include_services is False


# ── Summary output ───────────────────────────────────────────────────


class TestPrintSummary:
    """Tests for _print_summary()."""

    def _make_result(self, name, status, detail=""):
        r = install_all.DeviceResult(name)
        if status == "OK":
            r.ok(detail)
        elif status == "FAIL":
            r.fail(detail)
        elif status == "SKIP":
            r.skip(detail)
        return r

    def test_all_ok_exits_0(self, capsys):
        """When all results are OK, should exit 0."""
        results = [
            self._make_result("Cardputer", "OK", "Flashed 10 files"),
            self._make_result("RNode", "OK", "Already installed"),
        ]
        with pytest.raises(SystemExit) as exc_info:
            install_all._print_summary(results)
        assert exc_info.value.code == 0
        captured = capsys.readouterr().out
        assert "[OK]" in captured
        assert "Cardputer" in captured
        assert "RNode" in captured
        assert "successfully" in captured.lower()

    def test_any_fail_exits_1(self, capsys):
        """When any result is FAIL, should exit 1."""
        results = [
            self._make_result("Cardputer", "OK", ""),
            self._make_result("RNode", "FAIL", "Flash error"),
        ]
        with pytest.raises(SystemExit) as exc_info:
            install_all._print_summary(results)
        assert exc_info.value.code == 1
        captured = capsys.readouterr().out
        assert "[OK]" in captured
        assert "[FAIL]" in captured
        assert "FAILED" in captured

    def test_all_skip_exits_0(self, capsys):
        """When all results are SKIP, should exit 0."""
        results = [
            self._make_result("Cardputer", "SKIP", "Not detected"),
            self._make_result("RNode", "SKIP", "Not detected"),
        ]
        with pytest.raises(SystemExit) as exc_info:
            install_all._print_summary(results)
        assert exc_info.value.code == 0
        captured = capsys.readouterr().out
        assert "[SKIP]" in captured

    def test_empty_list_exits_0(self, capsys):
        """Empty results list should exit cleanly."""
        with pytest.raises(SystemExit) as exc_info:
            install_all._print_summary([])
        assert exc_info.value.code == 0

    def test_output_includes_detail(self, capsys):
        """Detail text should be present in output when provided."""
        results = [
            self._make_result("Cardputer", "OK", "Flashed 42 file(s) to Cardputer"),
        ]
        with pytest.raises(SystemExit):
            install_all._print_summary(results)
        captured = capsys.readouterr().out
        assert "Flashed 42 file(s) to Cardputer" in captured

    def test_label_width_accommodates_longest_name(self, capsys):
        """Output column widths should accommodate the longest device name."""
        results = [
            self._make_result("Cardputer", "OK"),
            self._make_result("RNode (Heltec)", "OK"),
        ]
        with pytest.raises(SystemExit):
            install_all._print_summary(results)
        captured = capsys.readouterr().out
        assert "RNode (Heltec)" in captured
        assert "Cardputer" in captured


# ── Flash cardputer client unit tests ───────────────────────────────


class TestFlashCardputerClient:
    """Direct unit tests for _flash_cardputer_client()."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        """Mock serial and all flash helpers."""
        self.mock_serial_class = MagicMock()
        self.mock_ser = MagicMock()
        self.mock_serial_class.return_value = self.mock_ser

        patches = {
            "time.sleep": patch("tools.install_all.time.sleep", return_value=None),
            "enter_raw_repl": patch.object(install_all, "enter_raw_repl", return_value=True),
            "verify_device": patch.object(
                install_all, "verify_device", return_value=(True, "ESP32 detected")
            ),
            "upload_file": patch.object(install_all, "upload_file", return_value=True),
            "exit_raw_repl": patch.object(install_all, "exit_raw_repl", return_value=None),
            "verify_files_exist": patch.object(
                install_all, "verify_files_exist", return_value=None
            ),
            "auto_discover_lib_files": patch.object(
                install_all, "auto_discover_lib_files", return_value=[]
            ),
            "os.path.getsize": patch("os.path.getsize", return_value=100),
        }
        self._all_patches = patches
        self.mocks = {}
        for key, p in patches.items():
            self.mocks[key] = p.start()
        # serial.Serial needs special handling (returns self.mock_ser)
        self._serial_patch = patch("tools.install_all.serial.Serial", self.mock_serial_class)
        self._serial_patch.start()
        # FILES_TO_UPLOAD is a list, not compatible with patch.object dict stop
        self._saved_files = install_all.FILES_TO_UPLOAD
        install_all.FILES_TO_UPLOAD = ["main.py", "config.py"]
        yield
        _stop_patches(self._all_patches)
        self._serial_patch.stop()
        install_all.FILES_TO_UPLOAD = self._saved_files

    def _make_result(self):
        return install_all.DeviceResult("Cardputer")

    # ── success path ──

    def test_successful_flash_sets_ok(self):
        """Successful flash should set result to OK."""
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "OK"
        assert "file(s)" in result.detail

    def test_successful_flash_closes_port(self):
        """Port should always be closed after flash."""
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        self.mock_ser.close.assert_called_once()

    # ── serial port open failure ──

    def test_cannot_open_port_sets_fail(self):
        """Serial port open failure should set result to FAIL."""
        import serial as pyserial

        self.mock_serial_class.side_effect = pyserial.SerialException("denied")
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "Cannot open serial port" in result.detail
        self.mock_ser.close.assert_not_called()

    # ── raw REPL failure ──

    def test_raw_repl_failure_sets_fail_and_closes_port(self):
        """Raw REPL entry failure should set FAIL and still close port."""
        self.mocks["enter_raw_repl"].return_value = False
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "raw REPL" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── missing source file ──

    def test_missing_source_file_sets_fail(self):
        """Missing source file should set FAIL and close port."""
        self.mocks["verify_files_exist"].side_effect = FileNotFoundError("main.py not found")
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "Missing source file" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── partial upload failure ──

    def test_partial_upload_sets_fail(self):
        """Partial upload failure should set FAIL."""
        # First file succeeds, second fails
        self.mocks["upload_file"].side_effect = [True, False]
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "failed to upload" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── serial exception during operations ──

    def test_serial_exception_during_operations_sets_fail(self):
        """Serial exception mid-operation should set FAIL and close port."""
        import serial as pyserial

        self.mocks["enter_raw_repl"].side_effect = pyserial.SerialException("disconnected")
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "Serial error" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── KeyboardInterrupt ──

    def test_keyboard_interrupt_sets_fail_and_closes_port(self):
        """Ctrl+C during flash should set FAIL and close port."""
        self.mocks["upload_file"].side_effect = KeyboardInterrupt()
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "Aborted by user" in result.detail
        # Port should still be closed
        self.mock_ser.close.assert_called_once()

    # ── device verification warning (non-ESP32) ──

    def test_device_verification_warning_continues_flash(self):
        """Device verification warning should not stop flash."""
        self.mocks["verify_device"].return_value = (False, "Unknown device")
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        # Should still succeed because verification is a warning, not a blocker
        assert result.status == "OK"
        self.mock_ser.close.assert_called_once()


# ── Main pipeline integration ────────────────────────────────────────


class TestMainPipeline:
    """Integration tests for main() with mocked hardware detection."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        """Mock out all hardware-dependent functions."""
        patches = {
            "serial": patch("tools.install_all.serial", MagicMock()),
            "serial.tools": patch("tools.install_all.serial.tools", MagicMock()),
            "serial.tools.list_ports": patch(
                "tools.install_all.serial.tools.list_ports", MagicMock()
            ),
            "find_cardputer_port": patch.object(
                install_all, "find_cardputer_port", return_value=None
            ),
            "find_rnode_port": patch.object(install_all, "find_rnode_port", return_value=None),
            "check_rnode_firmware": patch.object(
                install_all, "check_rnode_firmware", return_value=False
            ),
            "flash_rnode_firmware": patch.object(
                install_all, "flash_rnode_firmware", return_value=(True, "OK")
            ),
            "find_client_root": patch.object(
                install_all, "find_client_root", return_value="/fake/client_root"
            ),
            "enter_raw_repl": patch.object(install_all, "enter_raw_repl", return_value=True),
            "verify_device": patch.object(
                install_all, "verify_device", return_value=(True, "ESP32 detected")
            ),
            "upload_file": patch.object(install_all, "upload_file", return_value=True),
            "exit_raw_repl": patch.object(install_all, "exit_raw_repl", return_value=None),
            "verify_files_exist": patch.object(
                install_all, "verify_files_exist", return_value=None
            ),
            "auto_discover_lib_files": patch.object(
                install_all, "auto_discover_lib_files", return_value=[]
            ),
        }
        self.mocks, self._patches = _start_patches(patches)
        # Patch FILES_TO_UPLOAD (a list, not compatible with patch.object dict)
        self._saved_files = install_all.FILES_TO_UPLOAD
        install_all.FILES_TO_UPLOAD = ["main.py", "config.py"]
        self._getsizep = patch("os.path.getsize", return_value=100)
        self._getsizep.start()
        # Also mock serial.Serial for _flash_cardputer_client
        self._serial_patch = patch("tools.install_all.serial.Serial", MagicMock())
        self._serial_patch.start()
        yield
        _stop_patches(self._patches)
        install_all.FILES_TO_UPLOAD = self._saved_files
        self._getsizep.stop()
        self._serial_patch.stop()

    def test_cardputer_detected_and_flash_succeeds_exits_0(self, capsys):
        """Cardputer detected + flash succeeds → exit 0 with OK summary."""
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        captured = capsys.readouterr().out
        assert "[OK]" in captured
        assert "Cardputer" in captured

    def test_cardputer_detected_and_flash_fails_exits_1(self, capsys):
        """Cardputer detected + flash fails → exit 1."""
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        self.mocks["upload_file"].return_value = False
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 1
        captured = capsys.readouterr().out
        assert "[FAIL]" in captured

    def test_both_devices_detected_both_processed(self):
        """Both Cardputer and RNode detected → both processed."""
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["check_rnode_firmware"].return_value = True
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        self.mocks["find_cardputer_port"].assert_called_once()
        self.mocks["find_rnode_port"].assert_called_once()

    def test_cardputer_port_override_bypasses_auto_detection(self):
        """--cardputer-port override bypasses auto-detection."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--cardputer-port", "/dev/customACM0"])
        assert exc_info.value.code == 0
        # find_cardputer_port should have been called with the explicit port
        self.mocks["find_cardputer_port"].assert_called_once_with("/dev/customACM0")

    def test_client_root_override_passed_through(self):
        """--client-root override is passed through to flash function."""
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--client-root", "/custom/root"])
        assert exc_info.value.code == 0
        self.mocks["find_client_root"].assert_not_called()


class TestMainSkipFlags:
    """Test main() with --skip-cardputer and --skip-rnode flags."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        """Mock out all hardware-dependent functions."""
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        yield
        _stop_patches(self._patches)

    def test_skip_both_devices_exits_0(self):
        """When both devices are skipped, should exit 0 with no work."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--skip-cardputer", "--skip-rnode"])
        assert exc_info.value.code == 0

    def test_skip_cardputer_only(self):
        """When Cardputer is skipped, RNode should still be processed."""
        # Mock find_rnode_port to return a port
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--skip-cardputer"])
        assert exc_info.value.code == 0
        # RNode detection should have been called
        self.mocks["find_rnode_port"].assert_called_once()
        # Cardputer detection should NOT have been called
        self.mocks["find_cardputer_port"].assert_not_called()

    def test_skip_rnode_only(self):
        """When RNode is skipped, Cardputer should still be processed."""
        # Mock find_cardputer_port to return a port
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--skip-rnode"])
        assert exc_info.value.code == 0
        # Cardputer detection should have been called
        self.mocks["find_cardputer_port"].assert_called_once()
        # RNode detection should NOT have been called
        self.mocks["find_rnode_port"].assert_not_called()


class TestMainNoHardware:
    """Test main() when no hardware is connected."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        # Both ports return None (no hardware detected)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_no_hardware_exits_0(self):
        """When nothing is detected, main should exit 0 gracefully."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0

    def test_no_hardware_prints_skip(self, capsys):
        """When nothing is detected, output should show SKIP for both."""
        with pytest.raises(SystemExit):
            install_all.main([])
        captured = capsys.readouterr().out
        assert "[SKIP]" in captured
        assert "not detected" in captured.lower()


class TestMainRNodeDetected:
    """Test main() when only RNode is detected."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["check_rnode_firmware"].return_value = True  # already RNode
        yield
        _stop_patches(self._patches)

    def test_rnode_already_firmware_exits_0(self):
        """When RNode already has firmware, should exit 0 without flashing."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        # Should check firmware but NOT flash
        self.mocks["check_rnode_firmware"].assert_called_once_with("/dev/ttyUSB0")
        self.mocks["flash_rnode_firmware"].assert_not_called()

    def test_rnode_needs_flashing(self):
        """When RNode lacks firmware, it should be flashed."""
        self.mocks["check_rnode_firmware"].return_value = False
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        self.mocks["flash_rnode_firmware"].assert_called_once()


class TestMainRNodeFlashFails:
    """Test main() when RNode flashing fails."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["check_rnode_firmware"].return_value = False
        self.mocks["flash_rnode_firmware"].return_value = (False, "Flash error")
        yield
        _stop_patches(self._patches)

    def test_flash_failure_exits_1(self):
        """When flashing fails, should exit 1."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 1


class TestMainClientRootNotFound:
    """Test main() when client_root cannot be found."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        self.mocks["find_rnode_port"].return_value = None
        # Make find_client_root return None (not found)
        self.mocks["find_client_root"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_missing_client_root_reports_fail(self):
        """When client_root is None, Cardputer should be marked FAIL."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 1


class TestMainRNodePortOverride:
    """Test main() with --rnode-port override."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None  # won't be called
        self.mocks["check_rnode_firmware"].return_value = True
        yield
        _stop_patches(self._patches)

    def test_rnode_port_override_uses_specified_port(self):
        """When --rnode-port is given, it should be used directly."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--rnode-port", "/dev/customUSB0"])
        assert exc_info.value.code == 0
        # find_rnode_port should NOT be called (explicit port given)
        self.mocks["find_rnode_port"].assert_not_called()
        # check_rnode_firmware should be called with the explicit port
        self.mocks["check_rnode_firmware"].assert_called_once_with("/dev/customUSB0")


# ── Main pipeline — include-services integration ────────────────────


class TestMainWithoutServices:
    """Test main() without --include-services (back-compat)."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_services_get_skip_when_flag_not_set(self, capsys):
        """Pi Server and K8s Services should show SKIP when --include-services not set."""
        with pytest.raises(SystemExit):
            install_all.main([])
        captured = capsys.readouterr().out
        assert "Pi Server" in captured
        assert "K8s Services" in captured
        assert "--include-services not set" in captured

    def test_summary_has_five_rows_when_nothing_detected(self, capsys):
        """Summary should show all five rows even when nothing is connected."""
        with pytest.raises(SystemExit):
            install_all.main([])
        captured = capsys.readouterr().out
        assert captured.count("[SKIP]") == 5


class TestMainWithServicesSkipped:
    """Test main() with --include-services but both services skipped."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        # Also mock the service install functions
        patches["install_pi_server"] = patch.object(install_all, "install_pi_server")
        patches["install_k8s_services"] = patch.object(install_all, "install_k8s_services")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_skip_flags_prevent_service_calls(self):
        """--skip-server and --skip-k8s prevent service install calls."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-server", "--skip-k8s"])
        self.mocks["install_pi_server"].assert_not_called()
        self.mocks["install_k8s_services"].assert_not_called()

    def test_skip_flags_show_in_summary(self, capsys):
        """Skipped services should show in summary with skip reason."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-server", "--skip-k8s"])
        captured = capsys.readouterr().out
        assert "--skip-server" in captured
        assert "--skip-k8s" in captured


class TestMainWithServices:
    """Test main() with --include-services (services called)."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        # Mock the service install functions
        patches["install_pi_server"] = patch.object(install_all, "install_pi_server")
        patches["install_k8s_services"] = patch.object(install_all, "install_k8s_services")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_include_services_calls_install_functions(self):
        """--include-services should call both install functions."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        self.mocks["install_pi_server"].assert_called_once()
        self.mocks["install_k8s_services"].assert_called_once()

    def test_only_pi_server_called_when_k8s_skipped(self):
        """--skip-k8s should prevent k8s install but allow Pi server."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-k8s"])
        self.mocks["install_pi_server"].assert_called_once()
        self.mocks["install_k8s_services"].assert_not_called()

    def test_only_k8s_called_when_server_skipped(self):
        """--skip-server should prevent Pi server install but allow K8s."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-server"])
        self.mocks["install_pi_server"].assert_not_called()
        self.mocks["install_k8s_services"].assert_called_once()

    def test_service_results_in_summary(self, capsys):
        """Pi Server and K8s Services should appear in summary."""
        # The mocked install functions don't touch the DeviceResult, so
        # they'll remain at default SKIP unless we simulate OK.
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        captured = capsys.readouterr().out
        assert "Pi Server" in captured
        assert "K8s Services" in captured


# ── Unit tests for install_services.py ─────────────────────────────


class TestInstallRNodeFirmware:
    """Direct unit tests for _install_rnode_firmware()."""

    def _make_result(self):
        return install_all.DeviceResult("RNode (Heltec)")

    def test_already_rnode_returns_ok(self):
        """check_rnode_firmware returns True → status OK, no flash called."""
        result = self._make_result()
        with (
            patch.object(install_all, "check_rnode_firmware", return_value=True),
            patch.object(install_all, "flash_rnode_firmware") as mock_flash,
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "OK"
        assert "already installed" in result.detail
        mock_flash.assert_not_called()

    def test_flash_success_sets_ok(self):
        """check returns False, flash returns (True, "OK") → status OK."""
        result = self._make_result()
        with (
            patch.object(install_all, "check_rnode_firmware", return_value=False),
            patch.object(
                install_all,
                "flash_rnode_firmware",
                return_value=(True, "Flashed successfully"),
            ),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "OK"
        assert "Flashed" in result.detail

    def test_flash_failure_sets_fail(self):
        """check returns False, flash returns (False, "error") → status FAIL."""
        result = self._make_result()
        with (
            patch.object(install_all, "check_rnode_firmware", return_value=False),
            patch.object(
                install_all,
                "flash_rnode_firmware",
                return_value=(False, "Flash error: device not found"),
            ),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "Flash error" in result.detail

    def test_exception_during_check_sets_fail(self):
        """check_rnode_firmware raises → status FAIL, traceback printed."""
        result = self._make_result()
        with patch.object(install_all, "check_rnode_firmware", side_effect=OSError("serial error")):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "Unexpected error" in result.detail

    def test_exception_during_flash_sets_fail(self):
        """flash_rnode_firmware raises → status FAIL."""
        result = self._make_result()
        with (
            patch.object(install_all, "check_rnode_firmware", return_value=False),
            patch.object(
                install_all,
                "flash_rnode_firmware",
                side_effect=RuntimeError("timeout"),
            ),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "Unexpected error" in result.detail


# ── Unit tests for install_services.py ─────────────────────────────


class TestFindRepoRoot:
    """Direct unit tests for install_services._find_repo_root()."""

    def test_finds_by_dockerfile(self, tmp_path):
        """Should detect repo root by finding a Dockerfile marker."""
        (tmp_path / "Dockerfile").write_text("FROM ubuntu")
        with patch.object(
            install_services,
            "__file__",
            str(tmp_path / "tools" / "install_services.py"),
        ):
            root = install_services._find_repo_root()
        assert root == str(tmp_path)

    def test_finds_by_git_dir(self, tmp_path):
        """Should detect repo root by finding a .git directory."""
        (tmp_path / ".git").mkdir()
        with patch.object(
            install_services,
            "__file__",
            str(tmp_path / "tools" / "install_services.py"),
        ):
            root = install_services._find_repo_root()
        assert root == str(tmp_path)

    def test_returns_none_when_not_found(self, tmp_path):
        """Should return None when no marker is found within depth limit."""
        with patch.object(
            install_services,
            "__file__",
            str(tmp_path / "tools" / "install_services.py"),
        ):
            root = install_services._find_repo_root()
        assert root is None

    def test_stops_at_filesystem_root(self, tmp_path):
        """Should return None and not loop infinitely at filesystem root."""
        with patch.object(install_services, "__file__", "/"):
            root = install_services._find_repo_root()
        assert root is None

    def test_prefers_dockerfile_over_git(self, tmp_path):
        """Should prefer Dockerfile marker when both markers are present."""
        (tmp_path / "Dockerfile").write_text("FROM ubuntu")
        (tmp_path / ".git").mkdir()
        (tmp_path / "tools").mkdir()
        with patch.object(
            install_services,
            "__file__",
            str(tmp_path / "tools" / "install_services.py"),
        ):
            root = install_services._find_repo_root()
        # Should find the Dockerfile (checked first) before reaching .git
        assert root == str(tmp_path)


class TestInstallPiServer:
    """Unit tests for install_services.install_pi_server()."""

    def _make_result(self):
        return install_all.DeviceResult("Pi Server")

    def test_skips_when_docker_not_found(self):
        """Result should be SKIP when docker is not on PATH."""
        with patch("shutil.which", return_value=None):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "SKIP"
            assert "docker" in result.detail.lower()

    def test_skips_when_repo_root_none_and_not_found(self):
        """Result should be FAIL when repo_root cannot be located."""
        with patch.object(install_services, "_find_repo_root", return_value=None):
            result = self._make_result()
            install_services.install_pi_server(result, None)
            assert result.status == "FAIL"
            assert "repo root" in result.detail.lower()

    def test_builds_when_docker_found_and_succeeds(self):
        """Result should be OK when docker build succeeds."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "OK"
            assert "Docker image built" in result.detail

    def test_fails_when_docker_build_returns_nonzero(self):
        """Result should be FAIL when docker build returns non-zero."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Error: Dockerfile not found"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Docker build failed" in result.detail

    def test_fails_when_subprocess_raises(self):
        """Result should be FAIL when subprocess.run raises an OSError."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", side_effect=OSError("no such file")),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "FAIL"

    def test_fails_when_subprocess_error_raised(self):
        """Result should be FAIL when subprocess.run raises SubprocessError."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch(
                "subprocess.run",
                side_effect=subprocess.SubprocessError("command failed"),
            ),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Docker build error" in result.detail


class TestInstallK8sServices:
    """Unit tests for install_services.install_k8s_services()."""

    def _make_result(self):
        return install_all.DeviceResult("K8s Services")

    def test_skips_when_kubectl_not_found(self):
        """Result should be SKIP when kubectl is not on PATH."""
        with patch("shutil.which", return_value=None):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "SKIP"
            assert "kubectl" in result.detail.lower()

    def test_skips_when_repo_root_none_and_not_found(self):
        """Result should be FAIL when repo_root cannot be located."""
        with patch.object(install_services, "_find_repo_root", return_value=None):
            result = self._make_result()
            install_services.install_k8s_services(result, None)
            assert result.status == "FAIL"
            assert "repo root" in result.detail.lower()

    def test_fails_when_manifest_not_found(self):
        """Result should be FAIL when a manifest file does not exist."""
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("os.path.isfile", return_value=False),
        ):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Manifest not found" in result.detail

    def test_applies_manifests_when_kubectl_found(self):
        """Result should be OK when both manifests are applied successfully."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "OK"
            assert "Applied" in result.detail
            assert "lmao-service.yaml" in result.detail
            assert "nats-server.yaml" in result.detail

    def test_fails_when_first_manifest_apply_fails(self):
        """Result should be FAIL when the first kubectl apply returns non-zero."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "connection refused"
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "kubectl apply" in result.detail.lower()

    def test_fails_when_subprocess_raises(self):
        """Result should be FAIL when subprocess.run raises an OSError."""
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", side_effect=OSError("no such file")),
        ):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "FAIL"

    def test_fails_when_subprocess_error_raised(self):
        """Result should be FAIL when subprocess.run raises SubprocessError."""
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("os.path.isfile", return_value=True),
            patch(
                "subprocess.run",
                side_effect=subprocess.SubprocessError("kubectl error"),
            ),
        ):
            result = self._make_result()
            install_services.install_k8s_services(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "kubectl error" in result.detail
