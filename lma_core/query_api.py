"""Embedded HTTP query API for DuckDB stored data.

Provides a lightweight ``aiohttp`` HTTP server that runs alongside
the NATS subscribe loop and exposes a read-only SQL query interface
on port 8080.  Each query uses its own DuckDB cursor derived from the
store's shared connection so reads never serialise against the
writer path.

SQL guard
---------
The ``_validate_readonly_sql`` function uses ``sqlparse`` to
enforce read-only queries: only SELECT / WITH / EXPLAIN / SHOW /
DESCRIBE statements are accepted.  Multi-statement queries and
DML / DDL / plugin-install statements are rejected.

Usage::

    from lma_core.query_api import start_query_server
    runner = await start_query_server(store, port=8080)
    # ... application runs ...
    await runner.cleanup()

Endpoints
---------

    POST /query        Run a read-only SQL query; JSON body ``{"sql": "..."}``
    GET  /tables       List registered tables with row counts
    GET  /schema/{t}   Column metadata for table *t*
    GET  /healthz      K8s readiness probe — returns 200 OK
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------

_AIOHTTP_AVAILABLE = False
_AIOHTTP_IMPORT_ERROR = ""

try:
    import aiohttp.web  # noqa: F401

    _AIOHTTP_AVAILABLE = True
except ImportError as exc:
    _AIOHTTP_IMPORT_ERROR = (
        "aiohttp is not installed. Query API will be unavailable. Install with: pip install aiohttp"
    )
    _logger.warning("%s: %s", _AIOHTTP_IMPORT_ERROR, exc)

_SQLPARSE_AVAILABLE = False
_SQLPARSE_IMPORT_ERROR = ""

try:
    import sqlparse  # noqa: F401

    _SQLPARSE_AVAILABLE = True
except ImportError as exc:
    _SQLPARSE_IMPORT_ERROR = (
        "sqlparse is not installed. SQL guard will be unavailable. "
        "Install with: pip install sqlparse"
    )
    _logger.warning("%s: %s", _SQLPARSE_IMPORT_ERROR, exc)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Statement types accepted by the read-only guard.
# UNKNOWN is included because sqlparse (0.5.3) returns UNKNOWN for
# EXPLAIN / SHOW / DESCRIBE / DESC — we accept them only when the
# token-level blacklist pass (below) confirms no DML/DDL keywords.
_ACCEPTED_STATEMENT_TYPES: frozenset[str] = frozenset(
    {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "UNKNOWN"}
)

# Keywords rejected by the defense-in-depth blacklist scan.
# These are matched as whole words (case-insensitive) against the
# normalized SQL string.  The AST-level type whitelist above already
# catches most cases; this scan is defense-in-depth against CTEs or
# sub-statements that might try to smuggle DML.
_BLOCKED_KEYWORDS: list[str] = [
    "ATTACH",
    "COPY",
    "EXPORT",
    "INSTALL",
    "LOAD",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "PRAGMA",
]


# ---------------------------------------------------------------------------
# SQL guard
# ---------------------------------------------------------------------------


def _validate_readonly_sql(sql: str) -> None:
    """Validate that *sql* is a single read-only statement.

    Raises ``ValueError`` with a descriptive message if the SQL is
    empty, contains multiple statements, or uses a disallowed
    statement type / keyword.

    Parameters
    ----------
    sql:
        Raw SQL string from the request body.
    """
    if not _SQLPARSE_AVAILABLE:
        raise RuntimeError(_SQLPARSE_IMPORT_ERROR)

    stripped = sql.strip()
    if not stripped:
        raise ValueError("No SQL statement provided.")

    # Parse with sqlparse — handles comments, quoted strings, etc.
    import sqlparse
    from sqlparse.tokens import Comment, Token

    parsed = sqlparse.parse(sql)
    # Filter out empty / whitespace-only statements.
    statements = [s for s in parsed if s.tokens and str(s).strip()]

    if len(statements) == 0:
        raise ValueError("No SQL statement provided.")
    if len(statements) > 1:
        raise ValueError("Multiple statements not allowed.")

    stmt = statements[0]
    stmt_type = stmt.get_type()

    # Whitelist check — statement type must be read-only.
    if stmt_type not in _ACCEPTED_STATEMENT_TYPES:
        raise ValueError(f"Statement type '{stmt_type}' is not allowed (read-only only).")

    # Defense-in-depth: scan tokens (not raw text) for blocked DML/DDL
    # keywords.  Walking the flattened token stream lets us skip keywords
    # that appear inside comments or string literals — the raw-text regex
    # scan used previously would over-reject legitimate SELECTs whose
    # literals merely mention a blocked word.
    blocked_set: frozenset[str] = frozenset(k.upper() for k in _BLOCKED_KEYWORDS)
    _string_types = (Token.Literal.String, Token.Literal.String.Single)
    for token in stmt.flatten():
        ttype = token.ttype
        # Skip tokens inside comments and string literals.
        if ttype is Comment or ttype in _string_types:
            continue
        word = token.value.upper().strip()
        if word in blocked_set:
            raise ValueError(
                "Query contains disallowed keywords"
                " (INSERT/DROP/CREATE/...)."
            )


# ---------------------------------------------------------------------------
# aiohttp application factory
# ---------------------------------------------------------------------------


def create_app(
    store: Any,
    max_rows: int = 1000,
    query_timeout: float = 10.0,
) -> Any:  # aiohttp.web.Application
    """Create an aiohttp Application with query endpoints.

    Parameters
    ----------
    store:
        An initialized ``DuckDbStore`` instance.
    max_rows:
        Maximum rows to return per query.  A ``LIMIT {max_rows}``
        clause is appended to queries that do not already contain
        a LIMIT.
    query_timeout:
        Per-query timeout in seconds (DuckDB query timeout is not
        natively supported, so this is enforced at the HTTP level
        via ``asyncio.wait_for``).
    """

    if not _AIOHTTP_AVAILABLE:
        raise RuntimeError(_AIOHTTP_IMPORT_ERROR)

    import aiohttp.web

    routes = aiohttp.web.RouteTableDef()

    # ------------------------------------------------------------------
    # GET /healthz
    # ------------------------------------------------------------------

    @routes.get("/healthz")
    async def healthz(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"status": "ok"})

    # ------------------------------------------------------------------
    # POST /query
    # ------------------------------------------------------------------

    @routes.post("/query")
    async def query_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            body = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON body."}, status=400)

        sql_raw = body.get("sql", "") if isinstance(body, dict) else ""
        if not sql_raw:
            return aiohttp.web.json_response(
                {"error": "Missing 'sql' field in request body."}, status=400
            )

        # Guard
        try:
            _validate_readonly_sql(sql_raw)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

        # Strip trailing semicolons / whitespace so the appended LIMIT
        # clause is valid syntax.
        sql_stripped = sql_raw.rstrip("; \t\r\n")

        # Run query in executor with its own cursor
        loop = asyncio.get_event_loop()

        def _run_query() -> dict:
            conn = store.get_connection()
            cursor = conn.cursor()
            try:
                # Server-side authoritative row cap: always bound the DB
                # work with LIMIT max_rows+1 so we can detect over-cap and
                # truncate in application code.  This closes the bypass
                # where a user-supplied LIMIT larger than max_rows was
                # silently honored.
                cursor.execute(f"{sql_stripped} LIMIT {max_rows + 1}")
                rows = cursor.fetchall()
                over_cap = len(rows) > max_rows
                rows = rows[:max_rows]
                cols = [desc[0] for desc in (cursor.description or [])]
                return {
                    "columns": cols,
                    "rows": [list(row) for row in rows],
                    "row_count": len(rows),
                    "truncated": over_cap,
                }
            finally:
                cursor.close()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_query),
                timeout=query_timeout,
            )
            try:
                return aiohttp.web.json_response(result)
            except (TypeError, ValueError) as exc:
                _logger.warning("Query result not JSON-serializable: %s", exc, exc_info=True)
                return aiohttp.web.json_response(
                    {"error": "Query produced a non-JSON-serializable result."}, status=500
                )
        except asyncio.TimeoutError:
            # NOTE (POC): the executor thread may still be running the query
            # on the shared connection.  Keep query_timeout low and cap rows
            # so abandoned work is bounded.
            _logger.warning("Query timed out after %.1fs: %s", query_timeout, sql_stripped)
            return aiohttp.web.json_response({"error": "Query timed out."}, status=504)
        except Exception as exc:
            # Distinguish user SQL errors (400) from infrastructure failures (500).
            # duckdb.Error covers syntax, type errors, missing tables etc.
            # ValueError covers guard rejections (re-raised from _run_query).
            try:
                import duckdb

                if isinstance(exc, duckdb.Error):
                    _logger.info("User SQL rejected: %s", exc)
                    return aiohttp.web.json_response({"error": "Invalid SQL query."}, status=400)
            except ImportError:
                pass
            _logger.exception("Internal query failure")
            return aiohttp.web.json_response({"error": "Internal query error."}, status=500)

    # ------------------------------------------------------------------
    # GET /tables
    # ------------------------------------------------------------------

    @routes.get("/tables")
    async def tables_handler(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        loop = asyncio.get_event_loop()

        def _list_tables() -> list[dict]:
            conn = store.get_connection()
            cursor = conn.cursor()
            try:
                # List all user tables from information_schema.
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
                table_names = [row[0] for row in cursor.fetchall()]

                result: list[dict] = []
                for tname in table_names:
                    cursor.execute(f'SELECT count(*) FROM "{tname}"')
                    cnt = cursor.fetchone()[0]
                    result.append({"table": tname, "row_count": cnt})
                return result
            finally:
                cursor.close()

        try:
            tables = await loop.run_in_executor(None, _list_tables)
            return aiohttp.web.json_response(tables)
        except Exception as exc:
            _logger.warning("Tables query failed: %s", exc)
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # GET /schema/{table}
    # ------------------------------------------------------------------

    @routes.get("/schema/{table}")
    async def schema_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        table_name = request.match_info.get("table", "")
        if not table_name:
            return aiohttp.web.json_response({"error": "Table name required in path."}, status=400)

        loop = asyncio.get_event_loop()

        def _describe_table() -> list[dict]:
            conn = store.get_connection()
            cursor = conn.cursor()
            try:
                # Check table exists
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
                    "AND table_name = ?",
                    [table_name],
                )
                if not cursor.fetchone():
                    raise LookupError(f"Table '{table_name}' not found.")

                cursor.execute(
                    "SELECT column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = ? "
                    "ORDER BY ordinal_position",
                    [table_name],
                )
                return [{"name": row[0], "type": row[1]} for row in cursor.fetchall()]
            finally:
                cursor.close()

        try:
            columns = await loop.run_in_executor(None, _describe_table)
            return aiohttp.web.json_response(columns)
        except LookupError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            _logger.warning("Schema query failed: %s", exc)
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

    app = aiohttp.web.Application()
    app.add_routes(routes)
    return app


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def start_query_server(
    store: Any,
    host: str = "0.0.0.0",
    port: int = 8080,
    max_rows: int = 1000,
    query_timeout: float = 10.0,
) -> Any:  # aiohttp.web.AppRunner
    """Start the HTTP query API server.

    Parameters
    ----------
    store:
        An initialized ``DuckDbStore`` instance.
    host:
        Bind address (default ``0.0.0.0`` for K8s readiness probes).
    port:
        TCP port.
    max_rows:
        Default row cap for queries.
    query_timeout:
        Per-query timeout in seconds.

    Returns
    -------
        An ``aiohttp.web.AppRunner`` that the caller must clean up
        with ``await runner.cleanup()`` before closing the store.
    """
    if not _AIOHTTP_AVAILABLE:
        raise RuntimeError(_AIOHTTP_IMPORT_ERROR)

    import aiohttp.web

    app = create_app(store, max_rows=max_rows, query_timeout=query_timeout)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    _logger.info("Query API listening on %s:%d", host, port)
    return runner
