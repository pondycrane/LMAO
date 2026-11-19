"""Socket HTTP client tests against a real local HTTP server.

The firmware's API contract is exercised end to end (URL, method, auth
header, query parameter, JSON body) without mocking urllib.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lmao_socket_humidifier.socket_client import SocketClient, SocketError

TOKEN = "deadbeef"
STATE = {"relay": False, "lease_s": 0, "commands": []}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("X-Auth-Token") == TOKEN

    def do_GET(self):
        if self.path == "/status":
            self._reply(200, {"relay": STATE["relay"], "lease_s": STATE["lease_s"]})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._reply(403, {"error": "bad or missing token"})
            return
        if self.path.startswith("/on"):
            lease = int(self.path.split("lease=")[1])
            STATE.update(relay=True, lease_s=lease)
            STATE["commands"].append(("on", lease))
            self._reply(200, {"relay": True, "lease_s": lease})
        elif self.path == "/off":
            STATE.update(relay=False, lease_s=0)
            STATE["commands"].append(("off", 0))
            self._reply(200, {"relay": False, "lease_s": 0})
        else:
            self._reply(404, {"error": "not found"})


@pytest.fixture
def server():
    STATE.update(relay=False, lease_s=0, commands=[])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_status_requires_no_token(server):
    assert SocketClient(server).status() == {"relay": False, "lease_s": 0}


def test_on_sends_the_lease_and_the_token(server):
    client = SocketClient(server, TOKEN)
    assert client.set_on(300) == {"relay": True, "lease_s": 300}
    assert STATE["commands"] == [("on", 300)]
    assert client.status()["relay"] is True


def test_off_opens_the_relay(server):
    client = SocketClient(server, TOKEN)
    client.set_on(120)
    assert client.set_off() == {"relay": False, "lease_s": 0}
    assert STATE["commands"] == [("on", 120), ("off", 0)]


def test_missing_token_is_refused(server):
    with pytest.raises(SocketError, match="403"):
        SocketClient(server).set_on(60)
    assert STATE["commands"] == []


def test_scheme_is_optional(server):
    assert SocketClient(f"http://{server}", TOKEN).status()["relay"] is False


def test_unreachable_socket_raises_socket_error():
    client = SocketClient("127.0.0.1:1", TOKEN, timeout=1.0)
    with pytest.raises(SocketError):
        client.status()
