"""Unit tests for pipeline_monitor.py — mocked nats-py integration.

Loads ``k8s-app/pipeline_monitor.py`` via ``importlib`` (same pattern as
``test_iot_ingest.py``) because the directory name ``k8s-app`` has a hyphen
and cannot be imported with a regular ``import k8s_app...`` statement.

Uses ``sys.modules`` mocking (same pattern as ``test_queue.py``)
so tests run without a live NATS server.  All test methods are
``@pytest.mark.asyncio`` because the monitor's ``main()`` is async.
"""

import importlib.util
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixture — set up mocks for nats and nats.js, then clean up
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nats_modules():
    """Populate sys.modules with mocks for nats-py so NatsQueue imports.

    Must be used before importing ``lma_core.queue`` so that the lazy
    ``import nats`` succeeds.  Correctly wires ``nats.connect`` →
    Client → jetstream → ``_jetstream`` so that ``nq._js`` resolves
    to the mock the tests configure.
    """
    # Create the nats package and its sub-modules
    nats_mod = types.ModuleType("nats")
    nats_aio_mod = types.ModuleType("nats.aio")
    nats_aio_client_mod = types.ModuleType("nats.aio.client")
    nats_js_mod = types.ModuleType("nats.js")

    # --- nats.js.api (needed for StreamConfig namespace) ---------------
    nats_js_api_mod = types.ModuleType("nats.js.api")
    nats_js_mod.api = nats_js_api_mod

    # --- nats.js ------------------------------------------------------
    _jetstream = MagicMock()
    _jetstream.add_stream = AsyncMock()
    _jetstream.update_stream = AsyncMock()

    # publish returns a PubAck-like object with a .seq
    _puback = MagicMock()
    _puback.seq = 1
    _jetstream.publish = AsyncMock(return_value=_puback)

    # pull_subscribe returns a PullSubscription
    _psub = MagicMock()
    _psub.fetch = AsyncMock()
    _jetstream.pull_subscribe = AsyncMock(return_value=_psub)

    nats_js_mod.JetStreamContext = MagicMock()

    # --- nats.aio.client.Client ---------------------------------------
    _client_cls = MagicMock()
    # Wire jetstream() return value so nq._js resolves to _jetstream.
    _client_cls.jetstream = MagicMock(return_value=_jetstream)
    nats_aio_client_mod.Client = _client_cls

    # --- nats module-level connect() ----------------------------------
    # connect() must return the Client instance so that NatsQueue.connect
    # sets self._nc = <Client instance> and self._js = _nc.jetstream().
    # The fixture builds a fresh Client mock on every call; each test
    # creates its own via _make_monitor_main which re-executes the module.
    nats_mod.connect = AsyncMock(return_value=_client_cls)

    # Wire up
    sys.modules["nats"] = nats_mod
    sys.modules["nats.aio"] = nats_aio_mod
    sys.modules["nats.aio.client"] = nats_aio_client_mod
    sys.modules["nats.js"] = nats_js_mod
    sys.modules["nats.js.api"] = nats_js_api_mod

    yield nats_mod, nats_js_mod, _jetstream

    # Cleanup
    for mod in [
        "nats",
        "nats.aio",
        "nats.aio.client",
        "nats.js",
        "nats.js.api",
        "lma_core.queue",
    ]:
        if mod in sys.modules:
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Helpers — build mock stream_info results and load the monitor module
# ---------------------------------------------------------------------------


def _stream_info_mock(last_ts):
    """Return an AsyncMock that simulates stream_info() returning a StreamInfo
    object whose ``state.last_ts`` is *last_ts* (datetime or None)."""
    state = types.SimpleNamespace(last_ts=last_ts)
    stream_info = types.SimpleNamespace(state=state)
    mock = AsyncMock(return_value=stream_info)
    return mock


