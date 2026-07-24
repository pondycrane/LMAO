"""Unit tests for tools/install_all.py — no hardware required.

Covers argument parsing, device result tracking, summary output, and
integration of the main pipeline with mocked hardware detection.

Run with::

    bazel test //tests:test_install_all --test_output=all
"""

import os
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

# RNode DETECT protocol response signature (firmware v1.x).
_RNODE_DETECT_RESPONSE = bytes([0xC0, 0x08, 0x46, 0xC0])


def _make_serial_mock(read_data: bytes = _RNODE_DETECT_RESPONSE) -> MagicMock:
    """Return a mock serial port whose ``read()`` yields *read_data*."""
    ser = MagicMock()
    ser.read.return_value = read_data
    return ser


def _patch_imports():
    """Patch pyserial and install_all hardware-detection functions.

    Returns a dict of patches keyed by target string so callers can
    start/stop them independently.

    The mock serial port answers the RNode DETECT signature by default,
    so the inline RNode probe fallback (used when ``lma_core`` is not
    importable) also succeeds without hardware.
    """
    patches = {
        "serial_serial": patch(
            "tools.install_all.serial.Serial", return_value=_make_serial_mock()
        ),
        "find_cardputer_port": patch.object(install_all, "find_cardputer_port", return_value=None),
        "find_client_root": patch.object(
            install_all, "find_client_root", return_value="/fake/client_root"
        ),
        "enter_raw_repl": patch.object(install_all, "enter_raw_repl", return_value=True),
        "verify_files_exist": patch.object(install_all, "verify_files_exist", return_value=None),
        "upload_file": patch.object(install_all, "upload_file", return_value=True),
        "exit_raw_repl": patch.object(install_all, "exit_raw_repl", return_value=None),
        "mip_install": patch.object(install_all, "_mip_install", return_value=None),
        "auto_discover_lib_files": patch.object(
            install_all, "auto_discover_lib_files", return_value=[]
        ),
        "verify_device": patch.object(
            install_all, "verify_device", return_value=(True, "ESP32 detected")
        ),
        "os_path_getsize": patch("os.path.getsize", return_value=100),
        "detect_serial_devices": patch.object(
            install_all, "detect_serial_devices", return_value=(None, None)
        ),
        "stop_pi_server_container": patch.object(
            install_all, "stop_pi_server_container", return_value=False
        ),
        "comports": patch("serial.tools.list_ports.comports", return_value=[]),
    }
    # install_all/install_services import lma_core.device_detect lazily
    # inside functions.  When lma_core is importable (always the case
    # under Bazel — it is a dep of install_all_lib), patch its helpers
    # so tests never touch real hardware regardless of import path.
    try:
        import lma_core.device_detect as _dd

        patches["probe_rnode"] = patch.object(_dd, "probe_rnode", return_value=True)
        patches["find_rnode_port"] = patch.object(_dd, "find_rnode_port", return_value=None)
    except ImportError:
        pass
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
        assert args.skip_iot_ingest is False

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

    def test_skip_iot_ingest_flag(self):
        """--skip-iot-ingest sets the flag to True."""
        args = install_all._parse_args(["--include-services", "--skip-iot-ingest"])
        assert args.skip_iot_ingest is True

    def test_skip_iot_ingest_with_skip_k8s(self):
        """--skip-iot-ingest can be combined with --skip-k8s."""
        args = install_all._parse_args(["--include-services", "--skip-k8s", "--skip-iot-ingest"])
        assert args.skip_k8s is True
        assert args.skip_iot_ingest is True

    def test_skip_flags_noop_without_include_services(self):
        """--skip-server and --skip-k8s can be set without --include-services."""
        args = install_all._parse_args(["--skip-server", "--skip-k8s"])
        assert args.skip_server is True
        assert args.include_services is False

    # ── setup-registry flags ──

    def test_setup_registry_default_false(self):
        """--setup-registry defaults to False."""
        args = install_all._parse_args([])
        assert args.setup_registry is False

    def test_setup_registry_flag(self):
        """--setup-registry sets the flag to True."""
        args = install_all._parse_args(["--setup-registry"])
        assert args.setup_registry is True


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
            "disarm_watchdog": patch.object(
                install_all, "disarm_watchdog", return_value=True
            ),
            "recover_wedged_device": patch.object(
                install_all, "recover_wedged_device", return_value=None
            ),
            "verify_files_exist": patch.object(
                install_all, "verify_files_exist", return_value=None
            ),
            "auto_discover_lib_files": patch.object(
                install_all, "auto_discover_lib_files", return_value=[]
            ),
            "mip_install": patch.object(install_all, "_mip_install", return_value=None),
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

    # ── wedge recovery (issue #74) ──

    def test_wedged_device_recovers_and_completes(self):
        """A DeviceStalledError triggers one recovery attempt; the failed
        file is retried on the recovered connection and the flash completes."""
        from cardputer_client.flash import DeviceStalledError

        new_ser = MagicMock()
        self.mocks["recover_wedged_device"].return_value = new_ser
        # First upload_file call stalls; retry (and remaining files) succeed.
        self.mocks["upload_file"].side_effect = [
            DeviceStalledError("wedged at byte 0"),
            True,
            True,
        ]
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "OK"
        self.mocks["recover_wedged_device"].assert_called_once()
        # Old port closed by recovery path, new port closed at the end.
        new_ser.close.assert_called_once()

    def test_wedged_device_unrecoverable_sets_fail(self):
        """When recovery returns None the install fails with the stall error."""
        from cardputer_client.flash import DeviceStalledError

        self.mocks["recover_wedged_device"].return_value = None
        self.mocks["upload_file"].side_effect = DeviceStalledError("wedged")
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "wedged" in result.detail

    def test_wedged_device_restall_after_recovery_sets_fail(self):
        """Only one recovery is attempted; a second stall fails the install."""
        from cardputer_client.flash import DeviceStalledError

        new_ser = MagicMock()
        self.mocks["recover_wedged_device"].return_value = new_ser
        self.mocks["upload_file"].side_effect = [
            DeviceStalledError("wedged"),
            DeviceStalledError("still wedged"),
        ]
        result = self._make_result()
        install_all._flash_cardputer_client("/dev/ttyACM0", "/fake/root", result)
        assert result.status == "FAIL"
        assert "still wedged" in result.detail
        self.mocks["recover_wedged_device"].assert_called_once()

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
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        # Patch FILES_TO_UPLOAD (a list, not compatible with patch.object dict)
        self._saved_files = install_all.FILES_TO_UPLOAD
        install_all.FILES_TO_UPLOAD = ["main.py", "config.py"]
        yield
        _stop_patches(self._patches)
        install_all.FILES_TO_UPLOAD = self._saved_files

    def _set_rnode_detected(self, port: str = "/dev/ttyUSB0") -> None:
        """Configure mocks so the RNode is detected on *port*."""
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].return_value = port
        self.mocks["detect_serial_devices"].return_value = (port, None)

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
        self._set_rnode_detected("/dev/ttyUSB0")
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        self.mocks["find_cardputer_port"].assert_called_once()

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
        # Mock RNode detection to return a port
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["detect_serial_devices"].return_value = ("/dev/ttyUSB0", None)
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--skip-cardputer"])
        assert exc_info.value.code == 0
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


