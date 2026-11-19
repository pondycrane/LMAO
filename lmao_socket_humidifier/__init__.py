"""LMAO socket humidifier — room humidity from the mesh, relay on the socket.

Read side: the RoboDog home node's air humidity, via the LMAO server's
``HistoryRequest`` (Reticulum TCP) — the same contract ``robodog_home_client``
speaks.  Write side: the M5Stack Atom Socket's leased HTTP API
(``lmao_socket_humidifier/firmware``).

Split so the decision law is pure and host-testable:

* ``control.py``       hysteresis + safety limits (no I/O)
* ``socket_client.py`` Atom Socket HTTP client
* ``controller.py``    the poll/decide/act loop + CLI

    bazel run //lmao_socket_humidifier:controller -- --env-node "main bedroom" \
        --socket humidifier.local --token "$HF_API_TOKEN"
"""

__all__ = ["control", "socket_client", "controller"]
