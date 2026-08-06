"""Pipeline Silence Monitor — periodically checks whether the
``LMAO_MESSAGES`` JetStream stream has received new messages within
the last N hours.

Designed to run as a K8s CronJob.  If the pipeline has been silent
longer than the configured threshold, the script logs an ERROR and
exits with code 1 (failing the CronJob).  It also publishes an alert
to a dedicated NATS subject (best-effort — delivery failures are
logged and do not affect the exit code).

This catches the "Cardputer wedged → RNode TX wedged" failure mode
that caused a 4.5-day unnoticed outage (issue #96).

Configuration (environment variables):
    NATS_SERVER              NATS server URL
                             (default: nats://nats-server.default.svc.cluster.local:4222)
    SILENCE_THRESHOLD_HOURS  Max allowed gap since last message, in hours
                             (default: 3)
    ALERT_SUBJECT            Optional NATS subject to publish alert to on silence
                             (default: lmao.alerts.silence)

Usage::

    python k8s-app/pipeline_monitor.py

    NATS_SERVER=nats://localhost:4222 SILENCE_THRESHOLD_HOURS=2 \
        python k8s-app/pipeline_monitor.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_NATS_SERVER = "nats://nats-server.default.svc.cluster.local:4222"
_DEFAULT_SILENCE_THRESHOLD_HOURS = 3
_DEFAULT_ALERT_SUBJECT = "lmao.alerts.silence"
_STREAM_NAME = "LMAO_MESSAGES"


async def main() -> None:
    """Entry point for the pipeline silence monitor.

    Reads configuration from environment variables, connects to NATS,
    queries the ``LMAO_MESSAGES`` stream state, and compares the
    timestamp of the last published message with the current time.

    Exits 0 if the pipeline is healthy (recent messages received).
    Exits 1 if the pipeline is silent (no recent messages or stream
    never received a message).
    """
    nats_server = os.environ.get("NATS_SERVER", _DEFAULT_NATS_SERVER)
    silence_hours_str = os.environ.get(
        "SILENCE_THRESHOLD_HOURS", str(_DEFAULT_SILENCE_THRESHOLD_HOURS)
    )
    alert_subject = os.environ.get("ALERT_SUBJECT", _DEFAULT_ALERT_SUBJECT)

    # Validate threshold
    try:
        silence_hours = float(silence_hours_str)
    except ValueError:
        _logger.critical(
            "Invalid SILENCE_THRESHOLD_HOURS value: %s (must be numeric)",
            silence_hours_str,
        )
        sys.exit(1)

    if silence_hours <= 0:
        _logger.critical(
            "SILENCE_THRESHOLD_HOURS must be positive, got %s",
            silence_hours,
        )
        sys.exit(1)

    nq = None

    try:
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="pipeline-monitor")

        _logger.info(
            "Pipeline monitor starting: NATS=%s, threshold=%.1fh, stream=%s",
            nats_server,
            silence_hours,
            _STREAM_NAME,
        )

        await nq.connect(servers=nats_server)

        # Query stream state — stream_info() returns a StreamInfo whose
        # .state dataclass has last_ts (datetime | None, UTC).
        try:
            stream_info = await nq.stream_info(_STREAM_NAME)
        except Exception as exc:
            _logger.critical(
                "Could not query stream '%s' on '%s' — "
                "verify it exists and that iot-ingest has run once: %s",
                _STREAM_NAME,
                nats_server,
                exc,
            )
            sys.exit(1)
        last_ts = stream_info.state.last_ts

        if last_ts is None:
            _logger.error(
                "PIPELINE SILENCE: Stream '%s' has never received a message — "
                "pipeline may have never started. Alert.",
                _STREAM_NAME,
            )
            await _maybe_alert(nq, alert_subject, silence_hours, never_started=True)
            sys.exit(1)

        now = datetime.now(timezone.utc)
        # NATS timestamps are server-side UTC — ensure last_ts is offset-aware
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        gap = now - last_ts
        if gap.total_seconds() < 0:
            _logger.warning(
                "Monitor clock appears %s seconds behind NATS server time "
                "(last_ts=%s, now=%s) — gap may be unreliable; check clock sync",
                abs(gap.total_seconds()),
                last_ts.isoformat(),
                now.isoformat(),
            )
        gap_hours = gap.total_seconds() / 3600.0

        if gap_hours > silence_hours:
            _logger.error(
                "PIPELINE SILENCE: Last message was %.1f hours ago "
                "(threshold: %.1f hours). Stream '%s' appears silent. "
                "Last message at %s, now is %s.",
                gap_hours,
                silence_hours,
                _STREAM_NAME,
                last_ts.isoformat(),
                now.isoformat(),
            )
            await _maybe_alert(nq, alert_subject, silence_hours, gap_hours=gap_hours)
            sys.exit(1)

        _logger.info(
            "Pipeline OK: last message %.1f hours ago (threshold: %.1fh)",
            gap_hours,
            silence_hours,
        )
        sys.exit(0)

    except ImportError as exc:
        _logger.critical("Missing dependency: %s", exc)
        sys.exit(1)
    except Exception:
        _logger.critical("Fatal error in monitor", exc_info=True)
        sys.exit(1)
    finally:
        if nq is not None:
            await nq.close()


async def _maybe_alert(
    nq,
    alert_subject: str,
    silence_hours: float,
    *,
    never_started: bool = False,
    gap_hours: float | None = None,
) -> None:
    """Optionally publish an alert to the configured NATS subject.

    This is a best-effort operation — failures are logged but do not
    affect the exit code.
    """
    if never_started:
        payload = (
            f"PIPELINE SILENCE: Stream has never received a message. "
            f"Threshold: {silence_hours:.1f}h."
        ).encode()
    else:
        payload = (
            f"PIPELINE SILENCE: Last message was {gap_hours:.1f} hours ago. "
            f"Threshold: {silence_hours:.1f}h."
        ).encode()

    try:
        await nq.publish(alert_subject, payload)
        _logger.info("Alert published to '%s'", alert_subject)
    except Exception:
        _logger.error(
            "Alert delivery FAILED for subject '%s' — "
            "silence reported but not published. "
            "Relying on CronJob exit status for visibility.",
            alert_subject,
            exc_info=True,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        asyncio.run(main())
    except ImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        _logger.exception("Monitor failed")
        sys.exit(1)