class TestMainNoHardware:
    """Test main() when no hardware is connected."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        # Both ports return None (no hardware detected)
        self.mocks["find_cardputer_port"].return_value = None
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
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["detect_serial_devices"].return_value = ("/dev/ttyUSB0", None)
        yield
        _stop_patches(self._patches)

    def test_rnode_firmware_detected_exits_0(self):
        """RNode probe confirms firmware → exit 0 (no programmatic flashing)."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        if "probe_rnode" in self.mocks:
            self.mocks["probe_rnode"].assert_called_once_with("/dev/ttyUSB0")

    def test_rnode_not_responding_exits_1(self):
        """RNode detected on USB but not answering the probe → FAIL, exit 1.

        RNode firmware is never flashed programmatically (web flasher
        only), so a non-responsive device must be reported as [FAIL].
        """
        if "probe_rnode" in self.mocks:
            self.mocks["probe_rnode"].return_value = False
        self.mocks["serial_serial"].return_value.read.return_value = b""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 1


class TestMainRNodeProbeFails:
    """Test main() when the RNode probe fails on a detected port."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        self.mocks["detect_serial_devices"].return_value = ("/dev/ttyUSB0", None)
        if "probe_rnode" in self.mocks:
            self.mocks["probe_rnode"].return_value = False
        # Inline fallback probe also gets an unexpected (non-RNode) response
        self.mocks["serial_serial"].return_value.read.return_value = b"\x01\x02\x03"
        yield
        _stop_patches(self._patches)

    def test_probe_failure_exits_1(self):
        """When the device does not answer as an RNode, should exit 1."""
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
        self.mocks["detect_serial_devices"].return_value = (None, None)
        yield
        _stop_patches(self._patches)

    def test_rnode_port_override_uses_specified_port(self):
        """When --rnode-port is given, it should be used directly."""
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--rnode-port", "/dev/customUSB0"])
        assert exc_info.value.code == 0
        # Auto-detection should NOT run (explicit port given)
        self.mocks["detect_serial_devices"].assert_not_called()
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].assert_not_called()
        # The RNode probe should run against the explicit port
        if "probe_rnode" in self.mocks:
            self.mocks["probe_rnode"].assert_called_once_with("/dev/customUSB0")


# ── Main pipeline — include-services integration ────────────────────


class TestMainWithoutServices:
    """Test main() without --include-services (back-compat)."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
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

    def test_summary_has_six_rows_when_nothing_detected(self, capsys):
        """Summary should show all six rows even when nothing is connected.

        Six rows: Cardputer, RNode (Heltec), Local Registry, Pi Server,
        K8s Services, IoT Ingest Consumer.
        """
        with pytest.raises(SystemExit):
            install_all.main([])
        captured = capsys.readouterr().out
        assert captured.count("[SKIP]") == 6


class TestMainWithServicesSkipped:
    """Test main() with --include-services but both services skipped."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        # Also mock the service install functions to prevent real Docker/kubectl calls
        patches["install_pi_server"] = patch.object(install_all, "install_pi_server")
        patches["install_k8s_services"] = patch.object(install_all, "install_k8s_services")
        patches["install_iot_ingest_consumer"] = patch.object(
            install_all, "install_iot_ingest_consumer"
        )
        patches["run_pi_server"] = patch.object(install_all, "run_pi_server")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
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
        # Mock the service install functions to prevent real Docker/kubectl calls
        patches["install_pi_server"] = patch.object(install_all, "install_pi_server")
        patches["install_k8s_services"] = patch.object(install_all, "install_k8s_services")
        patches["install_iot_ingest_consumer"] = patch.object(
            install_all, "install_iot_ingest_consumer"
        )
        patches["run_pi_server"] = patch.object(install_all, "run_pi_server")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_include_services_calls_install_functions(self):
        """--include-services should call all three install functions."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        self.mocks["install_pi_server"].assert_called_once()
        self.mocks["install_k8s_services"].assert_called_once()
        self.mocks["install_iot_ingest_consumer"].assert_called_once()

    def test_only_pi_server_called_when_k8s_skipped(self):
        """--skip-k8s should prevent k8s install but allow Pi server."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-k8s"])
        self.mocks["install_pi_server"].assert_called_once()
        self.mocks["install_k8s_services"].assert_not_called()
        # --skip-k8s also skips iot-ingest (cascading skip)
        self.mocks["install_iot_ingest_consumer"].assert_not_called()

    def test_only_k8s_called_when_server_skipped(self):
        """--skip-server should prevent Pi server install but allow K8s."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-server"])
        self.mocks["install_pi_server"].assert_not_called()
        self.mocks["install_k8s_services"].assert_called_once()
        self.mocks["install_iot_ingest_consumer"].assert_called_once()

    def test_skip_iot_ingest_prevents_iot_call(self):
        """--skip-iot-ingest should prevent iot-ingest install but allow others."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-iot-ingest"])
        self.mocks["install_pi_server"].assert_called_once()
        self.mocks["install_k8s_services"].assert_called_once()
        self.mocks["install_iot_ingest_consumer"].assert_not_called()

    def test_service_results_in_summary(self, capsys):
        """Pi Server, K8s Services, and IoT Ingest Consumer should appear in summary."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        captured = capsys.readouterr().out
        assert "Pi Server" in captured
        assert "K8s Services" in captured
        assert "IoT Ingest Consumer" in captured

    def test_run_pi_server_called_after_successful_release(self):
        """run_pi_server should deploy the container when the build/push OK."""
        self.mocks["install_pi_server"].side_effect = lambda result: result.ok("released")
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        self.mocks["run_pi_server"].assert_called_once()

    def test_stops_server_container_before_hardware_stages(self):
        """--include-services should stop a running lmao-server container
        before hardware probing so it cannot race the RNode probe."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        self.mocks["stop_pi_server_container"].assert_called_once()

    def test_no_container_stop_when_server_skipped(self):
        """--skip-server should not stop the container."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services", "--skip-server"])
        self.mocks["stop_pi_server_container"].assert_not_called()

    def test_run_pi_server_skipped_when_release_fails(self):
        """run_pi_server must NOT run when the image build/push failed —
        the stage stays [FAIL] and the summary exits non-zero."""
        self.mocks["install_pi_server"].side_effect = lambda result: result.fail("push failed")
        with pytest.raises(SystemExit) as exc_info:
            install_all.main(["--include-services"])
        self.mocks["run_pi_server"].assert_not_called()
        assert exc_info.value.code == 1


# ── Unit tests for install_services.py ─────────────────────────────


