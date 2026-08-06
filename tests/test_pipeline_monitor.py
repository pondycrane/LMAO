"""Unit tests for pipeline_monitor.py — mocked nats-py integration.

Uses ``sys.modules`` mocking (same pattern as ``test_queue.py``)
so tests run without a live NATS server.  All test methods are
``@pytest.mark.asyncio`` because the monitor's ``main()`` is async.
"""

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

    Must be used before importing ``lma_core.queue`` and
    ``k8s-app.pipeline_monitor`` so that the lazy ``import nats`` succeeds.
    """
    # Create the nats package and its sub-modules
    nats_mod = types.ModuleType("nats")
    nats_aio_mod = types.ModuleType("nats.aio")
    nats_aio_client_mod = types.ModuleType("nats.aio.client")
    nats_js_mod = types.ModuleType("nats.js")

    # --- nats module-level attributes ----------------------------------
    nats_mod.connect = AsyncMock()

    # --- nats.aio.client.Client ---------------------------------------
    _client_cls = MagicMock()
    _client_cls.jetstream = MagicMock()
    nats_aio_client_mod.Client = _client_cls

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

    # --- nats.js.api (needed for StreamConfig namespace) ---------------
    nats_js_api_mod = types.ModuleType("nats.js.api")
    nats_js_mod.api = nats_js_api_mod

    # Wire up
    sys.modules["nats"] = nats_mod
    sys.modules["nats.aio"] = nats_aio_mod
    sys.modules["nats.aio.client"] = nats_aio_client_mod
    sys.modules["nats.js"] = nats_js_mod
    sys.modules["nats.js.api"] = nats_js_api_mod

    # Remove cached imports so they re-import with our mocks
    for key in list(sys.modules):
        if key.startswith("lma_core.queue") or key.startswith("k8s-app.pipeline_monitor"):
            del sys.modules[key]

    yield nats_mod, nats_js_mod, _jetstream

    # Cleanup
    for mod in [
        "nats",
        "nats.aio",
        "nats.aio.client",
        "nats.js",
        "nats.js.api",
        "lma_core.queue",
        "k8s-app.pipeline_monitor",
    ]:
        if mod in sys.modules:
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Helpers — build mock stream_info results
# ---------------------------------------------------------------------------


def _stream_info_mock(last_ts):
    """Return an AsyncMock that simulates stream_info() returning a StreamInfo
    object whose ``state.last_ts`` is *last_ts* (datetime or None)."""
    state = types.SimpleNamespace(last_ts=last_ts)
    stream_info = types.SimpleNamespace(state=state)
    mock = AsyncMock(return_value=stream_info)
    return mock


def _make_monitor_main(monkeypatch, nats_server="nats://test:4222", threshold="3"):
    """Import and return the monitor's main() coroutine with env vars set."""
    monkeypatch.setenv("NATS_SERVER", nats_server)
    monkeypatch.setenv("SILENCE_THRESHOLD_HOURS", threshold)
    # Clear cached imports so the monitor module picks up our mocks
    for key in list(sys.modules):
        if key.startswith("k8s-app.pipeline_monitor") or key.startswith("lma_core.queue"):
            del sys.modules[key]
    import k8s_app.pipeline_monitor as monitor

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

    @pytest.mark.asyncio
    async def test_silence_detected_when_last_ts_too_old(self, mock_nats_modules, monkeypatch, caplog):
        """Exit 1 + ERROR log when last message is older than threshold."""
        import logging

        _, _, jetstream = mock_nats_modules

        old = datetime.now(timezone.utc) - timedelta(hours=5)
        jetstream.stream_info = _stream_info_mock(old)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with caplog.at_level(logging.ERROR, logger="k8s_app.pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1
        assert "PIPELINE SILENCE" in caplog.text
        assert "5" in caplog.text  # gap hours approx 5

    @pytest.mark.asyncio
    async def test_alert_when_stream_empty(self, mock_nats_modules, monkeypatch, caplog):
        """Exit 1 + ERROR log when stream has never received a message."""
        import logging

        _, _, jetstream = mock_nats_modules

        jetstream.stream_info = _stream_info_mock(None)  # never received

        main_fn = _make_monitor_main(monkeypatch)

        with caplog.at_level(logging.ERROR, logger="k8s_app.pipeline_monitor"):
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
    async def test_threshold_exactly_at_boundary(self, mock_nats_modules, monkeypatch):
        """Exit 0 when last message age equals threshold exactly."""
        _, _, jetstream = mock_nats_modules

        exactly_3h = datetime.now(timezone.utc) - timedelta(hours=3)
        jetstream.stream_info = _stream_info_mock(exactly_3h)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        # The gap is exactly 3 hours — not greater, so should be OK
        assert exc_info.value.code == 0

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
        """Exit 0 when last_ts is naive-datetime (no tzinfo) — treated as UTC."""
        _, _, jetstream = mock_nats_modules

        # Naive datetime — should be treated as UTC
        recent = datetime.now(timezone.utc) - timedelta(minutes=15)
        jetstream.stream_info = _stream_info_mock(recent)

        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with pytest.raises(SystemExit) as exc_info:
            await main_fn()

        assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_alert_published_on_silence(self, mock_nats_modules, monkeypatch, caplog):
        """Alert is published to ALERT_SUBJECT when silence is detected."""
        import logging

        _, _, jetstream = mock_nats_modules

        old = datetime.now(timezone.utc) - timedelta(hours=10)
        jetstream.stream_info = _stream_info_mock(old)

        monkeypatch.setenv("ALERT_SUBJECT", "lmao.alerts.silence")
        main_fn = _make_monitor_main(monkeypatch, threshold="3")

        with caplog.at_level(logging.INFO, logger="k8s_app.pipeline_monitor"):
            with pytest.raises(SystemExit) as exc_info:
                await main_fn()

        assert exc_info.value.code == 1
        # publish should have been called on the alert subject
        jetstream.publish.assert_called()
        call_args = jetstream.publish.call_args
        assert call_args[0][0] == "lmao.alerts.silence"
        assert b"PIPELINE SILENCE" in call_args[0][1]
