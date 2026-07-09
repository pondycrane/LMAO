"""Unit tests for lma_core.queue.NatsQueue (mocked nats-py).

Uses ``sys.modules`` mocking (same pattern as ``conftest.py``)
so tests run without a live NATS server.  All test methods are
``@pytest.mark.asyncio`` because every public method on NatsQueue
is ``async def``.
"""

import sys
import types
from unittest.mock import MagicMock, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Fixture — set up mocks for nats and nats.js, then clean up
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nats_modules():
    """Populate sys.modules with mocks for nats-py so NatsQueue imports.

    Must be used before importing ``lma_core.queue`` so that the
    lazy ``import nats`` succeeds.
    """
    # Create the nats package and its sub-modules
    nats_mod = types.ModuleType("nats")
    nats_aio_mod = types.ModuleType("nats.aio")
    nats_aio_client_mod = types.ModuleType("nats.aio.client")
    nats_js_mod = types.ModuleType("nats.js")

    # --- nats module-level attributes ----------------------------------
    # Real nats.connect returns a nats.aio.client.Client
    nats_mod.connect = AsyncMock()

    # --- nats.aio.client.Client ---------------------------------------
    _client_cls = MagicMock()
    _client_cls.jetstream = MagicMock()
    nats_aio_client_mod.Client = _client_cls

    # --- nats.js ------------------------------------------------------
    _jetstream = MagicMock()
    # add_stream / update_stream return None in real API
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

    # Remove lma_core.queue from sys.modules so it re-imports
    # with our mocked nats available.
    for key in list(sys.modules):
        if key.startswith("lma_core.queue"):
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
    ]:
        if mod in sys.modules:
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Fixture — connected NatsQueue ready for publish / subscribe tests
# ---------------------------------------------------------------------------


@pytest.fixture
def connected_queue(mock_nats_modules):
    """Return a factory that creates a connected NatsQueue.

    Because pytest-asyncio 1.4 does not support async fixtures,
    we return a regular sync callable that the test can ``await``.
    """
    from lma_core.queue import NatsQueue  # noqa: E402 — must be after mocks

    async def _make():
        nq = NatsQueue(name="test-queue")
        await nq.connect(servers="nats://test:4222")
        # Replace internal js with our mock for fine-grained assertions
        _, _, jetstream_mock = mock_nats_modules
        nq._js = jetstream_mock
        return nq

    return _make


@pytest.fixture
def nats_unavailable():
    """Simulate missing nats-py by temporarily removing modules from sys.modules."""
    saved = {}
    for mod in ["nats", "nats.aio", "nats.aio.client", "nats.js", "nats.js.api"]:
        if mod in sys.modules:
            saved[mod] = sys.modules.pop(mod)
    for key in list(sys.modules):
        if key.startswith("lma_core.queue"):
            del sys.modules[key]
    yield
    sys.modules.update(saved)


# ===================================================================
# Tests
# ===================================================================


