"""Pipeline Silence Monitor — fails when the LMAO ingest pipeline goes quiet.

Runs as a K8s CronJob. It asks the in-cluster ingest query API for the newest
``ingested_at`` row and compares it with the wall clock. If nothing has been
ingested within ``SILENCE_THRESHOLD_HOURS``, it logs ``PIPELINE SILENCE`` and
exits 1 (failing the CronJob), which is what surfaces the outage.

Why ``ingested_at`` and not the device timestamp: ``ingested_at`` is written by
the consumer with the *server's* ``CURRENT_TIMESTAMP``, so it is authoritative.
The device-side ``timestamp_ms`` is whatever the client's clock says, and those
clocks are routinely hours off — using them made a healthy pipeline look silent
(and a silent one look fresh). The JetStream stream state is not usable either:
the pinned ``nats-py`` 0.2.0 ``StreamState`` carries sequence numbers only, no
timestamps.

This catches the "Cardputer wedged → RNode TX wedged" failure mode that caused
a 4.5-day unnoticed outage (issue #96).

Configuration (environment variables):
    QUERY_URL                Ingest query API base URL
                             (default http://iot-query.default.svc.cluster.local:8080)
    SILENCE_THRESHOLD_HOURS  Max allowed gap since the last ingested row, in hours
                             (default 3)

Usage::

    python k8s-app/pipeline_monitor.py

    QUERY_URL=http://localhost:8080 SILENCE_THRESHOLD_HOURS=2 \
        python k8s-app/pipeline_monitor.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import urllib.error
import urllib.request

_logger = logging.getLogger(__name__)

_DEFAULT_QUERY_URL = "http://iot-query.default.svc.cluster.local:8080"
_DEFAULT_SILENCE_THRESHOLD_HOURS = 3.0
_TABLES = ("sensor_readings", "text_messages")


def _query(url: str, sql: str, timeout: float = 15.0) -> list[list[object]]:
    """POST *sql* to the ingest query API and return its rows."""
    body = json.dumps({"sql": sql}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/query", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["rows"]


def _parse_ts(value: object) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("+00", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        ts = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def newest_ingest(rows_by_table: dict[str, list[list[object]]]) -> dt.datetime | None:
    """Newest ``ingested_at`` across every table (pure; unit-tested)."""
    newest: dt.datetime | None = None
    for rows in rows_by_table.values():
        for row in rows:
            if not row:
                continue
            ts = _parse_ts(row[0])
            if ts is None:
                continue
            if newest is None or ts > newest:
                newest = ts
    return newest


def main() -> None:
    """Entry point: query the ingest clock and fail the job on silence.

    Exits 0 when a row was ingested within the threshold, 1 when the pipeline
    is silent, has never run, or the query API cannot be reached.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    query_url = os.environ.get("QUERY_URL", _DEFAULT_QUERY_URL)
    raw_threshold = os.environ.get(
        "SILENCE_THRESHOLD_HOURS", str(_DEFAULT_SILENCE_THRESHOLD_HOURS)
    )
    try:
        threshold = float(raw_threshold)
    except ValueError:
        _logger.critical("Invalid SILENCE_THRESHOLD_HOURS value: %s", raw_threshold)
        sys.exit(1)
    if threshold <= 0:
        _logger.critical("SILENCE_THRESHOLD_HOURS must be positive, got %s", threshold)
        sys.exit(1)

    _logger.info("Pipeline monitor starting: query=%s, threshold=%.1fh", query_url, threshold)

    try:
        rows_by_table = {
            table: _query(query_url, f"SELECT max(ingested_at) FROM {table}")
            for table in _TABLES
        }
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        _logger.critical("Could not query %s — is iot-ingest running? %s", query_url, exc)
        sys.exit(1)

    newest = newest_ingest(rows_by_table)
    if newest is None:
        _logger.error(
            "PIPELINE SILENCE: no rows have ever been ingested — the pipeline never ran."
        )
        sys.exit(1)

    now = dt.datetime.now(dt.timezone.utc)
    gap = now - newest
    if gap.total_seconds() < 0:
        _logger.warning(
            "Monitor clock appears %s seconds behind the ingest clock — gap may be unreliable",
            abs(gap.total_seconds()),
        )
    gap_hours = gap.total_seconds() / 3600.0

    if gap_hours > threshold:
        _logger.error(
            "PIPELINE SILENCE: last row ingested %.1f hours ago (threshold %.1f h). "
            "Newest ingested_at=%s, now=%s.",
            gap_hours,
            threshold,
            newest.isoformat(),
            now.isoformat(),
        )
        sys.exit(1)

    _logger.info("Pipeline OK: last row ingested %.1f hours ago (threshold %.1fh)", gap_hours, threshold)
    sys.exit(0)


if __name__ == "__main__":
    main()
