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
from typing import Any

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
# Schema registry — maps protobuf oneof field number to table metadata
# ---------------------------------------------------------------------------

_CREATE_SENSOR_READINGS_TABLE = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    node_id TEXT NOT NULL,
    seq INTEGER,
    battery REAL,
    sensor_id INTEGER,
    value REAL,
    unit TEXT,
    timestamp_ms BIGINT,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_TEXT_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS text_messages (
    node_id TEXT NOT NULL,
    content TEXT,
    timestamp_ms BIGINT,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_COMMAND_ACKS_TABLE = """
CREATE TABLE IF NOT EXISTS command_acks (
    cmd_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    success BOOLEAN,
    msg TEXT,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_INSERT_SENSOR_READINGS = (
    "INSERT INTO sensor_readings "
    "(node_id, seq, battery, sensor_id, value, unit, timestamp_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_TEXT_MESSAGES = (
    "INSERT INTO text_messages (node_id, content, timestamp_ms) VALUES (?, ?, ?)"
)

_INSERT_COMMAND_ACKS = (
    "INSERT INTO command_acks (cmd_id, node_id, success, msg) VALUES (?, ?, ?, ?)"
)

# Registry keyed by protobuf field number (matches envelope.WhichOneof results via
# reverse lookup in _ONEOF_NAME_TO_FIELD).
_SCHEMA_REGISTRY: dict[int, dict[str, Any]] = {
    10: {  # sensor
        "table": "sensor_readings",
        "ddl": _CREATE_SENSOR_READINGS_TABLE,
        "insert_sql": _INSERT_SENSOR_READINGS,
        "extract": "_extract_sensor_rows",
    },
    20: {  # text
        "table": "text_messages",
        "ddl": _CREATE_TEXT_MESSAGES_TABLE,
        "insert_sql": _INSERT_TEXT_MESSAGES,
        "extract": "_extract_text_rows",
    },
    12: {  # ack
        "table": "command_acks",
        "ddl": _CREATE_COMMAND_ACKS_TABLE,
        "insert_sql": _INSERT_COMMAND_ACKS,
        "extract": "_extract_ack_rows",
    },
}

# Reverse mapping from WhichOneof("payload") string to field number.
_ONEOF_NAME_TO_FIELD: dict[str, int] = {
    "sensor": 10,
    "text": 20,
    "ack": 12,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DuckDbStore:
    """Async-safe DuckDB persistent store for IoT messages.

    Encapsulates a DuckDB database connection with idempotent
    initialization and close.  All database operations are delegated to a
    thread-pool executor so the asyncio event loop is never
    blocked by synchronous DuckDB I/O.

    The store dispatches incoming ``LMAOEnvelope`` messages to
    multiple tables via ``store_envelope()``.  The schema registry
    maps protobuf oneof field numbers (10=sensor, 20=text, 12=ack)
    to registered tables.

    Typical usage::

        store = DuckDbStore()
        store.initialize("/data/sensors.db")
        await store.store_envelope(envelope_bytes)
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
        self._db_path: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, db_path: str, read_only: bool = False) -> None:
        """Open (or create) a DuckDB database file and ensure the schema exists.

        Safe to call multiple times — subsequent calls are no-ops
        as long as *db_path* matches the current open database.

        Parameters
        ----------
        db_path:
            Filesystem path to the DuckDB database file, e.g.
            ``"/data/sensors.db"``.  The directory must exist;
            DuckDB will create the file if it does not exist.
        read_only:
            If True, open the database in read-only mode.
            Writes will be rejected by DuckDB at the connection level.
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

        _logger.info("Opening DuckDB database at %s (read_only=%s) ...", db_path, read_only)
        try:
            self._conn = duckdb.connect(db_path, read_only=read_only)
            self._db_path = db_path
        except Exception:
            _logger.critical("Failed to open DuckDB database at %s", db_path, exc_info=True)
            raise

        # Ensure schema exists — iterate the registry so every
        # registered table is created (CREATE TABLE IF NOT EXISTS).
        try:
            for entry in _SCHEMA_REGISTRY.values():
                self._conn.execute(entry["ddl"])
        except Exception:
            _logger.critical(
                "Failed to create schema in DuckDB database at %s",
                db_path,
                exc_info=True,
            )
            raise

        _logger.info("DuckDB store '%s' initialized at %s", self._name, db_path)

    def close(self) -> None:
        """Close the DuckDB connection.

        Idempotent — calling close() on an already-closed store is a no-op.
        """
        if self._conn is not None:
            _logger.info("Closing DuckDB store '%s' ...", self._name)
            try:
                self._conn.close()
            except Exception:
                _logger.warning("Error closing DuckDB store '%s'", self._name, exc_info=True)
            finally:
                self._conn = None
                self._db_path = None
                _logger.info("DuckDB store '%s' closed.", self._name)

    # ------------------------------------------------------------------
    # Connection access (for query API)
    # ------------------------------------------------------------------

    def get_connection(self) -> Any:
        """Return the raw DuckDB connection.

        Callers should create their own cursors via
        ``conn.cursor()`` rather than using ``conn.execute()``
        directly — this avoids serialising on the writer's
        default cursor.  DuckDB connections and cursors are
        thread-safe within a single process.

        Returns
        -------
            The ``duckdb.DuckDBPyConnection`` instance.

        Raises
        ------
        RuntimeError:
            If ``initialize()`` has not been called.
        """
        self._check_initialized()
        return self._conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def store_envelope(self, envelope_bytes: bytes) -> None:
        """Parse a serialized LMAOEnvelope and dispatch to the correct table.

        Uses the schema registry to map the protobuf oneof type
        to a target table.  Unknown or unset oneof types are
        silently skipped (logged at DEBUG level).

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
        assert LMAOEnvelope is not None  # import re-raises on failure
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

        oneof_name = envelope.WhichOneof("payload")
        if oneof_name is None:
            _logger.debug("Envelope has no payload set — nothing to store")
            return

        field_num = _ONEOF_NAME_TO_FIELD.get(oneof_name)
        if field_num is None:
            _logger.debug("Unknown oneof type '%s' — skipping", oneof_name)
            return

        entry = _SCHEMA_REGISTRY[field_num]
        extract_method = getattr(self, entry["extract"])
        rows_to_insert: list[tuple] = extract_method(envelope)

        if not rows_to_insert:
            _logger.debug(
                "No rows to insert for %s envelope — nothing to store",
                entry["table"],
            )
            return

        # ── Execute INSERT in a thread pool ─────────────────────
        loop = asyncio.get_event_loop()

        def _insert() -> None:
            self._conn.executemany(entry["insert_sql"], rows_to_insert)

        await loop.run_in_executor(None, _insert)

        _logger.debug(
            "Stored %d row(s) into %s",
            len(rows_to_insert),
            entry["table"],
        )

    async def store_sensor_report(self, envelope_bytes: bytes) -> None:
        """Backward-compatible wrapper — delegates to :meth:`store_envelope`.

        Existing callers that only pass SensorReport envelopes do
        not need to change; the schema registry dispatches to the
        ``sensor_readings`` table as before.

        Parameters
        ----------
        envelope_bytes:
            Raw protobuf bytes from ``LMAOEnvelope.SerializeToString()``.
        """
        await self.store_envelope(envelope_bytes)

    # ------------------------------------------------------------------
    # Private — row extractors (one per registered message type)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_sensor_rows(envelope: Any) -> list[tuple]:
        """Extract sensor reading rows from a ``SensorReport`` envelope."""
        sensor = envelope.sensor
        rows: list[tuple] = []
        for reading in sensor.readings:
            rows.append(
                (
                    sensor.node_id,
                    sensor.seq,
                    sensor.battery,
                    reading.sensor_id,
                    reading.value,
                    reading.unit,
                    reading.timestamp_ms,
                )
            )
        return rows

    @staticmethod
    def _extract_text_rows(envelope: Any) -> list[tuple]:
        """Extract a text message row from a ``TextMessage`` envelope.

        The proto field is ``timestamp``; we store it as
        ``timestamp_ms`` in the DB column.
        """
        text = envelope.text
        return [
            (
                text.node_id,
                text.content,
                text.timestamp,  # proto field → DB column timestamp_ms
            )
        ]

    @staticmethod
    def _extract_ack_rows(envelope: Any) -> list[tuple]:
        """Extract a command-ack row from a ``CommandAck`` envelope.

        The proto field is ``message``; we store it as ``msg``
        in the DB column.
        """
        ack = envelope.ack
        return [
            (
                ack.cmd_id,
                ack.node_id,
                ack.success,
                ack.message,  # proto field → DB column msg
            )
        ]

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def query(self, sql: str, params: list[Any] | None = None) -> list[Any]:
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

        def _query() -> list[Any]:
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
