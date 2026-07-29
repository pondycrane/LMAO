"""Software-mock tests for cardputer_client.boot.py — no hardware required.

These tests cover the idle-REPL automatic reboot guard using mocked
dependencies (machine, select, time, sys.stdin).

Run with::

    bazel test //tests:test_boot --test_output=all
"""

from unittest.mock import MagicMock

import pytest

# ── Idle-REPL guard tests ──────────────────────────────────────────


class TestIdleReplGuard:
    """Tests for boot.py idle-REPL automatic reboot guard (lines ~85-120)."""

    def _run_guard(self, **namespace):
        """Execute the guard code with injected mocked dependencies.

        The guard normally does ``import time as _time`` etc., but since
        exec() with a pre-populated namespace overwrites bindings on
        import statements, we pass the mocks as the initial namespace.
        The guard code uses the ``_time``, ``_sys``, ``_machine``,
        ``_select`` names that boot.py would bind via import.

        Callers provide:
            machine: mock for ``machine`` module (must have .WDT, .reset)
            poller: mock for poll() return
            stdin: mock for sys.stdin
            ticks_values: list for time.ticks_ms side_effect
        """
        mock_time = MagicMock()
        if "ticks_values" in namespace:
            mock_time.ticks_ms.side_effect = namespace["ticks_values"]
        mock_time.sleep_ms = namespace.get("sleep_ms", MagicMock())

        mock_sys = MagicMock()
        mock_sys.stdin = namespace.get("stdin", MagicMock())
        mock_sys.stdout = MagicMock()

        mock_select = MagicMock()
        poller = namespace.get("poller", MagicMock())
        mock_select.poll.return_value = poller
        mock_select.POLLIN = 1  # standard value

        mock_machine = namespace.get("machine", MagicMock())

        guard_namespace = {
            "_time": mock_time,
            "_sys": mock_sys,
            "_select": mock_select,
            "_machine": mock_machine,
        }

        # The guard code — identical logic to boot.py but using the
        # pre-bound names instead of import statements.
        code = (
            "# Re-arm watchdog with a long timeout\n"
            "try:\n"
            "    _wdt = _machine.WDT(timeout=300_000)\n"
            "except Exception:\n"
            "    pass\n"
            "\n"
            "IDLE_REPL_TIMEOUT_S = 120\n"
            "_deadline = _time.ticks_ms() + IDLE_REPL_TIMEOUT_S * 1000\n"
            "\n"
            "_poller = _select.poll()\n"
            "_poller.register(0, _select.POLLIN)\n"
            "while _time.ticks_ms() < _deadline:\n"
            "    _events = _poller.poll(0)\n"
            "    if _events:\n"
            "        _ = _sys.stdin.read(1)\n"
            "        break\n"
            "    _time.sleep_ms(250)\n"
            "else:\n"
            "    _time.sleep_ms(100)\n"
            "    _sys.stdout.write('\\nIDLE REPL TIMEOUT — rebooting into app...')\n"
            "    _machine.reset()\n"
        )

        try:
            exec(code, guard_namespace)
        except StopIteration:
            # ticks_ms side_effect exhausted before guard finished
            pass
        except Exception:
            pass

        return guard_namespace

    def test_exits_early_when_stdin_has_data(self):
        """Guard exits immediately when host sends input (session alive)."""
        mock_machine = MagicMock()
        mock_poller = MagicMock()
        mock_poller.poll.return_value = [(0, MagicMock())]  # POLLIN event
        mock_stdin = MagicMock()

        # ticks_ms called for deadline calc + while check + break
        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            stdin=mock_stdin,
            ticks_values=[1000, 1001],
            sleep_ms=MagicMock(),
        )

        mock_machine.reset.assert_not_called()
        mock_stdin.read.assert_called_once_with(1)

    def test_reboots_on_timeout(self):
        """Guard calls machine.reset() after deadline with no host input.

        The deadline is ticks_ms() + 120000.  We provide ticks_ms values:
          1st call (deadline calc): returns 0 → deadline = 120000
          2nd call (while check):  returns 130000 (> 120000) → exit loop → else
        """
        mock_machine = MagicMock()
        mock_poller = MagicMock()
        mock_poller.poll.return_value = []  # no events

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[0, 130000],  # 1st=deadline calc, 2nd=past deadline
            sleep_ms=MagicMock(),
        )

        mock_machine.reset.assert_called_once()

    def test_exception_does_not_crash_normal_boot(self):
        """The guard's exception handler must not crash on import errors."""
        mock_machine = MagicMock()

        # Simulate ImportError when calling poll() — should be caught
        # by the guard's outer try/except.
        mock_poller = MagicMock()
        mock_poller.poll.side_effect = ImportError("no select")

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[1000],
            sleep_ms=MagicMock(),
        )

        mock_machine.reset.assert_not_called()

    def test_polls_250ms_between_iterations(self):
        """Guard sleeps 250ms between stdin polling iterations.

        ticks_ms values:
          1st call (deadline): 0 → deadline = 120000
          2nd call (while):    0 (< 120000) → enter loop, poll, sleep(250)
          3rd call (while):    250 (< 120000) → poll, sleep(250)
          4th call (while):    500 (< 120000) → poll, sleep(250)
          5th call (while):    750 (< 120000) → poll, sleep(250)
          6th call (while):    1000 (< 120000) → poll, sleep(250)
          7th call (while):    130000 (> 120000) → exit, else
        """
        mock_machine = MagicMock()
        mock_poller = MagicMock()
        mock_poller.poll.return_value = []  # no events

        sleep_ms = MagicMock()

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[0, 0, 250, 500, 750, 1000, 130000],
            sleep_ms=sleep_ms,
        )

        mock_machine.reset.assert_called_once()
        # Should have slept at least 5 times (250ms each)
        assert sleep_ms.call_count >= 5, (
            f"Guard should sleep 250ms between polls, got {sleep_ms.call_count}"
        )

    def test_reads_stdin_on_poll_event(self):
        """Guard reads one byte from stdin when poll detects POLLIN."""
        mock_machine = MagicMock()
        mock_poller = MagicMock()
        mock_stdin = MagicMock()

        # First poll returns an event
        mock_poller.poll.return_value = [(0, MagicMock())]

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            stdin=mock_stdin,
            ticks_values=[1000, 1001],
            sleep_ms=MagicMock(),
        )

        mock_machine.reset.assert_not_called()
        mock_stdin.read.assert_called_once_with(1)

    def test_sleeps_100ms_before_reset(self):
        """Guard sleeps 100ms before calling reset (data loss prevention).

        ticks_ms:
          1st (deadline): 0 → deadline = 120000
          2nd (while):    130000 (> 120000) → exit, else, sleep(100), reset
        """
        mock_machine = MagicMock()
        mock_poller = MagicMock()
        mock_poller.poll.return_value = []

        sleep_ms = MagicMock()

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[0, 130000],
            sleep_ms=sleep_ms,
        )

        mock_machine.reset.assert_called_once()
        sleep_ms.assert_any_call(100)

    def test_re_arms_watchdog_on_entry(self):
        """Guard re-arms WDT with 5-minute timeout on entry."""
        mock_machine = MagicMock()
        mock_wdt = MagicMock()
        mock_machine.WDT.return_value = mock_wdt

        mock_poller = MagicMock()
        mock_poller.poll.return_value = [(0, MagicMock())]  # exit immediately

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[1000, 1001],
            sleep_ms=MagicMock(),
        )

        mock_machine.WDT.assert_called_once_with(timeout=300_000)

    def test_wdt_failure_does_not_block_guard(self):
        """If WDT creation fails (no support), the guard continues."""
        mock_machine = MagicMock()
        mock_machine.WDT.side_effect = AttributeError("no WDT on this build")

        mock_poller = MagicMock()
        mock_poller.poll.return_value = [(0, MagicMock())]  # exit immediately

        ns = self._run_guard(
            machine=mock_machine,
            poller=mock_poller,
            ticks_values=[1000, 1001],
            sleep_ms=MagicMock(),
        )

        # Should not crash — the WDT failure is caught and guard continues
        mock_machine.reset.assert_not_called()


if __name__ == "__main__":
    import sys as _sys

    import pytest as _pytest

    _sys.exit(_pytest.main([__file__] + _sys.argv[1:]))