def _load_monitor():
    """Load k8s-app/pipeline_monitor.py via importlib because the directory
    name has a hyphen and cannot be imported with a regular
    ``import k8s_app.pipeline_monitor`` statement (same pattern as
    ``test_iot_ingest.py``)."""
    # Clear cached lma_core imports so they pick up our mocks.
    for key in list(sys.modules):
        if key.startswith("lma_core"):
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location("pipeline_monitor", "k8s-app/pipeline_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_monitor_main(monkeypatch, nats_server="nats://test:4222", threshold="3"):
    """Import and return the monitor's main() coroutine with env vars set."""
    monkeypatch.setenv("NATS_SERVER", nats_server)
    monkeypatch.setenv("SILENCE_THRESHOLD_HOURS", threshold)
    monitor = _load_monitor()
    return monitor.main


# ===================================================================
# Tests
# ===================================================================


class TestPipelineMonitor:
    """Pipeline silence monitor tests with mocked NATS."""

    @pytest.mark.asyncio
    async def test_ok_when_recent_messages(self, mock_nats_modules, monkeypatch):
        """Exit 0 when last message is within threshold."""
        _, _, jetstream = mock_nats_modules

        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        jetstream.stream_info = _stream_info_mock(recent)

        main_fn = _make_monitor_main(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 0
        # Verify the stream query actually went to our configured mock.
        jetstream.stream_info.assert_awaited_once_with("LMAO_MESSAGES")

    @pytest.mark.asyncio
    async def test_silence_detected_when_last_ts_too_old(
        self, mock_nats_modules, monkeypatch, caplog
    ):
        """Exit 1 + ERROR log when last message is older than threshold."""
        _, _, jetstream = mock_nats_modules

        old = datetime.now(timezone.utc) - timedelta(hours=5)
        jetstream.stream_info = _stream_info_mock(old)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with caplog.at_level(logging.ERROR, logger="pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1
        assert "PIPELINE SILENCE" in caplog.text
        assert "5" in caplog.text  # gap hours approx 5

    @pytest.mark.asyncio
    async def test_alert_when_stream_empty(self, mock_nats_modules, monkeypatch, caplog):
        """Exit 1 + ERROR log when stream has never received a message."""
        _, _, jetstream = mock_nats_modules

        jetstream.stream_info = _stream_info_mock(None)  # never received

        main_fn = _make_monitor_main(monkeypatch)

        with caplog.at_level(logging.ERROR, logger="pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1
        assert "never received a message" in caplog.text

    @pytest.mark.asyncio
    async def test_nats_unreachable(self, mock_nats_modules, monkeypatch):
        """Exit 1 when NATS connect fails."""
        import nats

        nats.connect = AsyncMock(side_effect=OSError("connection refused"))

        main_fn = _make_monitor_main(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_threshold_near_but_within(self, mock_nats_modules, monkeypatch):
        """Exit 0 when last message age is comfortably within threshold.

        Uses a value safely inside the threshold (e.g. 2h59m for a 3h
        threshold) so the strict ``>`` comparison is deterministic —
        no timing-race on construct-vs-now execution.
        """
        _, _, jetstream = mock_nats_modules

        within = datetime.now(timezone.utc) - timedelta(hours=2, minutes=59)
        jetstream.stream_info = _stream_info_mock(within)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_threshold_slightly_over(self, mock_nats_modules, monkeypatch):
        """Exit 1 when last message age is just over threshold."""
        _, _, jetstream = mock_nats_modules

        over = datetime.now(timezone.utc) - timedelta(hours=3, minutes=1)
        jetstream.stream_info = _stream_info_mock(over)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_invalid_threshold_exits_early(self, mock_nats_modules, monkeypatch):
        """Exit 1 when SILENCE_THRESHOLD_HOURS is non-numeric."""
        main_fn = _make_monitor_main(monkeypatch, threshold="not-a-number")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_nonzero_threshold_exits_early(self, mock_nats_modules, monkeypatch):
        """Exit 1 when SILENCE_THRESHOLD_HOURS is negative or zero."""
        main_fn = _make_monitor_main(monkeypatch, threshold="-1")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_naive_last_ts_handled(self, mock_nats_modules, monkeypatch):
        """Exit 0 when last_ts is truly naive (no tzinfo) — treated as UTC."""
        _, _, jetstream = mock_nats_modules

        # Build an AWARE datetime, then strip tzinfo so it is truly naive.
        aware = datetime.now(timezone.utc) - timedelta(minutes=15)
        naive = aware.replace(tzinfo=None)
        jetstream.stream_info = _stream_info_mock(naive)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_alert_published_on_silence(self, mock_nats_modules, monkeypatch, caplog):
        """Alert is published to ALERT_SUBJECT when silence is detected."""
        _, _, jetstream = mock_nats_modules

        old = datetime.now(timezone.utc) - timedelta(hours=10)
        jetstream.stream_info = _stream_info_mock(old)

        monkeypatch.setenv("ALERT_SUBJECT", "lmao.alerts.silence")
        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with caplog.at_level(logging.INFO, logger="pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1
        # publish should have been called on the alert subject
        jetstream.publish.assert_called()
        call_args = jetstream.publish.call_args
        assert call_args[0][0] == "lmao.alerts.silence"
        assert b"PIPELINE SILENCE" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_alert_publish_failure_still_exits_1(
        self, mock_nats_modules, monkeypatch, caplog
    ):
        """Exit 1 on silence even when alert publish fails (best-effort contract).

        The alert-delivery failure is logged at ERROR but must NOT change
        the exit code — silence is still reported as exit 1.
        """
        _, _, jetstream = mock_nats_modules

        old = datetime.now(timezone.utc) - timedelta(hours=10)
        jetstream.stream_info = _stream_info_mock(old)
        # Simulate alert delivery failure
        jetstream.publish = AsyncMock(side_effect=RuntimeError("nats down"))

        monkeypatch.setenv("ALERT_SUBJECT", "lmao.alerts.silence")
        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with caplog.at_level(logging.ERROR, logger="pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1  # silence still reported
        assert "Alert delivery FAILED" in caplog.text