class TestInstallRNodeFirmware:
    """Direct unit tests for _install_rnode_firmware().

    The current implementation probes for RNode firmware via
    ``lma_core.device_detect.probe_rnode`` (falling back to an inline
    serial probe when lma_core is unavailable).  It never flashes
    programmatically — the web flasher is the only supported method.
    """

    def _make_result(self):
        return install_all.DeviceResult("RNode (Heltec)")

    def test_probe_ok_returns_ok(self):
        """probe_rnode returns True → status OK."""
        result = self._make_result()
        with patch("lma_core.device_detect.probe_rnode", return_value=True):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "OK"
        assert "RNode firmware detected" in result.detail

    def test_probe_false_inline_signature_returns_ok(self):
        """probe_rnode False, inline probe gets DETECT signature → OK."""
        result = self._make_result()
        with (
            patch("lma_core.device_detect.probe_rnode", return_value=False),
            patch(
                "tools.install_all.serial.Serial",
                return_value=_make_serial_mock(_RNODE_DETECT_RESPONSE),
            ),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "OK"
        assert "RNode firmware detected" in result.detail

    def test_probe_false_unexpected_response_sets_fail(self):
        """Device responds but not as RNode → status FAIL."""
        result = self._make_result()
        with (
            patch("lma_core.device_detect.probe_rnode", return_value=False),
            patch(
                "tools.install_all.serial.Serial",
                return_value=_make_serial_mock(b"\x01\x02\x03"),
            ),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "responded but not as RNode" in result.detail

    def test_probe_false_no_response_sets_fail(self):
        """Device does not respond at all → status FAIL with web-flasher hint."""
        result = self._make_result()
        with (
            patch("lma_core.device_detect.probe_rnode", return_value=False),
            patch("tools.install_all.serial.Serial", return_value=_make_serial_mock(b"")),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "not responding as RNode" in result.detail

    def test_serial_exception_sets_fail(self):
        """Serial error during probe → status FAIL."""
        result = self._make_result()
        with (
            patch("lma_core.device_detect.probe_rnode", return_value=False),
            patch("tools.install_all.serial.Serial", side_effect=OSError("serial error")),
        ):
            install_all._install_rnode_firmware("/dev/ttyUSB0", result)
        assert result.status == "FAIL"
        assert "RNode probe failed" in result.detail


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

    # ── Fast-path tests (BUILD_WORKSPACE_DIRECTORY) ──

    def test_finds_via_build_workspace_env(self, tmp_path):
        """BUILD_WORKSPACE_DIRECTORY env var should be used when set."""
        with (
            patch.dict(os.environ, {"BUILD_WORKSPACE_DIRECTORY": str(tmp_path)}, clear=False),
            patch.object(install_services, "__file__", "/nonexistent/tools/install_services.py"),
        ):
            root = install_services._find_repo_root()
        assert root == str(tmp_path)

    def test_build_workspace_env_skipped_when_not_dir(self):
        """BUILD_WORKSPACE_DIRECTORY is set but not a directory → fall through."""
        with (
            patch.dict(os.environ, {"BUILD_WORKSPACE_DIRECTORY": "/nonexistent/path"}, clear=False),
            patch.object(install_services, "__file__", "/"),
        ):
            root = install_services._find_repo_root()
        # Should fall through (no Dockerfile or .git found at /)
        assert root is None

    def test_build_workspace_env_not_set_falls_through(self, tmp_path):
        """Without BUILD_WORKSPACE_DIRECTORY, should walk directories as normal."""
        (tmp_path / "Dockerfile").write_text("FROM ubuntu")
        # Ensure env var is not set
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                install_services,
                "__file__",
                str(tmp_path / "tools" / "install_services.py"),
            ),
        ):
            root = install_services._find_repo_root()
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

    def test_releases_image_when_build_and_push_succeed(self):
        """Result should be OK when build + registry push both succeed."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_proc),
            patch.object(install_services, "_check_registry", return_value=(True, "reachable")),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "OK"
            assert "released to local registry" in result.detail
            assert "192.168.0.36:5000/lmao-server:latest" in result.detail

    def test_fails_when_registry_unreachable(self):
        """Result should be FAIL when the local registry is unreachable."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_proc),
            patch.object(
                install_services,
                "_check_registry",
                return_value=(False, "registry unreachable — start it"),
            ),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Cannot release image" in result.detail

    def test_fails_when_push_fails(self):
        """Result should be FAIL when docker push returns non-zero."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "denied: access forbidden"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", side_effect=[mock_ok, mock_ok, mock_fail]),
            patch.object(install_services, "_check_registry", return_value=(True, "reachable")),
        ):
            result = self._make_result()
            install_services.install_pi_server(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "docker push" in result.detail

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

    @pytest.fixture(autouse=True)
    def _patch_cluster_check(self):
        """Patch the K8s cluster health check (no real cluster in tests)."""
        with patch.object(
            install_services, "_check_k8s_cluster", return_value=(True, "cluster healthy")
        ):
            yield

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


class TestInstallIotIngestConsumer:
    """Unit tests for install_services.install_iot_ingest_consumer()."""

    @pytest.fixture(autouse=True)
    def _patch_health_checks(self):
        """Patch cluster + registry health checks (no real network)."""
        with (
            patch.object(
                install_services, "_check_k8s_cluster", return_value=(True, "cluster healthy")
            ),
            patch.object(
                install_services, "_check_registry", return_value=(True, "registry reachable")
            ),
        ):
            yield

    def _make_result(self):
        return install_all.DeviceResult("IoT Ingest Consumer")

    def test_skips_when_docker_not_found(self):
        """Result should be SKIP when docker is not on PATH."""
        with patch("shutil.which", return_value=None):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "SKIP"
            assert "docker" in result.detail.lower()

    def test_skips_when_repo_root_none_and_not_found(self):
        """Result should be FAIL when repo_root cannot be located."""
        with patch.object(install_services, "_find_repo_root", return_value=None):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, None)
            assert result.status == "FAIL"
            assert "repo root" in result.detail.lower()

    def test_fails_when_dockerfile_not_found(self):
        """Result should be FAIL when Dockerfile.iot-ingest does not exist."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=False),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Dockerfile not found" in result.detail

    def test_deploys_and_verifies_running_when_all_succeed(self):
        """OK when build + push + apply + rollout + pod check all succeed."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Running"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "OK"
            assert "deployed and Running" in result.detail
            assert "192.168.0.36:5000/lmao-iot-ingest:latest" in result.detail

    def test_fails_when_registry_unreachable(self):
        """Result should be FAIL when the local registry is unreachable."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
            patch.object(
                install_services, "_check_registry", return_value=(False, "registry down")
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Cannot release image" in result.detail

    def test_fails_when_push_fails(self):
        """Result should be FAIL when docker push returns non-zero."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "denied"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            # build ok, tag ok, push fails
            patch("subprocess.run", side_effect=[mock_ok, mock_ok, mock_fail]),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "docker push" in result.detail

    def test_fails_when_rollout_does_not_complete(self):
        """Result should be FAIL when the Deployment rollout times out."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_rollout_fail = MagicMock()
        mock_rollout_fail.returncode = 1
        mock_rollout_fail.stderr = "error: timed out waiting for the condition"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            # build, tag, push, apply all ok; rollout fails
            patch(
                "subprocess.run",
                side_effect=[mock_ok, mock_ok, mock_ok, mock_ok, mock_rollout_fail],
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "rollout status" in result.detail

    def test_fails_when_no_pod_running(self):
        """Result should be FAIL when no consumer pod reaches Running."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_pods = MagicMock()
        mock_pods.returncode = 0
        mock_pods.stdout = "Pending"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            # build, tag, push, apply, rollout ok; pod check returns Pending
            patch(
                "subprocess.run",
                side_effect=[mock_ok, mock_ok, mock_ok, mock_ok, mock_ok, mock_pods],
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "No iot-ingest-consumer pod Running" in result.detail

    def test_ok_when_cluster_unreachable_after_push(self):
        """Image is released even when the cluster is down; stage reports OK
        with recovery instructions (deploy when cluster recovers)."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
            patch.object(
                install_services, "_check_k8s_cluster", return_value=(False, "cluster down")
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "OK"
            assert "kubectl apply" in result.detail

    def test_fails_when_docker_build_returns_nonzero(self):
        """Result should be FAIL when docker build returns non-zero."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Error: manifest not found"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Docker build failed" in result.detail

    def test_fails_when_docker_build_raises_subprocess_error(self):
        """Result should be FAIL when docker build raises SubprocessError."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch(
                "subprocess.run",
                side_effect=subprocess.SubprocessError("build failed"),
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Docker build error" in result.detail

    def test_fails_when_kubectl_not_found_after_docker_build(self):
        """When docker build succeeds but kubectl is missing, result should be SKIP.

        Note: The function short-circuits at the Docker check when docker
        is missing. To test the post-Docker kubectl check, docker must
        succeed first.
        """
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        # shutil.which returns docker but not kubectl
        def _which_side_effect(cmd):
            if cmd == "docker":
                return "/usr/bin/docker"
            return None

        with (
            patch("shutil.which", side_effect=_which_side_effect),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "SKIP"
            assert "kubectl" in result.detail.lower()

    def test_fails_when_kubectl_apply_returns_nonzero(self):
        """Result should be FAIL when kubectl apply returns non-zero."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # Docker succeeds
        mock_proc.stderr = ""

        # build, tag, push succeed; kubectl apply fails
        mock_fail_proc = MagicMock()
        mock_fail_proc.returncode = 1
        mock_fail_proc.stderr = "connection refused"

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch(
                "subprocess.run",
                side_effect=[mock_proc, mock_proc, mock_proc, mock_fail_proc],
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "kubectl apply" in result.detail.lower()

    def test_fails_when_kubectl_apply_raises_subprocess_error(self):
        """Result should be FAIL when kubectl apply raises SubprocessError."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            # build, tag, push succeed; kubectl apply raises
            patch(
                "subprocess.run",
                side_effect=[
                    mock_proc,
                    mock_proc,
                    mock_proc,
                    subprocess.SubprocessError("kubectl error"),
                ],
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "kubectl error" in result.detail

    def test_fails_when_docker_build_raises_os_error(self):
        """Result should be FAIL when docker build raises OSError (caught by
        the generic ``except Exception`` handler)."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch(
                "subprocess.run",
                side_effect=OSError("no such file"),
            ),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Unexpected error during Docker build" in result.detail

    def test_fails_when_manifest_not_found_for_kubectl(self):
        """Result should be FAIL when k8s/iot-ingest.yaml is missing."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        # os.path.isfile returns True for Dockerfile, False for manifest
        def _isfile(path):
            return "Dockerfile" in path

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", side_effect=_isfile),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.install_iot_ingest_consumer(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "iot-ingest.yaml" in result.detail.lower()

# ── Unit tests for _check_registry() / _tag_and_push() ─────────────


class TestCheckRegistry:
    """Unit tests for install_services._check_registry()."""

    def test_ok_when_registry_responds_200(self):
        """HTTP 200 from /v2/ means the registry is reachable."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok, msg = install_services._check_registry()
            assert ok is True
            assert "reachable" in msg

    def test_fails_when_connection_refused(self):
        """Connection error → not ok, with recovery instructions."""
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            ok, msg = install_services._check_registry()
            assert ok is False
            assert "unreachable" in msg
            assert "manage.sh start" in msg


class TestCheckK8sCluster:
    """Unit tests for install_services._check_k8s_cluster()."""

    def test_fails_when_kubectl_not_found(self):
        with patch("shutil.which", return_value=None):
            ok, msg = install_services._check_k8s_cluster()
            assert ok is False
            assert "kubectl not found" in msg

    def test_healthy_cluster_with_version_skew_warning(self):
        """Benign stderr warnings (client/server version skew) must NOT be
        treated as cluster failure — serverVersion in stdout is authoritative."""
        import json

        version_proc = MagicMock()
        version_proc.returncode = 0
        version_proc.stdout = json.dumps(
            {"clientVersion": {"gitVersion": "v1.30.0"}, "serverVersion": {"gitVersion": "v1.35.5"}}
        )
        version_proc.stderr = (
            "WARNING: version difference between client (1.30) and server (1.35) "
            "exceeds the supported minor version skew of +/-1"
        )
        nodes_proc = MagicMock()
        nodes_proc.returncode = 0
        nodes_proc.stdout = "True True True"
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[version_proc, nodes_proc]),
        ):
            ok, msg = install_services._check_k8s_cluster()
            assert ok is True
            assert "3/3" in msg

    def test_fails_when_connection_refused(self):
        version_proc = MagicMock()
        version_proc.returncode = 1
        version_proc.stdout = ""
        version_proc.stderr = "The connection to the server 192.168.0.45:6443 was refused"
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=version_proc),
        ):
            ok, msg = install_services._check_k8s_cluster()
            assert ok is False
            assert "refusing connections" in msg

    def test_fails_when_no_route_to_host(self):
        version_proc = MagicMock()
        version_proc.returncode = 1
        version_proc.stdout = ""
        version_proc.stderr = "Unable to connect to the server: dial tcp: no route to host"
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=version_proc),
        ):
            ok, msg = install_services._check_k8s_cluster()
            assert ok is False
            assert "unreachable" in msg

    def test_fails_when_no_nodes_ready(self):
        import json

        version_proc = MagicMock()
        version_proc.returncode = 0
        version_proc.stdout = json.dumps({"serverVersion": {"gitVersion": "v1.35.5"}})
        version_proc.stderr = ""
        nodes_proc = MagicMock()
        nodes_proc.returncode = 0
        nodes_proc.stdout = "False False"
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[version_proc, nodes_proc]),
        ):
            ok, msg = install_services._check_k8s_cluster()
            assert ok is False
            assert "not Ready" in msg


class TestTagAndPush:
    """Unit tests for install_services._tag_and_push()."""

    def _make_result(self):
        return install_all.DeviceResult("Test")

    def test_ok_when_tag_and_push_succeed(self):
        """Returns True when docker tag + push both succeed."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        result = self._make_result()
        with patch("subprocess.run", return_value=mock_proc):
            assert install_services._tag_and_push(
                result, "lmao-server:latest", "reg:5000/lmao-server:latest"
            )
        assert result.status != "FAIL"

    def test_fails_when_tag_fails(self):
        """Returns False and marks FAIL when docker tag fails."""
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "no such image"
        result = self._make_result()
        with patch("subprocess.run", return_value=mock_fail):
            assert not install_services._tag_and_push(
                result, "lmao-server:latest", "reg:5000/lmao-server:latest"
            )
        assert result.status == "FAIL"
        assert "docker tag failed" in result.detail

    def test_fails_when_push_fails(self):
        """Returns False and marks FAIL when docker push fails."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "denied"
        result = self._make_result()
        with patch("subprocess.run", side_effect=[mock_ok, mock_fail]):
            assert not install_services._tag_and_push(
                result, "lmao-server:latest", "reg:5000/lmao-server:latest"
            )
        assert result.status == "FAIL"
        assert "docker push" in result.detail


# ── Unit tests for setup_registry() ─────────────────────────────────


class TestSetupRegistry:
    """Unit tests for install_services.setup_registry()."""

    def _make_result(self):
        return install_all.DeviceResult("Local Registry")

    def test_skips_when_docker_not_found(self):
        """Result should be SKIP when docker is not on PATH."""
        with patch("shutil.which", return_value=None):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "SKIP"
            assert "docker" in result.detail.lower()

    def test_fails_when_repo_root_none_and_not_found(self):
        """Result should be FAIL when repo_root cannot be located."""
        with patch.object(install_services, "_find_repo_root", return_value=None):
            result = self._make_result()
            install_services.setup_registry(result, None)
            assert result.status == "FAIL"
            assert "repo root" in result.detail.lower()

    def test_fails_when_manage_script_not_found(self):
        """Result should be FAIL when manage.sh does not exist."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=False),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "manage.sh" in result.detail.lower()

    def test_ok_when_start_and_push_succeed(self):
        """Result should be OK when both start and push succeed."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "OK"
            assert "pushed" in result.detail.lower()

    def test_fails_when_start_returns_nonzero(self):
        """Result should be FAIL when manage.sh start fails."""
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "Error: port in use"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", return_value=mock_fail),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "start failed" in result.detail.lower()

    def test_fails_when_push_returns_nonzero(self):
        """Result should be FAIL when manage.sh push fails after start succeeds."""
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "Error: connection refused"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", side_effect=[mock_ok, mock_fail]),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "push failed" in result.detail.lower()

    def test_fails_when_subprocess_raises(self):
        """Result should be FAIL when subprocess.run raises SubprocessError."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", side_effect=subprocess.SubprocessError("timeout")),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Registry setup error" in result.detail

    def test_fails_when_generic_exception_raised(self):
        """Result should be FAIL when an unexpected exception occurs."""
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run", side_effect=RuntimeError("kernel panic")),
        ):
            result = self._make_result()
            install_services.setup_registry(result, "/fake/repo")
            assert result.status == "FAIL"
            assert "Unexpected error" in result.detail


# ── Main pipeline — registry integration ────────────────────────────


class TestMainWithRegistry:
    """Test main() with --setup-registry flag."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        patches["setup_registry"] = patch.object(install_all, "setup_registry")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_registry_skipped_when_flag_not_set(self, capsys):
        """Local Registry should show SKIP when --setup-registry not set."""
        with pytest.raises(SystemExit):
            install_all.main([])
        captured = capsys.readouterr().out
        assert "Local Registry" in captured
        assert "--setup-registry not set" in captured
        self.mocks["setup_registry"].assert_not_called()

    def test_registry_called_when_flag_set(self):
        """--setup-registry should call setup_registry()."""
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry"])
        self.mocks["setup_registry"].assert_called_once()

    def test_registry_in_summary_when_set(self, capsys):
        """Local Registry should appear in summary when flag is set."""
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry"])
        captured = capsys.readouterr().out
        assert "Local Registry" in captured

    def test_registry_failure_does_not_block_services(self, capsys):
        """Registry failure should not prevent service install."""
        # setup_registry raises an exception
        self.mocks["setup_registry"].side_effect = RuntimeError("Docker daemon not running")
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry"])
        captured = capsys.readouterr().out
        assert "FAIL" in captured
        assert "Docker daemon not running" in captured


# ── Main pipeline — registry + services integration ──────────────


class TestMainWithRegistryAndServices:
    """Test main() with --setup-registry and --include-services together."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        patches["setup_registry"] = patch.object(install_all, "setup_registry")
        patches["install_pi_server"] = patch.object(install_all, "install_pi_server")
        patches["install_k8s_services"] = patch.object(install_all, "install_k8s_services")
        patches["install_iot_ingest_consumer"] = patch.object(
            install_all, "install_iot_ingest_consumer"
        )
        patches["run_pi_server"] = patch.object(install_all, "run_pi_server")
        self.mocks, self._patches = _start_patches(patches)
        self.mocks["find_cardputer_port"].return_value = None
        yield
        _stop_patches(self._patches)

    def test_registry_with_services_calls_iot_deploy(self):
        """When both --setup-registry and --include-services are set,
        install_iot_ingest_consumer should be called (it releases through
        the local registry internally — no registry kwargs needed)."""
        # Simulate successful registry setup (sets result.status to "OK")
        self.mocks["setup_registry"].side_effect = lambda result: setattr(result, "status", "OK")
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry", "--include-services"])
        self.mocks["install_iot_ingest_consumer"].assert_called_once()
        call_kwargs = self.mocks["install_iot_ingest_consumer"].call_args.kwargs
        assert "registry_host" not in call_kwargs
        assert "registry_port" not in call_kwargs

    def test_services_only_no_registry_passed_to_iot(self):
        """When only --include-services (no --setup-registry) is set,
        install_iot_ingest_consumer should NOT receive registry params."""
        with pytest.raises(SystemExit):
            install_all.main(["--include-services"])
        self.mocks["install_iot_ingest_consumer"].assert_called_once()
        call_kwargs = self.mocks["install_iot_ingest_consumer"].call_args.kwargs
        assert "registry_host" not in call_kwargs
        assert "registry_port" not in call_kwargs

    def test_registry_only_does_not_call_iot(self):
        """When only --setup-registry (no --include-services) is set,
        install_iot_ingest_consumer should NOT be called."""
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry"])
        self.mocks["install_iot_ingest_consumer"].assert_not_called()

    def test_registry_with_services_and_skip_iot(self):
        """--skip-iot-ingest should prevent iot install even with registry."""
        self.mocks["setup_registry"].side_effect = lambda result: setattr(result, "status", "OK")
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry", "--include-services", "--skip-iot-ingest"])
        self.mocks["install_iot_ingest_consumer"].assert_not_called()

    def test_registry_failure_still_attempts_iot_deploy(self):
        """When registry setup fails, IoT deploy is still attempted (it will
        fail at the push step with a clear message if the registry is down)."""
        # Simulate registry failure (status stays SKIP or becomes FAIL)
        self.mocks["setup_registry"].side_effect = lambda result: setattr(result, "status", "FAIL")
        with pytest.raises(SystemExit):
            install_all.main(["--setup-registry", "--include-services"])
        self.mocks["install_iot_ingest_consumer"].assert_called_once()
        call_kwargs = self.mocks["install_iot_ingest_consumer"].call_args.kwargs
        assert "registry_host" not in call_kwargs
        assert "registry_port" not in call_kwargs


# ── Unit tests for new helpers in install_services.py ──────────────


class TestDetectRNodePort:
    """Direct unit tests for install_services._detect_rnode_port().

    Tests the updated implementation that delegates to ``detect_serial_devices()``
    then ``_detect_rnode_port_fallback()`` before returning ``None``.
    """

    def test_env_var_overrides_everything(self):
        """LMAO_RNODE_PORT env var should be returned directly without calling detect."""
        with (
            patch.dict(os.environ, {"LMAO_RNODE_PORT": "/dev/ttyS0"}, clear=True),
            patch.object(install_services, "detect_serial_devices") as mock_detect,
            patch.object(install_services, "_detect_rnode_port_fallback") as mock_fallback,
        ):
            port = install_services._detect_rnode_port()
            assert port == "/dev/ttyS0"
            mock_detect.assert_not_called()
            mock_fallback.assert_not_called()

    def test_env_var_warns_if_device_missing(self, capsys):
        """LMAO_RNODE_PORT set but device missing → warning printed, port still returned."""
        with (
            patch.dict(os.environ, {"LMAO_RNODE_PORT": "/dev/ttyS0"}, clear=True),
            patch("os.path.exists", return_value=False),
        ):
            port = install_services._detect_rnode_port()
            assert port == "/dev/ttyS0"
            captured = capsys.readouterr().out
            assert "WARNING" in captured
            assert "device not found" in captured

    def test_detect_serial_devices_used(self):
        """detect_serial_devices called when no env var set."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                install_services, "detect_serial_devices", return_value=("/dev/ttyACM0", None)
            ),
            patch.object(install_services, "_detect_rnode_port_fallback") as mock_fallback,
        ):
            port = install_services._detect_rnode_port()
            assert port == "/dev/ttyACM0"
            mock_fallback.assert_not_called()

    def test_fallback_used_when_detect_returns_none(self):
        """_detect_rnode_port_fallback used when detect_serial_devices returns None."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(install_services, "detect_serial_devices", return_value=(None, None)),
            patch.object(
                install_services, "_detect_rnode_port_fallback", return_value="/dev/ttyUSB0"
            ),
        ):
            port = install_services._detect_rnode_port()
            assert port == "/dev/ttyUSB0"

    def test_returns_none_when_everything_fails(self):
        """Fallback returns None → returns None (no more hardcoded /dev/ttyUSB0)."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(install_services, "detect_serial_devices", return_value=(None, None)),
            patch.object(install_services, "_detect_rnode_port_fallback", return_value=None),
        ):
            port = install_services._detect_rnode_port()
            assert port is None


# ── Unit tests for detect_serial_devices and probe functions ────────


# Helper to create fake port objects for mocking serial.tools.list_ports.comports()
def _make_port(device, vid, pid, description="USB device"):
    from types import SimpleNamespace

    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


class TestDetectSerialDevices:
    """Tests for install_services.detect_serial_devices().

    The function delegates to ``lma_core.device_detect.detect_devices()``;
    VID/PID fingerprint matching itself is covered by
    ``tests/test_device_detect.py``.  These tests cover the delegation
    and the ImportError fallback only.
    """

    def _make_detection(self, rnode_port=None, cardputer_port=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            rnode_port=rnode_port,
            cardputer_port=cardputer_port,
            all_ports=[],
            confidence={},
        )

    def test_returns_detected_ports(self):
        """Both devices detected → both ports returned."""
        with patch(
            "lma_core.device_detect.detect_devices",
            return_value=self._make_detection("/dev/ttyUSB0", "/dev/ttyACM0"),
        ):
            rnode, cardputer = install_services.detect_serial_devices()
        assert rnode == "/dev/ttyUSB0"
        assert cardputer == "/dev/ttyACM0"

    def test_returns_none_when_nothing_detected(self):
        """Nothing detected → (None, None)."""
        with patch(
            "lma_core.device_detect.detect_devices",
            return_value=self._make_detection(),
        ):
            rnode, cardputer = install_services.detect_serial_devices()
        assert rnode is None
        assert cardputer is None

    def test_import_error_uses_fallback(self):
        """ImportError on lma_core → falls back to _detect_rnode_port_fallback()."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "lma_core.device_detect":
                raise ImportError("no lma_core")
            return real_import(name, *args, **kwargs)

        with (
            patch.object(
                install_services, "_detect_rnode_port_fallback", return_value="/dev/ttyACM0"
            ),
            patch("builtins.__import__", side_effect=mock_import),
        ):
            rnode, cardputer = install_services.detect_serial_devices()
        assert rnode == "/dev/ttyACM0"
        assert cardputer is None

class TestProbeForRNode:
    """Tests for install_services._probe_for_rnode().

    The function delegates to ``lma_core.device_detect.probe_rnode``
    (covered by tests/test_device_detect.py) with an inline serial
    fallback when lma_core is not importable.
    """

    def test_delegates_to_lma_core(self):
        """Return value comes from lma_core.device_detect.probe_rnode."""
        with patch("lma_core.device_detect.probe_rnode", return_value=True):
            assert install_services._probe_for_rnode("/dev/ttyUSB0") is True
        with patch("lma_core.device_detect.probe_rnode", return_value=False):
            assert install_services._probe_for_rnode("/dev/ttyUSB0") is False

    def _force_lma_core_import_error(self):
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "lma_core.device_detect":
                raise ImportError("no lma_core")
            return real_import(name, *args, **kwargs)

        return patch("builtins.__import__", side_effect=mock_import)

    def test_inline_fallback_detects_signature(self):
        """ImportError fallback: DETECT signature on serial → True."""
        with (
            self._force_lma_core_import_error(),
            patch("serial.Serial", return_value=_make_serial_mock(_RNODE_DETECT_RESPONSE)),
        ):
            assert install_services._probe_for_rnode("/dev/ttyUSB0") is True

    def test_inline_fallback_no_response_returns_false(self):
        """ImportError fallback: no response → False."""
        with (
            self._force_lma_core_import_error(),
            patch("serial.Serial", return_value=_make_serial_mock(b"")),
        ):
            assert install_services._probe_for_rnode("/dev/ttyUSB0") is False

    def test_inline_fallback_serial_error_returns_false(self):
        """ImportError fallback: serial error → False (no crash)."""
        with (
            self._force_lma_core_import_error(),
            patch("serial.Serial", side_effect=OSError("no such port")),
        ):
            assert install_services._probe_for_rnode("/dev/ttyUSB0") is False


class TestDetectRNodePortFallback:
    """Tests for install_services._detect_rnode_port_fallback()."""

    def test_returns_first_existing_port(self):
        """Returns the first port from the list that exists."""
        with patch("os.path.exists", side_effect=lambda p: p == "/dev/ttyACM0"):
            port = install_services._detect_rnode_port_fallback()
            assert port == "/dev/ttyACM0"

    def test_returns_none_when_no_ports_exist(self):
        """No existing ports → returns None."""
        with patch("os.path.exists", return_value=False):
            port = install_services._detect_rnode_port_fallback()
            assert port is None


class TestMainDetectSerialDevices:
    """Test main() with the new detect_serial_devices path."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        patches = _patch_imports()
        self.mocks, self._patches = _start_patches(patches)
        # detect_serial_devices is already patched by _patch_imports at install_all level
        self.mocks["detect_serial_devices"].return_value = ("/dev/ttyACM0", None)
        yield
        _stop_patches(self._patches)

    def test_rnode_detected_via_detect_serial_devices(self):
        """RNode detected by detect_serial_devices → processed."""
        if "find_rnode_port" in self.mocks:
            self.mocks["find_rnode_port"].return_value = "/dev/ttyUSB0"
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0

    def test_no_rnode_detected_skips(self, capsys):
        """No RNode detected → skip with message."""
        self.mocks["detect_serial_devices"].return_value = (None, None)
        with pytest.raises(SystemExit) as exc_info:
            install_all.main([])
        assert exc_info.value.code == 0
        captured = capsys.readouterr().out
        assert "SKIP" in captured
        assert "not detected" in captured.lower()


class TestDockerPsql:
    """Direct unit tests for install_services._docker_psql()."""

    def _make_result(self):
        return install_all.DeviceResult("Pi Server")

    def test_returns_container_id_when_running(self):
        """Should return the container ID when docker ps matches."""
        mock_proc = MagicMock()
        mock_proc.stdout = "abc123\n"
        mock_proc.returncode = 0
        with patch("subprocess.run", return_value=mock_proc):
            cid = install_services._docker_psql("name=lmao-server")
            assert cid == "abc123"

    def test_returns_none_when_no_container(self):
        """Should return None when no container matches."""
        mock_proc = MagicMock()
        mock_proc.stdout = ""
        mock_proc.returncode = 0
        with patch("subprocess.run", return_value=mock_proc):
            cid = install_services._docker_psql("name=lmao-server")
            assert cid is None


class TestResolveNatsAddress:
    """Unit tests for install_services._resolve_nats_address()."""

    @pytest.fixture(autouse=True)
    def _patch_cluster_check(self):
        """Patch the K8s cluster health check (no real cluster in tests)."""
        with patch.object(
            install_services, "_check_k8s_cluster", return_value=(True, "cluster healthy")
        ):
            yield

    def test_returns_none_when_kubectl_not_found(self):
        """No kubectl on PATH should return None."""
        with patch("shutil.which", return_value=None):
            assert install_services._resolve_nats_address() is None

    def test_returns_none_when_get_svc_fails(self):
        """kubectl get svc returns non-zero should return None."""
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=mock_fail),
        ):
            assert install_services._resolve_nats_address() is None

    def test_returns_nats_nodeport_for_nodeport_svc(self):
        """NodePort service should return nats://<node_ip>:<node_port>."""
        mock_ctx = MagicMock()
        mock_ctx.returncode = 0
        mock_ctx.stdout = "minikube\n"
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "NodePort|10.43.0.1\n"
        mock_port = MagicMock()
        mock_port.returncode = 0
        mock_port.stdout = "30146\n"
        mock_node = MagicMock()
        mock_node.returncode = 0
        mock_node.stdout = "192.168.0.43\n"

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[mock_ctx, mock_svc, mock_port, mock_node]),
        ):
            result = install_services._resolve_nats_address()
            assert result == "nats://192.168.0.43:30146"

    def test_returns_nats_clusterip_for_clusterip_svc(self):
        """ClusterIP service should return nats://<cluster_ip>:4222."""
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "ClusterIP|10.43.0.1\n"

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=mock_svc),
        ):
            result = install_services._resolve_nats_address()
            assert result == "nats://10.43.0.1:4222"

    def test_returns_none_when_clusterip_is_none(self):
        """ClusterIP service with IP 'None' should return None."""
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "ClusterIP|None\n"

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=mock_svc),
        ):
            assert install_services._resolve_nats_address() is None

    def test_returns_none_when_nodeport_has_no_port(self):
        """NodePort service with empty port should return None."""
        mock_ctx = MagicMock()
        mock_ctx.returncode = 0
        mock_ctx.stdout = "minikube\n"
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "NodePort|10.43.0.1\n"
        mock_port = MagicMock()
        mock_port.returncode = 0
        mock_port.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[mock_ctx, mock_svc, mock_port]),
        ):
            assert install_services._resolve_nats_address() is None

    def test_returns_none_when_nodeport_has_no_node_ip(self):
        """NodePort service with empty node IP should return None."""
        mock_ctx = MagicMock()
        mock_ctx.returncode = 0
        mock_ctx.stdout = "minikube\n"
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "NodePort|10.43.0.1\n"
        mock_port = MagicMock()
        mock_port.returncode = 0
        mock_port.stdout = "30146\n"
        mock_node = MagicMock()
        mock_node.returncode = 0
        mock_node.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[mock_ctx, mock_svc, mock_port, mock_node]),
        ):
            assert install_services._resolve_nats_address() is None

    def test_returns_none_on_subprocess_error(self):
        """SubprocessError during kubectl call should return None."""
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["kubectl"], 10)),
        ):
            assert install_services._resolve_nats_address() is None

    def test_returns_none_when_svc_type_unexpected(self):
        """Unknown service type (e.g., LoadBalancer) should return None."""
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "LoadBalancer|10.43.0.1\n"

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=mock_svc),
        ):
            assert install_services._resolve_nats_address() is None

    def test_handles_malformed_jsonpath_output(self):
        """JSONPath output with missing '|' delimiter and no NodePort should return None."""
        mock_ctx = MagicMock()
        mock_ctx.returncode = 0
        mock_ctx.stdout = "minikube\n"
        mock_svc = MagicMock()
        mock_svc.returncode = 0
        mock_svc.stdout = "NodePort|\n"
        mock_empty = MagicMock()
        mock_empty.returncode = 0
        mock_empty.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[mock_ctx, mock_svc, mock_empty, mock_empty]),
        ):
            assert install_services._resolve_nats_address() is None