class TestNatsQueueConnect:
    """Connection lifecycle tests."""

    @pytest.mark.asyncio
    async def test_connect_creates_jetstream_context(self, mock_nats_modules):
        """connect() should call nats.connect and obtain a JetStream context."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="test-connect")
        await nq.connect(servers="nats://test:4222")

        nats_mod, _, _ = mock_nats_modules
        nats_mod.connect.assert_called_once()
        assert nq._nc is not None  # connected
        assert nq._js is not None

        await nq.close()

    @pytest.mark.asyncio
    async def test_connect_raises_on_failure(self, mock_nats_modules):
        """connect() should raise when nats.connect() fails."""
        from lma_core.queue import NatsQueue

        nats_mod, _, _ = mock_nats_modules
        nats_mod.connect.side_effect = OSError("connection refused")

        nq = NatsQueue(name="test-fail")
        with pytest.raises(OSError, match="connection refused"):
            await nq.connect()

    @pytest.mark.asyncio
    async def test_nats_unavailable_raises_import_error(self, nats_unavailable):
        """When nats-py is not installed, NatsQueue.__init__ raises ImportError."""
        from lma_core.queue import NatsQueue

        with pytest.raises(ImportError, match="nats-py"):
            NatsQueue()

    @pytest.mark.asyncio
    async def test_graceful_close(self, mock_nats_modules):
        """close() should drain the NATS connection."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="test-close")
        await nq.connect()
        # Replace nc with a mock that records drain calls
        mock_nc = MagicMock()
        mock_nc.drain = AsyncMock()
        nq._nc = mock_nc

        await nq.close()

        mock_nc.drain.assert_called_once()
        assert nq._nc is None
        assert nq._js is None

    @pytest.mark.asyncio
    async def test_connect_forwards_kwargs(self, mock_nats_modules):
        """connect() should forward **kwargs to nats.connect()."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="test-kwargs")
        await nq.connect(servers="nats://test:4222", token="s3kr1t")

        nats_mod, _, _ = mock_nats_modules
        nats_mod.connect.assert_called_once_with(
            servers="nats://test:4222",
            name="test-kwargs",
            token="s3kr1t",
        )
        await nq.close()

    @pytest.mark.asyncio
    async def test_close_handles_drain_error(self, mock_nats_modules):
        """close() should clear _nc/_js even if drain() raises."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="test-drain-err")
        await nq.connect()
        # Make drain raise
        mock_nc = MagicMock()
        mock_nc.drain = AsyncMock(side_effect=OSError("drain failed"))
        nq._nc = mock_nc
        nq._js = MagicMock()

        await nq.close()

        mock_nc.drain.assert_called_once()
        assert nq._nc is None
        assert nq._js is None

    @pytest.mark.asyncio
    async def test_not_connected_raises_runtime_error(self, mock_nats_modules):
        """Calling publish/subscribe before connect() should raise RuntimeError."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="test-no-connect")
        with pytest.raises(RuntimeError, match="Not connected"):
            await nq.publish("subj", b"data")

        with pytest.raises(RuntimeError, match="Not connected"):
            await nq.subscribe("subj", "dur", lambda m: None)


class TestNatsQueueStream:
    """Stream management tests."""

    @pytest.mark.asyncio
    async def test_ensure_stream_creates_stream(self, connected_queue):
        """ensure_stream() should call add_stream with correct config."""
        nq = await connected_queue()
        await nq.ensure_stream("TEST", ["test.>"])

        nq._js.add_stream.assert_called_once()
        call_kwargs = nq._js.add_stream.call_args.kwargs
        assert call_kwargs["name"] == "TEST"
        assert call_kwargs["subjects"] == ["test.>"]
        assert call_kwargs["retention"] == "limits"
        assert call_kwargs["storage"] == "file"

    @pytest.mark.asyncio
    async def test_ensure_stream_updates_existing(self, connected_queue):
        """If add_stream fails because stream exists, ensure_stream should update."""
        nq = await connected_queue()
        nq._js.add_stream.side_effect = Exception("stream name already in use")

        await nq.ensure_stream("EXISTING", ["e.>"])

        nq._js.add_stream.assert_called_once()
        nq._js.update_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_stream_raises_on_non_exists_error(self, connected_queue):
        """If add_stream fails with a non-'already exists' error, re-raise it."""
        nq = await connected_queue()
        nq._js.add_stream.side_effect = OSError("connection refused")

        with pytest.raises(OSError, match="connection refused"):
            await nq.ensure_stream("BROKEN", ["b.>"])

        nq._js.add_stream.assert_called_once()
        nq._js.update_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_stream_raises_on_both_failures(self, connected_queue):
        """If add_stream fails with 'already exists' and update_stream also fails, propagate."""
        nq = await connected_queue()
        nq._js.add_stream.side_effect = Exception("stream name already in use")
        nq._js.update_stream.side_effect = RuntimeError("update failed")

        with pytest.raises(RuntimeError, match="update failed"):
            await nq.ensure_stream("BROKEN", ["b.>"])

    @pytest.mark.asyncio
    async def test_ensure_stream_passes_overrides(self, connected_queue):
        """ensure_stream() should forward **overrides to add_stream."""
        nq = await connected_queue()
        await nq.ensure_stream("OVERRIDE", ["o.>"], max_bytes=500, num_replicas=3)

        nq._js.add_stream.assert_called_once()
        call_kwargs = nq._js.add_stream.call_args.kwargs
        assert call_kwargs["max_bytes"] == 500
        assert call_kwargs["num_replicas"] == 3


class TestNatsQueuePublish:
    """Publish tests."""

    @pytest.mark.asyncio
    async def test_publish_sends_bytes(self, connected_queue):
        """publish() should send payload to the correct subject."""
        nq = await connected_queue()
        ack = await nq.publish("sensors.temp", b'{"v":22}')

        nq._js.publish.assert_called_once_with(
            "sensors.temp", b'{"v":22}'
        )
        assert ack.seq == 1

    @pytest.mark.asyncio
    async def test_publish_empty_payload(self, connected_queue):
        """publish() should accept empty bytes."""
        nq = await connected_queue()
        ack = await nq.publish("empty", b"")

        nq._js.publish.assert_called_once_with("empty", b"")

    @pytest.mark.asyncio
    async def test_publish_rejects_large_payload(self, connected_queue):
        """publish() should raise ValueError for payloads exceeding max_payload."""
        nq = await connected_queue()
        too_large = b"x" * (NatsQueue._MAX_MSG_SIZE + 1)

        with pytest.raises(ValueError, match="Payload size"):
            await nq.publish("big", too_large)

        nq._js.publish.assert_not_called()


class TestNatsQueueSubscribe:
    """Subscribe tests."""

    async def _make_fetch_that_raises_timeout(self):
        """An async fetch that yields control before raising TimeoutError."""
        import asyncio
        await asyncio.sleep(0)
        raise TimeoutError

    @pytest.mark.asyncio
    async def test_subscribe_creates_pull_consumer(self, connected_queue):
        """subscribe() should call pull_subscribe with durable name."""
        nq = await connected_queue()
        # Make fetch an actual async function so cancellation can interrupt it
        nq._js.pull_subscribe.return_value.fetch = self._make_fetch_that_raises_timeout

        callback = AsyncMock()

        import asyncio
        task = asyncio.ensure_future(
            nq.subscribe("sensors.>", "pod-1", callback)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        nq._js.pull_subscribe.assert_called_once()
        call_kwargs = nq._js.pull_subscribe.call_args.kwargs
        assert call_kwargs["durable"] == "pod-1"

    @pytest.mark.asyncio
    async def test_subscribe_calls_callback_and_acks(self, connected_queue):
        """subscribe() should ack messages after callback returns."""
        nq = await connected_queue()
        msg = MagicMock()
        msg.data = b"hello"
        msg.ack = AsyncMock()

        import asyncio

        call_count = 0
        async def _fetch_with_msg_then_timeout(batch=1, timeout=5):
            nonlocal call_count
            await asyncio.sleep(0)
            call_count += 1
            if call_count == 1:
                return [msg]
            raise TimeoutError

        nq._js.pull_subscribe.return_value.fetch = _fetch_with_msg_then_timeout

        callback = AsyncMock()

        task = asyncio.ensure_future(
            nq.subscribe("sensors.>", "pod-1", callback)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        callback.assert_called_once_with(msg)
        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_naks_on_callback_error(self, connected_queue):
        """subscribe() should NAK when callback raises."""
        nq = await connected_queue()
        msg = MagicMock()
        msg.data = b"bad"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()

        import asyncio

        call_count = 0
        async def _fetch_with_msg_then_timeout(batch=1, timeout=5):
            nonlocal call_count
            await asyncio.sleep(0)
            call_count += 1
            if call_count == 1:
                return [msg]
            raise TimeoutError

        nq._js.pull_subscribe.return_value.fetch = _fetch_with_msg_then_timeout

        def failing_cb(m):
            raise ValueError("processing error")

        task = asyncio.ensure_future(
            nq.subscribe("err.>", "pod-err", failing_cb)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        msg.ack.assert_not_called()
        msg.nak.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_with_sync_callback(self, connected_queue):
        """subscribe() should handle sync callbacks (no await) and ACK."""
        nq = await connected_queue()
        msg = MagicMock()
        msg.data = b"sync"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()

        import asyncio

        call_count = 0

        async def _fetch_with_msg_then_timeout(batch=1, timeout=5):
            nonlocal call_count
            await asyncio.sleep(0)
            call_count += 1
            if call_count == 1:
                return [msg]
            raise TimeoutError

        nq._js.pull_subscribe.return_value.fetch = _fetch_with_msg_then_timeout

        # Sync callback — returns None, no await
        def sync_callback(m):
            pass  # sync, no return

        task = asyncio.ensure_future(
            nq.subscribe("sync.>", "pod-sync", sync_callback)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        msg.ack.assert_called_once()
        msg.nak.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribe_recovers_from_fetch_error(self, connected_queue):
        """subscribe() should retry after fetch errors and eventually process messages."""
        nq = await connected_queue()
        msg = MagicMock()
        msg.data = b"recovered"
        msg.ack = AsyncMock()

        import asyncio

        call_count = 0

        async def _fetch_fail_then_succeed(batch=1, timeout=5):
            nonlocal call_count
            await asyncio.sleep(0)
            call_count += 1
            if call_count == 1:
                raise OSError("temporary network error")
            if call_count == 2:
                return [msg]
            raise TimeoutError

        nq._js.pull_subscribe.return_value.fetch = _fetch_fail_then_succeed

        callback = AsyncMock()

        task = asyncio.ensure_future(
            nq.subscribe("recover.>", "pod-recover", callback)
        )
        await asyncio.sleep(1.5)  # enough time for backoff(1) + retry
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Callback should have been called once with the recovered message
        callback.assert_called_once_with(msg)
        msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# Module-level import for tests that reference NatsQueue class attributes
# (e.g., NatsQueue._MAX_MSG_SIZE). This must be placed AFTER the mock_nats_modules
# fixture definition so that the lazy import of nats-py succeeds with our mocks.
# Test methods inside classes use local imports (after fixtures are active).
# ---------------------------------------------------------------------------
from lma_core.queue import NatsQueue  # noqa: E402
