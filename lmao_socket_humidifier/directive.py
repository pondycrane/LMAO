"""Publish the controller's decision for the socket to pull.

The controller keeps the law and its persisted state; this module is the one
outbound call that hands the decision to the server's directive relay
(``lma_core.humidifier_api``).  The socket polls that relay on its own
outbound path, so nothing here needs to know the socket's address — the
coupling that broke on 2026-11-21 when a DHCP lease moved.

Publishing is best-effort and never fatal: if the relay is unreachable the
controller keeps running (its push path and state file are unaffected), the
relay stops serving the directive at its ``max_age_s``, and the socket opens
the relay on ``503``.  The error is logged once per distinct failure, not
once per cycle.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("lmao_socket_humidifier")

DEFAULT_URL = "http://localhost:8082/humidifier"


@dataclass(frozen=True)
class Directive:
    """One decision, in the shape the relay validates and the socket parses."""

    key: str
    relay_on: bool
    reason: str
    lease_s: int
    issued_s: float
    fail_safe: bool = False

    def payload(self) -> dict:
        return {
            "on": self.relay_on,
            # An OFF directive carries no lease: the socket must not be able
            # to arm a deadman for a relay it is opening.
            "lease_s": self.lease_s if self.relay_on else 0,
            "reason": self.reason,
            "issued_s": round(self.issued_s, 3),
            "fail_safe": self.fail_safe,
        }


class Publisher:
    """POST directives to the relay; remembers the last error to avoid spam."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 5.0):
        self._url = url.rstrip("/")
        self._timeout = float(timeout)
        self._last_error: str | None = None
        self.last_ok_s: float = 0.0

    def publish(self, directive: Directive) -> bool:
        url = f"{self._url}/{urllib.parse.quote(directive.key, safe='')}"
        body = json.dumps(directive.payload()).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if msg != self._last_error:
                logger.warning("directive publish failed: %s", msg)
                self._last_error = msg
            return False
        if self._last_error is not None:
            logger.info("directive publish recovered")
            self._last_error = None
        self.last_ok_s = time.time()
        return True
