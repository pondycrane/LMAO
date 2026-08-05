"""Unit tests for lma_core.query_api (SQL guard + HTTP endpoints).

Tests the ``_validate_readonly_sql`` function directly (no DuckDB
needed) and the aiohttp query endpoints with a mocked DuckDbStore.
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# SQL guard tests — pure function, no DuckDB or aiohttp needed
# ---------------------------------------------------------------------------


class TestSqlGuard:
    """Tests for ``_validate_readonly_sql`` — read-only SQL enforcement."""

    @pytest.fixture(autouse=True)
    def _ensure_sqlparse(self):
        """Ensure sqlparse is importable (real module, not mocked)."""
        # sqlparse must be a real import for the guard to work.
        # If it's not installed, skip these tests.
        try:
            import sqlparse  # noqa: F401
        except ImportError:
            pytest.skip("sqlparse not installed")

    @staticmethod
    def _validate(sql: str) -> None:
        from lma_core.query_api import _validate_readonly_sql

        _validate_readonly_sql(sql)

    # ── Accepted statements ─────────────────────────────────────

    def test_accepts_select(self):
        self._validate("SELECT * FROM t")

    def test_accepts_select_lowercase(self):
        self._validate("select node_id from sensor_readings")

    def test_accepts_with_cte(self):
        self._validate("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_accepts_explain(self):
        self._validate("EXPLAIN SELECT 1")

    def test_accepts_show(self):
        self._validate("SHOW TABLES")

    def test_accepts_describe(self):
        self._validate("DESCRIBE sensor_readings")

    def test_accepts_desc(self):
        self._validate("DESC sensor_readings")

    # ── Rejected — DML / DDL ────────────────────────────────────

    def test_rejects_insert(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("INSERT INTO t VALUES (1)")

    def test_rejects_drop(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("DROP TABLE t")

    def test_rejects_delete(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("DELETE FROM t")

    def test_rejects_update(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("UPDATE t SET x=1")

    def test_rejects_truncate(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("TRUNCATE t")

    def test_rejects_grant(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("GRANT SELECT ON t TO u")

    def test_rejects_alter(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("ALTER TABLE t ADD COLUMN x INT")

    def test_rejects_create(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("CREATE TABLE t (x INT)")

    def test_rejects_pragma(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("PRAGMA database_list")

    # ── Rejected — DuckDB-specific ──────────────────────────────

    def test_rejects_attach(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("ATTACH 'file.db'")

    def test_rejects_copy(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("COPY t TO 'file.csv'")

    def test_rejects_export(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("EXPORT DATABASE 'dir'")

    def test_rejects_install(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("INSTALL httpfs")

    def test_rejects_load(self):
        with pytest.raises(ValueError, match="not allowed"):
            self._validate("LOAD httpfs")

    # ── Multi-statement / empty ─────────────────────────────────

    def test_rejects_multi_statement(self):
        with pytest.raises(ValueError, match="Multiple"):
            self._validate("SELECT 1; DROP TABLE t")

    def test_rejects_multi_statement_newline(self):
        with pytest.raises(ValueError, match="Multiple"):
            self._validate("SELECT 1;\nSELECT 2")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="No SQL"):
            self._validate("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="No SQL"):
            self._validate("   ")

    # ── Edge cases ──────────────────────────────────────────────

    def test_sql_comments_are_handled(self):
        """SQL with a comment and SELECT should be accepted."""
        self._validate("-- INSERT into t\nSELECT 1")

    def test_block_comment_with_select(self):
        """Block comment with SELECT should be accepted."""
        self._validate("/* DROP TABLE t */ SELECT 1")

    def test_select_with_string_literal_containing_keyword(self):
        """SELECT with a string literal containing 'DROP' should be accepted
        (sqlparse handles quoted strings)."""
        self._validate("SELECT 'DROP TABLE t' AS msg FROM sensor_readings")

    def test_rejects_cte_with_dml(self):
        """CTE that contains an UPDATE in the sub-statement should be
        rejected by the keyword blacklist defense-in-depth."""
        with pytest.raises(ValueError, match="disallowed keywords"):
            self._validate("WITH x AS (UPDATE t SET c=1) SELECT * FROM x")


# ---------------------------------------------------------------------------
# Fixture — mock DuckDB store for endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_store():
    """Return a MagicMock DuckDbStore with get_connection and cursor support."""
    store = MagicMock()
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    store.get_connection.return_value = mock_conn
    return store, mock_cursor


# ---------------------------------------------------------------------------
# Fixture — aiohttp test client
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(mock_store):
    """Return an aiohttp test client for the query API app."""
    try:
        from aiohttp.test_utils import TestClient, TestServer  # noqa: F401
    except ImportError:
        pytest.skip("aiohttp test utilities unavailable")

    from lma_core.query_api import create_app

    store, _ = mock_store
    app = create_app(store, max_rows=10, query_timeout=30.0)

    async with TestClient(TestServer(app)) as cli:
        yield cli


# ---------------------------------------------------------------------------
# Fixture — ensure aiohttp and sqlparse are importable
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_deps():
    """Skip all endpoint tests if aiohttp or sqlparse are not installed."""
    try:
        import aiohttp  # noqa: F401
        import sqlparse  # noqa: F401
    except ImportError:
        pytest.skip("aiohttp or sqlparse not installed")


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestQueryEndpoints:
    """HTTP endpoint tests with mocked DuckDB store."""

    async def test_healthz_returns_200(self, mock_store):
        """GET /healthz should return 200 with status ok."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, _ = mock_store
        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"status": "ok"}

    async def test_query_valid_select(self, mock_store):
        """POST /query with valid SELECT should return JSON results."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        # Configure mock cursor response
        mock_cursor.description = [
            ("node_id",),
            ("value",),
        ]
        mock_cursor.fetchall.return_value = [
            ("node-1", 22.5),
            ("node-2", 23.1),
        ]

        app = create_app(store, max_rows=10)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/query",
                json={"sql": "SELECT node_id, value FROM sensor_readings"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["columns"] == ["node_id", "value"]
            assert data["rows"] == [["node-1", 22.5], ["node-2", 23.1]]
            assert data["row_count"] == 2

    async def test_query_rejects_insert(self, mock_store):
        """POST /query with INSERT should return 400."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, _ = mock_store
        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/query",
                json={"sql": "INSERT INTO t VALUES(1)"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data

    async def test_query_row_cap(self, mock_store):
        """POST /query should enforce row cap via appended LIMIT."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        mock_cursor.description = [("x",)]
        # Simulate more rows than max_rows — the cap is applied via SQL,
        # not post-filtering.  We just verify the LIMIT was appended.
        mock_cursor.fetchall.return_value = [(i,) for i in range(10)]

        # Use a small max_rows
        app = create_app(store, max_rows=5)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/query",
                json={"sql": "SELECT x FROM t"},
            )
            assert resp.status == 200
            # Verify that the cursor was called with LIMIT 5 appended
            call_sql = mock_cursor.execute.call_args[0][0]
            assert "LIMIT 5" in call_sql

    async def test_query_preserves_user_limit(self, mock_store):
        """POST /query should NOT append LIMIT if user already supplied one."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        mock_cursor.description = [("x",)]
        mock_cursor.fetchall.return_value = [(1,), (2,)]

        app = create_app(store, max_rows=5)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/query",
                json={"sql": "SELECT x FROM t LIMIT 3"},
            )
            assert resp.status == 200
            call_sql = mock_cursor.execute.call_args[0][0]
            # Should have exactly one LIMIT (user's), not two
            assert call_sql.count("LIMIT") == 1

    async def test_tables_endpoint(self, mock_store):
        """GET /tables should return list of tables with row counts."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        # First query: list tables
        # Second/third queries: count rows in each table
        mock_cursor.fetchall.side_effect = [
            [("sensor_readings",), ("text_messages",)],  # table list
        ]
        # fetchone is used for row counts
        mock_cursor.fetchone.side_effect = [(42,), (7,)]

        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/tables")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, list)

    async def test_schema_endpoint(self, mock_store):
        """GET /schema/<table> should return column metadata."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        # First query: table existence check
        # Second query: column list
        mock_cursor.fetchone.side_effect = [
            ("sensor_readings",),  # table exists
            None,  # columns query uses fetchall
        ]
        mock_cursor.fetchall.side_effect = [
            [],  # table list (not used in this test)
            [("node_id", "VARCHAR"), ("value", "FLOAT")],
            [],
        ]

        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/schema/sensor_readings")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 2
            assert data[0]["name"] == "node_id"
            assert data[0]["type"] == "VARCHAR"
            assert data[1]["name"] == "value"
            assert data[1]["type"] == "FLOAT"

    async def test_schema_404(self, mock_store):
        """GET /schema/<nonexistent> should return 404."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, mock_cursor = mock_store
        mock_cursor.fetchone.return_value = None  # table not found

        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/schema/nonexistent")
            assert resp.status == 404

    async def test_query_missing_body(self, mock_store):
        """POST /query with no body should return 400."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, _ = mock_store
        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/query", data="not json")
            assert resp.status == 400

    async def test_query_invalid_json(self, mock_store):
        """POST /query with malformed JSON should return 400."""
        from lma_core.query_api import create_app
        from aiohttp.test_utils import TestClient, TestServer

        store, _ = mock_store
        app = create_app(store)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/query",
                data="{not valid json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


# ---------------------------------------------------------------------------
# Server lifecycle test
# ---------------------------------------------------------------------------


class TestStartQueryServer:
    """Tests for start_query_server lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_cleanup(self, mock_store):
        """start_query_server() should return an AppRunner that can be cleaned up."""
        from lma_core.query_api import start_query_server

        store, _ = mock_store
        runner = await start_query_server(store, host="127.0.0.1", port=0)
        assert runner is not None
        await runner.cleanup()
