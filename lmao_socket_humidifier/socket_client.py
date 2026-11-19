"""HTTP client for the humidifier Atom Socket firmware.

The socket exposes a leased control API on the LAN:

    GET  /status          JSON snapshot
    POST /on?lease=SECS   close the relay, valid for SECS
    POST /off             open the relay now

Commands carry the NVS-provisioned token in ``X-Auth-Token``.  A socket with
no token refuses commands (fails closed), which surfaces here as HTTP 403.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class SocketError(RuntimeError):
    """The socket refused or did not answer."""


class SocketClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 5.0):
        url = base_url.strip()
        if "://" not in url:
            url = "http://" + url
        self.base_url = url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)

    # ---- plumbing -------------------------------------------------------

    def _request(self, path: str, method: str = "GET") -> dict:
        req = urllib.request.Request(self.base_url + path, method=method)
        if self.token:
            req.add_header("X-Auth-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise SocketError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SocketError(f"{method} {path} failed: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SocketError(f"{method} {path} returned non-JSON: {body!r}") from exc

    # ---- API ------------------------------------------------------------

    def status(self) -> dict:
        """Relay/lease/metering snapshot."""
        return self._request("/status")

    def set_on(self, lease_s: int) -> dict:
        """Close the relay for ``lease_s`` seconds (renews an active lease)."""
        query = urllib.parse.urlencode({"lease": int(lease_s)})
        return self._request(f"/on?{query}", method="POST")

    def set_off(self) -> dict:
        """Open the relay now."""
        return self._request("/off", method="POST")
