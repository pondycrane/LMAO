"""Unit tests for tools/install_all.py — no hardware required.

Covers argument parsing, device result tracking, summary output, and
integration of the main pipeline with mocked hardware detection.

Run with::

    bazel test //tests:test_install_all --test_output=all
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Import the install_all module (available under Bazel via deps).
try:
    from tools import install_all
except ImportError:
    install_all = None


# ── helpers ─────────────────────────────────────────────────────────


def _patch_imports():
    """Patch pyserial and install_all hardware-detection functions.

    Returns a dict of patches keyed by target string so callers can
    start/stop them independently.
    """
    patches = {
        "serial": patch("tools.install_all.serial", MagicMock()),
        "serial.tools": patch("tools.install_all.serial.tools", MagicMock()),
        "serial.tools.list_ports": patch(
            "tools.install_all.serial.tools.list_ports", MagicMock()
        ),
        "find_cardputer_port": patch.object(
            install_all, "find_cardputer_port", return_value=None
        ),
        "find_rnode_port": patch.object(
            install_all, "find_rnode_port", return_value=None
        ),
        "check_rnode_firmware": patch.object(
            install_all, "check_rnode_firmware", return_value=False
        ),
        "flash_rnode_firmware": patch.object(
            install_all, "flash_rnode_firmware", return_value=(True, "OK")
        ),
        "find_client_root": patch.object(
            install_all, "find_client_root", return_value="/fake/client_root"
        ),
    }
    return patches


def _start_patches(patches_dict):
    """Start all mocks in *patches_dict* and return the active mocks."""
    mocks = {}
    for key, p in patches_dict.items():
        mocks[key] = p.start()
    return mocks


def _stop_patches(mocks_dict):
    """Stop all mocks in *mocks_dict*."""
    for p in mocks_dict.values():
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
        args = install_all._parse_args(
            ["--cardputer-port", "/dev/ttyACM0", "--skip-rnode"]
        )
        assert args.cardputer_port == "/dev/ttyACM0"
        assert args.skip_rnode is True
        assert args.skip_cardputer is False


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
            "enter_raw_repl": patch.object(
                install_all, "enter_raw_repl", return_value=True
            ),
            "verify_device": patch.object(
                install_all, "verify_device", return_value=(True, "ESP32 detected")
            ),
            "upload_file": patch.object(
                install_all, "upload_file", return_value=True
            ),
            "exit_raw_repl": patch.object(
                install_all, "exit_raw_repl", return_value=None
            ),
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
        self._serial_patch = patch(
            "tools.install_all.serial.Serial", self.mock_serial_class
        )
        self._serial_patch.start()
        # FILES_TO_UPLOAD is a list, not compatible with patch.object dict stop
        self._saved_files = install_all.FILES_TO_UPLOAD
        install_all.FILES_TO_UPLOAD = ["main.py", "config.py"]
        yield
        for p in self.mocks.values():
            p.stop()
        self._serial_patch.stop()
        install_all.FILES_TO_UPLOAD = self._saved_files

    def _make_result(self):
        return install_all.DeviceResult("Cardputer")

    # ── success path ──

    def test_successful_flash_sets_ok(self):
        """Successful flash should set result to OK."""
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "OK"
        assert "file(s)" in result.detail

    def test_successful_flash_closes_port(self):
        """Port should always be closed after flash."""
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        self.mock_ser.close.assert_called_once()

    # ── serial port open failure ──

    def test_cannot_open_port_sets_fail(self):
        """Serial port open failure should set result to FAIL."""
        import serial as pyserial
        self.mock_serial_class.side_effect = pyserial.SerialException("denied")
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "Cannot open serial port" in result.detail
        self.mock_ser.close.assert_not_called()

    # ── raw REPL failure ──

    def test_raw_repl_failure_sets_fail_and_closes_port(self):
        """Raw REPL entry failure should set FAIL and still close port."""
        self.mocks["enter_raw_repl"].return_value = False
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "raw REPL" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── missing source file ──

    def test_missing_source_file_sets_fail(self):
        """Missing source file should set FAIL and close port."""
        self.mocks["verify_files_exist"].side_effect = FileNotFoundError(
            "main.py not found"
        )
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "Missing source file" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── partial upload failure ──

    def test_partial_upload_sets_fail(self):
        """Partial upload failure should set FAIL."""
        # First file succeeds, second fails
        self.mocks["upload_file"].side_effect = [True, False]
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "failed to upload" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── serial exception during operations ──

    def test_serial_exception_during_operations_sets_fail(self):
        """Serial exception mid-operation should set FAIL and close port."""
        import serial as pyserial
        self.mocks["enter_raw_repl"].side_effect = pyserial.SerialException("disconnected")
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "Serial error" in result.detail
        self.mock_ser.close.assert_called_once()

    # ── KeyboardInterrupt ──

    def test_keyboard_interrupt_sets_fail_and_closes_port(self):
        """Ctrl+C during flash should set FAIL and close port."""
        self.mocks["upload_file"].side_effect = KeyboardInterrupt()
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
        assert result.status == "FAIL"
        assert "Aborted by user" in result.detail
        # Port should still be closed
        self.mock_ser.close.assert_called_once()

    # ── device verification warning (non-ESP32) ──

    def test_device_verification_warning_continues_flash(self):
        """Device verification warning should not stop flash."""
        self.mocks["verify_device"].return_value = (False, "Unknown device")
        result = self._make_result()
        install_all._flash_cardputer_client(
            "/dev/ttyACM0", "/fake/root", result
        )
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
            "find_rnode_port": patch.object(
                install_all, "find_rnode_port", return_value=None
            ),
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
            "verify_device": patch.object(
                install_all, "verify_device", return_value=(True, "ESP32 detected")
            ),
            "upload_file": patch.object(
                install_all, "upload_file", return_value=True
            ),
            "exit_raw_repl": patch.object(
                install_all, "exit_raw_repl", return_value=None
            ),
            "verify_files_exist": patch.object(
                install_all, "verify_files_exist", return_value=None
            ),
            "auto_discover_lib_files": patch.object(
                install_all, "auto_discover_lib_files", return_value=[]
            ),
        }
        self.mocks = _start_patches(patches)
        # Patch FILES_TO_UPLOAD (a list, not compatible with patch.object dict)
        self._saved_files = install_all.FILES_TO_UPLOAD
        install_all.FILES_TO_UPLOAD = ["main.py", "config.py"]
        self._getsizep = patch("os.path.getsize", return_value=100)
        self._getsizep.start()
        # Also mock serial.Serial for _flash_cardputer_client
        self._serial_patch = patch("tools.install_all.serial.Serial", MagicMock())
        self._serial_patch.start()
        yield
        _stop_patches(self.mocks)
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
        self.mocks = _start_patches(patches)
        yield
        _stop_patches(self.mocks)

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
        self.mocks = _start_patches(patches)
        # Both ports return None (no hardware detected)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None
        yield
        _stop_patches(self.mocks)

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
        self.mocks = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["check_rnode_firmware"].return_value = True  # already RNode
        yield
        _stop_patches(self.mocks)

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
        self.mocks = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["check_rnode_firmware"].return_value = False
        self.mocks["flash_rnode_firmware"].return_value = (False, "Flash error")
        yield
        _stop_patches(self.mocks)

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
        self.mocks = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = "/dev/ttyACM0"
        self.mocks["find_rnode_port"].return_value = None
        # Make find_client_root return None (not found)
        self.mocks["find_client_root"].return_value = None
        yield
        _stop_patches(self.mocks)

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
        self.mocks = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        self.mocks["find_rnode_port"].return_value = None  # won't be called
        self.mocks["check_rnode_firmware"].return_value = True
        yield
        _stop_patches(self.mocks)

    def test_rnode_port_override_uses_specified_port(self):
        """When --rnode-port is given, it should be used directly."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--rnode-port", "/dev/customUSB0"])
        assert exc_info.value.code == 0
        # find_rnode_port should NOT be called (explicit port given)
        self.mocks["find_rnode_port"].assert_not_called()
        # check_rnode_firmware should be called with the explicit port
        self.mocks["check_rnode_firmware"].assert_called_once_with("/dev/customUSB0")
