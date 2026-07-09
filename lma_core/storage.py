"""Persistent DuckDB storage for IoT SensorReport data.

Provides a lightweight ``DuckDbStore`` class that mirrors the existing
``lma_core`` module conventions: lazy imports with try/except,
module-level ``_logger``, and a class-based API with idempotent
initialization / close.

Requires ``duckdb`` at runtime (``pip install duckdb``).
When ``duckdb`` is absent the module logs a warning and
``DuckDbStore`` raises ``ImportError`` with a descriptive message.

Usage::

    import asyncio
    from lma_core.storage import DuckDbStore

    async def main():
        store = DuckDbStore()
        store.initialize("/data/sensors.db")

        # Store a serialized LMAOEnvelope
        await store.store_sensor_report(envelope_bytes)

        # Query stored data
        rows = await store.query("SELECT * FROM sensor_readings LIMIT 10")
        for row in rows:
            print(row)

        store.close()

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of duckdb — graceful fallback when absent
# ---------------------------------------------------------------------------

_DUCKDB_AVAILABLE = False
_DUCKDB_IMPORT_ERROR = ""

try:
    import duckdb  # noqa: F401

    _DUCKDB_AVAILABLE = True
except ImportError as exc:
    _DUCKDB_IMPORT_ERROR = (
        "duckdb is not installed. Persistent storage features will be unavailable. "
        "Install with: pip install duckdb"
    )
    _logger.warning("%s: %s", _DUCKDB_IMPORT_ERROR, exc)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_SENSOR_READINGS_TABLE = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL,
    seq INTEGER,
    battery REAL,
    sensor_id INTEGER,
    value REAL,
    unit TEXT,
    timestamp_ms INTEGER,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DuckDbStore:
    """Async-safe DuckDB persistent store for IoT sensor readings.

    Encapsulates a DuckDB database connection with idempotent
    initialization and close.  All writes are delegated to a
    thread-pool executor so the asyncio event loop is never
    blocked by synchronous DuckDB I/O.

    Typical usage::

        store = DuckDbStore()
        store.initialize("/data/sensors.db")
        await store.store_sensor_report(envelope_bytes)
        rows = await store.query("SELECT count(*) FROM sensor_readings")
        store.close()

    Parameters
    ----------
    name:
        Optional human-readable name for this store instance,
        used in log messages for easier debugging.
    """

    def __init__(self, name: str = "duckdb-store") -> None:
        if not _DUCKDB_AVAILABLE:
            raise ImportError(_DUCKDB_IMPORT_ERROR)

        self._name = name
        self._conn: Any = None  # duckdb.DuckDBPyConnection
        self._db_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, db_path: str) -> None:
        """Open (or create) a DuckDB database file and ensure the schema exists.

        Safe to call multiple times — subsequent calls are no-ops
        as long as *db_path* matches the current open database.

        Parameters
        ----------
        db_path:
            Filesystem path to the DuckDB database file, e.g.
            ``"/data/sensors.db"``.  The directory must exist;
            DuckDB will create the file if it does not exist.
        """
        if self._conn is not None:
            if self._db_path == db_path:
                _logger.debug(
                    "DuckDB store '%s' already initialized at %s",
                    self._name,
                    db_path,
                )
                return
            # Path changed — close old connection first
            _logger.info(
                "DuckDB store '%s' switching from %s to %s",
                self._name,
                self._db_path,
                db_path,
            )
            self.close()

        if not _DUCKDB_AVAILABLE:
            raise ImportError(_DUCKDB_IMPORT_ERROR)

        _logger.info("Opening DuckDB database at %s ...", db_path)
        try:
            self._conn = duckdb.connect(db_path)
            self._db_path = db_path
        except Exception:
            _logger.critical(
                "Failed to open DuckDB database at %s", db_path, exc_info=True
            )
            raise

        # Ensure schema exists
        try:
            self._conn.execute(_CREATE_SENSOR_READINGS_TABLE)
        except Exception:
            _logger.critical(
                "Failed to create schema in DuckDB database at %s",
                db_path,
                exc_info=True,
            )
            raise

        _logger.info(
            "DuckDB store '%s' initialized at %s", self._name, db_path
        )

    def close(self) -> None:
        """Close the DuckDB connection.

        Idempotent — calling close() on an already-closed store is a no-op.
        """
        if self._conn is not None:
            _logger.info("Closing DuckDB store '%s' ...", self._name)
            try:
                self._conn.close()
            except Exception:
                _logger.warning(
                    "Error closing DuckDB store '%s'", self._name, exc_info=True
                )
            finally:
                self._conn = None
                self._db_path = None
                _logger.info("DuckDB store '%s' closed.", self._name)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def store_sensor_report(self, envelope_bytes: bytes) -> None:
        """Parse a serialized LMAOEnvelope and insert each sensor reading.

        The protobuf parse happens on the calling thread; only the
        DuckDB INSERT is delegated to the executor.

        Parameters
        ----------
        envelope_bytes:
            Raw protobuf bytes from ``LMAOEnvelope.SerializeToString()``.

        Raises
        ------
        RuntimeError:
            If ``initialize()`` has not been called.
        """
        self._check_initialized()

        # Lazy import for test mocking — tests can replace
        # lma_core.LMAOEnvelope in sys.modules after storage.py
        # is imported.
        from lma_core import LMAOEnvelope  # noqa: E402

        # ── Parse the envelope ──────────────────────────────────
        envelope = LMAOEnvelope()
        try:
            envelope.ParseFromString(envelope_bytes)
        except Exception as exc:
            _logger.warning(
                "Failed to parse LMAOEnvelope (%d bytes): %s",
                len(envelope_bytes),
                exc,
            )
            raise

        sensor = envelope.sensor
        rows_to_insert: List[tuple] = []

        for reading in sensor.readings:
            rows_to_insert.append((
                sensor.node_id,
                sensor.seq,
                sensor.battery,
                reading.sensor_id,
                reading.value,
                reading.unit,
                reading.timestamp_ms,
            ))

        if not rows_to_insert:
            _logger.debug(
                "SensorReport from node '%s' has no readings — nothing to store",
                sensor.node_id,
            )
            return

        # ── Execute INSERT in a thread pool ─────────────────────
        loop = asyncio.get_event_loop()

        def _insert() -> None:
            self._conn.executemany(
                "INSERT INTO sensor_readings "
                "(node_id, seq, battery, sensor_id, value, unit, timestamp_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows_to_insert,
            )

        await loop.run_in_executor(None, _insert)

        _logger.debug(
            "Stored %d reading(s) from node '%s' (seq=%d)",
            len(rows_to_insert),
            sensor.node_id,
            sensor.seq,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def query(self, sql: str, params: Optional[List[Any]] = None) -> List[Any]:
        """Execute a read-only SQL query and return all rows.

        Parameters
        ----------
        sql:
            SQL query string (e.g. ``"SELECT * FROM sensor_readings"``).
        params:
            Optional list of positional parameters for parameterized queries.

        Returns
        -------
            List of row objects (duckdb fetches as tuples by default).

        Raises
        ------
        RuntimeError:
            If ``initialize()`` has not been called.
        """
        self._check_initialized()

        loop = asyncio.get_event_loop()

        def _query() -> List[Any]:
            if params is not None:
                return self._conn.execute(sql, params).fetchall()
            return self._conn.execute(sql).fetchall()

        rows = await loop.run_in_executor(None, _query)
        return rows

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_initialized(self) -> None:
        """Raise if the DuckDB connection has not been established."""
        if self._conn is None:
            raise RuntimeError(
                "DuckDB store not initialized. Call `store.initialize(db_path)` first."
            )
