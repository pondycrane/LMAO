"""Unit tests for lma_core.storage.DuckDbStore (mocked duckdb).

Uses ``sys.modules`` mocking (same pattern as ``test_queue.py``)
so tests run without a live DuckDB database.  All async methods
are ``@pytest.mark.asyncio``.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixture — mock duckdb module so DuckDbStore imports
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_duckdb_module():
    """Populate sys.modules with a mock for duckdb so DuckDbStore imports.

    Must be used before importing ``lma_core.storage`` so that the
    lazy ``import duckdb`` succeeds.
    """
    duckdb_mod = types.ModuleType("duckdb")

    # duckdb.connect returns a DuckDBPyConnection-like mock
    _mock_conn = MagicMock()
    _mock_conn.execute = MagicMock()
    _mock_conn.close = MagicMock()
    _mock_conn.executemany = MagicMock()
    duckdb_mod.connect = MagicMock(return_value=_mock_conn)

    sys.modules["duckdb"] = duckdb_mod

    # Remove lma_core.storage from sys.modules so it re-imports
    # with our mocked duckdb available.
    for key in list(sys.modules):
        if key.startswith("lma_core.storage"):
            del sys.modules[key]

    yield duckdb_mod, _mock_conn

    # Cleanup
    for mod in ["duckdb", "lma_core.storage"]:
        if mod in sys.modules:
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Fixture — initialized DuckDbStore ready for write / read tests
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_store(mock_duckdb_module):
    """Return a DuckDbStore that has been initialized with a mock connection."""
    from lma_core.storage import DuckDbStore  # noqa: E402

    store = DuckDbStore(name="test-store")
    store.initialize("/tmp/test.db")
    return store, mock_duckdb_module


@pytest.fixture
def duckdb_unavailable():
    """Simulate missing duckdb by raising ImportError when duckdb is imported.

    Uses ``unittest.mock.patch`` on ``builtins.__import__`` to make ``duckdb``
    unimportable regardless of whether ``duckdb`` is actually installed.
    """
    import builtins
    from unittest.mock import patch

    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "duckdb" or name.startswith("duckdb."):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _mock_import):
        saved = {}
        for mod in ["duckdb", "lma_core.storage"]:
            if mod in sys.modules:
                saved[mod] = sys.modules.pop(mod)
        for key in list(sys.modules):
            if key.startswith("lma_core.storage."):
                saved[key] = sys.modules.pop(key)
        yield
        for mod in list(saved):
            if mod in sys.modules:
                del sys.modules[mod]
        sys.modules.update(saved)


# ===================================================================
# Tests
# ===================================================================


class TestDuckDbStoreInit:
    """Initialization and lifecycle tests."""

    def test_import_error_when_duckdb_unavailable(self, duckdb_unavailable):
        """When duckdb is not installed, DuckDbStore.__init__ raises ImportError."""
        from lma_core.storage import DuckDbStore

        with pytest.raises(ImportError, match="duckdb"):
            DuckDbStore()

    def test_initialize_creates_connection(self, mock_duckdb_module):
        """initialize() should call duckdb.connect and create the schema."""
        from lma_core.storage import DuckDbStore

        duckdb_mod, mock_conn = mock_duckdb_module
        store = DuckDbStore(name="test-init")
        store.initialize("/tmp/test.db")

        duckdb_mod.connect.assert_called_once_with("/tmp/test.db", read_only=False)
        assert store._conn is not None
        assert store._db_path == "/tmp/test.db"

        # Should have executed CREATE TABLE
        assert mock_conn.execute.call_count >= 1

    def test_initialize_idempotent_same_path(self, mock_duckdb_module):
        """Calling initialize() twice with the same path should be a no-op."""
        from lma_core.storage import DuckDbStore

        duckdb_mod, mock_conn = mock_duckdb_module
        store = DuckDbStore(name="test-idem")
        store.initialize("/tmp/test.db")

        # Reset call counters
        duckdb_mod.connect.reset_mock()
        mock_conn.execute.reset_mock()

        # Second call — same path
        store.initialize("/tmp/test.db")

        # Should NOT create a new connection
        duckdb_mod.connect.assert_not_called()

    def test_initialize_switches_path(self, mock_duckdb_module):
        """Calling initialize() with a different path should close old and open new."""
        from lma_core.storage import DuckDbStore

        duckdb_mod, mock_conn = mock_duckdb_module
        store = DuckDbStore(name="test-switch")
        store.initialize("/tmp/first.db")

        # Reset mocks but make a new connection mock for the second call
        duckdb_mod.connect.reset_mock()
        second_conn = MagicMock()
        second_conn.execute = MagicMock()
        second_conn.close = MagicMock()
        duckdb_mod.connect.return_value = second_conn

        store.initialize("/tmp/second.db")

        # Old connection should have been closed
        mock_conn.close.assert_called_once()
        # New connection should have been opened
        duckdb_mod.connect.assert_called_once_with("/tmp/second.db", read_only=False)
        assert store._db_path == "/tmp/second.db"

    def test_initialize_raises_on_connect_failure(self, mock_duckdb_module):
        """initialize() should raise when duckdb.connect fails."""
        from lma_core.storage import DuckDbStore

        duckdb_mod, _ = mock_duckdb_module
        duckdb_mod.connect.side_effect = OSError("permission denied")

        store = DuckDbStore(name="test-fail")
        with pytest.raises(OSError, match="permission denied"):
            store.initialize("/tmp/bad.db")

        assert store._conn is None

    def test_initialize_raises_on_schema_failure(self, mock_duckdb_module):
        """initialize() should raise when CREATE TABLE fails after a successful connect."""
        from lma_core.storage import DuckDbStore

        duckdb_mod, mock_conn = mock_duckdb_module
        # duckdb.connect succeeds but execute(CREATE TABLE) fails
        mock_conn.execute.side_effect = RuntimeError("schema creation failed")

        store = DuckDbStore(name="test-schema-fail")
        with pytest.raises(RuntimeError, match="schema creation failed"):
            store.initialize("/tmp/test.db")

        # Connection was established before schema failure
        assert store._conn is not None

    def test_close_idempotent(self, mock_duckdb_module):
        """close() should be safe to call multiple times."""
        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="test-close-idem")
        # close() before initialize() is a no-op
        store.close()  # should not raise

        store.initialize("/tmp/test.db")
        store.close()
        # Second close should be a no-op
        store.close()  # should not raise

        assert store._conn is None
        assert store._db_path is None

    def test_close_handles_error(self, mock_duckdb_module):
        """close() should set _conn to None even if conn.close() raises."""
        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="test-close-err")
        store.initialize("/tmp/test.db")

        # Make close raise
        store._conn.close.side_effect = OSError("close failed")

        store.close()  # should not raise

        assert store._conn is None
        assert store._db_path is None


class TestDuckDbStoreWrite:
    """store_sensor_report tests."""

    @pytest.mark.asyncio
    async def test_store_before_init_raises(self, mock_duckdb_module):
        """store_sensor_report() before initialize() should raise RuntimeError."""
        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="test-no-init")
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.store_sensor_report(b"fake")

    @pytest.mark.asyncio
    async def test_store_inserts_readings(self, initialized_store):
        """store_sensor_report() should insert rows from a valid SensorReport."""
        store, (duckdb_mod, mock_conn) = initialized_store

        # Build a mock LMAOEnvelope with sensor data
        mock_envelope = MagicMock()
        mock_envelope.sensor.node_id = "node-7"
        mock_envelope.sensor.seq = 42
        mock_envelope.sensor.battery = 3.7

        reading1 = MagicMock()
        reading1.sensor_id = 1
        reading1.value = 22.5
        reading1.unit = "C"
        reading1.timestamp_ms = 1700000000000

        reading2 = MagicMock()
        reading2.sensor_id = 2
        reading2.value = 68.0
        reading2.unit = "%"
        reading2.timestamp_ms = 1700000000001

        mock_envelope.sensor.readings = [reading1, reading2]

        # Patch LMAOEnvelope to return our mock when instantiated
        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            await store.store_sensor_report(b"valid_protobuf_bytes")

        # Should have called executemany with the INSERT
        mock_conn.executemany.assert_called_once()
        call_args = mock_conn.executemany.call_args
        sql, rows = call_args[0]
        assert "INSERT INTO sensor_readings" in sql
        assert len(rows) == 2

        # Verify row values
        row0 = rows[0]
        assert row0[0] == "node-7"
        assert row0[1] == 42
        assert row0[2] == 3.7
        assert row0[3] == 1
        assert row0[4] == 22.5
        assert row0[5] == "C"
        assert row0[6] == 1700000000000

        row1 = rows[1]
        assert row1[0] == "node-7"
        assert row1[3] == 2
        assert row1[4] == 68.0
        assert row1[5] == "%"
        assert row1[6] == 1700000000001

    @pytest.mark.asyncio
    async def test_store_handles_parse_error(self, initialized_store):
        """store_sensor_report() should log a warning and raise on parse failure."""
        store, _ = initialized_store

        # Make ParseFromString raise
        mock_envelope = MagicMock()
        mock_envelope.ParseFromString.side_effect = Exception("invalid protobuf")

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            with pytest.raises(Exception, match="invalid protobuf"):
                await store.store_sensor_report(b"garbage")

    @pytest.mark.asyncio
    async def test_store_empty_readings(self, initialized_store):
        """store_sensor_report() should not call executemany when there are no readings."""
        store, (duckdb_mod, mock_conn) = initialized_store

        mock_envelope = MagicMock()
        mock_envelope.sensor.node_id = "empty-node"
        mock_envelope.sensor.seq = 1
        mock_envelope.sensor.battery = 4.0
        mock_envelope.sensor.readings = []  # empty!

        mock_conn.executemany.reset_mock()

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            await store.store_sensor_report(b"valid_bytes")

        # Should NOT have called executemany
        mock_conn.executemany.assert_not_called()


class TestDuckDbStoreQuery:
    """query tests."""

    @pytest.mark.asyncio
    async def test_query_runs_sql(self, initialized_store):
        """query() should execute SQL and return results via run_in_executor."""
        store, (duckdb_mod, mock_conn) = initialized_store

        mock_conn.execute.reset_mock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("node-1", 42),
            ("node-2", 99),
        ]

        rows = await store.query("SELECT node_id, seq FROM sensor_readings")

        mock_conn.execute.assert_called_once_with("SELECT node_id, seq FROM sensor_readings")
        assert len(rows) == 2
        assert rows[0] == ("node-1", 42)
        assert rows[1] == ("node-2", 99)

    @pytest.mark.asyncio
    async def test_query_with_params(self, initialized_store):
        """query() should pass params to execute()."""
        store, (duckdb_mod, mock_conn) = initialized_store

        mock_conn.execute.reset_mock()
        mock_conn.execute.return_value.fetchall.return_value = [("node-5",)]

        rows = await store.query(
            "SELECT node_id FROM sensor_readings WHERE seq = ?",
            params=[42],
        )

        mock_conn.execute.assert_called_once_with(
            "SELECT node_id FROM sensor_readings WHERE seq = ?", [42]
        )
        assert len(rows) == 1
        assert rows[0] == ("node-5",)

    @pytest.mark.asyncio
    async def test_query_before_init_raises(self, mock_duckdb_module):
        """query() before initialize() should raise RuntimeError."""
        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="test-no-init-query")
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.query("SELECT 1")
