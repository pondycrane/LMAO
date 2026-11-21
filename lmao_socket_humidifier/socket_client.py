"""HTTP client for the humidifier Atom Socket firmware.

The socket exposes a leased control API on the LAN:

    GET  /status          JSON snapshot
    POST /on?lease=SECS   close the relay, valid for SECS
    POST /off             open the relay now

Commands carry the NVS-provisioned token in ``X-Auth-Token``.  A socket with
no token refuses commands (fails closed), which surfaces here as HTTP 403.

A host may be given as a comma-separated fallback list (``humidifier.local,
192.168.50.22``): mDNS resolution fails transiently, and a transport failure
falls through to the next host.  An HTTP answer is authoritative and never
falls through.  The host that answers becomes the sticky first choice.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class SocketError(RuntimeError):
    """The socket refused or did not answer."""

    def __init__(self, msg: str, *, transport: bool = False):
        super().__init__(msg)
        self.transport = transport


class SocketClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 5.0):
        # ``base_url`` may be a comma-separated fallback list.  mDNS
        # (``humidifier.local``) resolves through nss-mdns and does fail
        # transiently; a pinned address keeps the relay controllable then.
        urls = []
        for part in base_url.split(","):
            part = part.strip()
            if not part:
                continue
            if "://" not in part:
                part = "http://" + part
            urls.append(part.rstrip("/"))
        if not urls:
            raise ValueError("no socket host given")
        self.base_urls = urls
        self.token = token
        self.timeout = float(timeout)

    # ---- plumbing -------------------------------------------------------

    def _request(self, path: str, method: str = "GET") -> dict:
        # Every failed host is reported with the host that failed it.  The
        # bare ``<urlopen error … Name or service not known>`` was misleading:
        # the pinned IP times out while the mDNS name raises the name error,
        # so a single "last error" blamed DNS for an IP timeout.
        failures: list[str] = []
        for idx, base in enumerate(self.base_urls):
            try:
                body = self._send(base, path, method)
            except SocketError as exc:
                if not exc.transport:
                    raise
                failures.append(f"{base} -> {exc}")
                continue
            if idx:
                # Sticky failover: the working host moves to the front so a
                # dead mDNS name is not re-tried on every command.
                self.base_urls.insert(0, self.base_urls.pop(idx))
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise SocketError(f"{method} {path} returned non-JSON: {body!r}") from exc
        # The loop returns on the first success and re-raises a non-transport
        # error, so reaching here means every host failed.
        raise SocketError(
            f"{method} {path} failed on all {len(failures)} host(s): "
            + "; ".join(failures),
            transport=True,
        )

    def _send(self, base: str, path: str, method: str) -> bytes:
        req = urllib.request.Request(base + path, method=method)
        if self.token:
            req.add_header("X-Auth-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise SocketError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SocketError(f"{method} {path} failed: {exc}", transport=True) from exc

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