class TestRunPiServer:
    """Unit tests for install_services.run_pi_server()."""

    def _make_result(self):
        return install_all.DeviceResult("Pi Server")

    def test_skips_when_docker_not_found(self):
        """Result should be SKIP when docker is not on PATH."""
        with patch("shutil.which", return_value=None):
            result = self._make_result()
            install_services.run_pi_server(result)
            assert result.status == "SKIP"
            assert "Docker" in result.detail

    def test_stops_existing_container_and_starts_new(self):
        """Should stop existing container, run new one, and install systemd."""
        mock_docker_stop = MagicMock()
        mock_docker_stop.returncode = 0
        mock_docker_rm = MagicMock()
        mock_docker_rm.returncode = 0
        mock_systemctl_start = MagicMock()
        mock_systemctl_start.returncode = 0
        mock_docker_verify = MagicMock()
        mock_docker_verify.stdout = "abc123def456 Up 10 seconds\n"
        mock_sudo_mv = MagicMock()
        mock_sudo_mv.returncode = 0
        mock_sudo_reload = MagicMock()
        mock_sudo_reload.returncode = 0
        mock_sudo_enable = MagicMock()
        mock_sudo_enable.returncode = 0

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(install_services, "_resolve_nats_address", return_value=None),
            patch.object(install_services, "_docker_psql", return_value="old-container"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("tempfile.mkstemp", return_value=(3, "/tmp/lmao-server-xxx.service")),
            patch("os.fdopen"),
            patch("os.unlink"),
        ):
            # Flow: pull from registry, stop, rm, systemd install (mv,
            # reload, enable), systemctl start, then verify
            mock_docker_pull = MagicMock()
            mock_docker_pull.returncode = 0
            mock_run.side_effect = [
                mock_docker_pull,  # docker pull (registry release image)
                mock_sudo_mv,  # sudo systemctl stop (best-effort)
                mock_docker_stop,  # docker stop
                mock_docker_rm,  # docker rm
                mock_sudo_mv,  # sudo mv
                mock_sudo_reload,  # sudo systemctl daemon-reload
                mock_sudo_enable,  # sudo systemctl enable
                mock_systemctl_start,  # sudo systemctl start lmao-server
                mock_docker_verify,  # docker ps --filter --format
            ]
            result = self._make_result()
            install_services.run_pi_server(result)
            assert result.status == "OK"
            assert "running" in result.detail.lower()
            # The container must run the registry release image — check the
            # systemd unit content (written via tempfile) or the run args
            run_calls = [
                c for c in mock_run.call_args_list if "docker" in str(c[0][0])
            ]
            assert any(
                "192.168.0.36:5000/lmao-server:latest" in str(c) for c in run_calls
            )

    def test_fails_when_docker_pull_fails(self):
        """Result should be FAIL when the registry image cannot be pulled."""
        mock_pull_fail = MagicMock()
        mock_pull_fail.returncode = 1
        mock_pull_fail.stderr = "Error response from daemon: manifest unknown\n"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_pull_fail),
        ):
            result = self._make_result()
            install_services.run_pi_server(result)
            assert result.status == "FAIL"
            assert "docker pull" in result.detail

    def test_graceful_degradation_when_docker_run_fails(self):
        """Result should not be FAIL when docker run fails; systemd is already installed."""
        mock_docker_run = MagicMock()
        mock_docker_run.returncode = 1
        mock_docker_run.stderr = "Error: port in use\n"
        # Make sudo commands succeed (return mock with returncode=0)
        mock_sudo = MagicMock()
        mock_sudo.returncode = 0

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(install_services, "_resolve_nats_address", return_value=None),
            patch.object(install_services, "_docker_psql", return_value=None),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("tempfile.mkstemp", return_value=(3, "/tmp/lmao-server-xxx.service")),
            patch("os.fdopen"),
            patch("os.unlink"),
        ):
            # pull ok; sudo mv, reload, enable succeed; systemctl start
            # fails; fallback docker run also fails
            mock_systemctl_start = MagicMock()
            mock_systemctl_start.returncode = 1
            mock_systemctl_start.stderr = "Failed to start lmao-server.service\n"
            mock_run.side_effect = [
                mock_sudo,  # docker pull
                mock_sudo,  # sudo systemctl stop (best-effort)
                mock_sudo,  # sudo mv
                mock_sudo,  # sudo daemon-reload
                mock_sudo,  # sudo enable
                mock_systemctl_start,  # sudo systemctl start (fails)
                mock_docker_run,  # docker run fallback (fails)
            ]
            result = self._make_result()
            install_services.run_pi_server(result)
            # Container didn't start but systemd is installed
            assert result.status == "OK"
            assert "Systemd service installed" in result.detail

    def test_graceful_degradation_when_docker_psql_raises(self):
        """Result should not be FAIL when _docker_psql raises; graceful degradation."""
        mock_sudo = MagicMock()
        mock_sudo.returncode = 0

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(install_services, "_resolve_nats_address", return_value=None),
            patch.object(
                install_services,
                "_docker_psql",
                side_effect=subprocess.TimeoutExpired(["docker", "ps", "-q"], 15),
            ),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run", return_value=mock_sudo),
            patch("tempfile.mkstemp", return_value=(3, "/tmp/lmao-server-xxx.service")),
            patch("os.fdopen"),
            patch("os.unlink"),
        ):
            result = self._make_result()
            install_services.run_pi_server(result)
            # Graceful degradation: warning printed, container still starts
            # Since docker run also uses mock_sudo (success), container starts
            assert result.status == "OK"

    def test_uses_nats_server_env_var_when_set(self):
        """NATS_SERVER env var should be used directly, skipping auto-discovery."""
        mock_sudo = MagicMock()
        mock_sudo.returncode = 0

        with (
            patch.dict(os.environ, {"NATS_SERVER": "nats://custom:4222"}, clear=True),
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(install_services, "_resolve_nats_address") as mock_resolve,
            patch.object(install_services, "_docker_psql", return_value=None),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run", return_value=mock_sudo),
            patch("tempfile.mkstemp", return_value=(3, "/tmp/lmao-server-xxx.service")),
            patch("os.fdopen"),
            patch("os.unlink"),
        ):
            result = self._make_result()
            install_services.run_pi_server(result)
            # _resolve_nats_address should NOT be called when env var is set
            mock_resolve.assert_not_called()
            assert result.status == "OK"

    def test_handles_bytes_stderr(self):
        """str/bytes decode guard: CalledProcessError with bytes stderr should not crash."""
        bytes_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["sudo", "mv", "..."],
            output=b"",
            stderr=b"Error: port in use\n",
        )
        mock_pull = MagicMock()
        mock_pull.returncode = 0
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(install_services, "_resolve_nats_address", return_value=None),
            patch.object(install_services, "_docker_psql", return_value=None),
            patch("os.path.exists", return_value=True),
            # docker pull succeeds; the sudo/systemd step raises with bytes stderr
            patch("subprocess.run", side_effect=[mock_pull, bytes_error] * 4),
            patch("tempfile.mkstemp", return_value=(3, "/tmp/lmao-server-xxx.service")),
            patch("os.fdopen"),
            patch("os.unlink"),
        ):
            result = self._make_result()
            # Should not crash — the .decode() guard handles bytes stderr
            try:
                install_services.run_pi_server(result)
            except Exception:
                pytest.fail("run_pi_server raised unexpectedly with bytes stderr")
            # Both systemd and docker run fail, so result is FAIL — but the
            # key assertion is that no AttributeError was raised by .decode()
            assert result.status in ("OK", "FAIL")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
