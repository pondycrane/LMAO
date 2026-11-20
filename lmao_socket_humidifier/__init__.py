"""LMAO socket humidifier — room humidity from the Atom Socket's ENV III.

One device per room: the M5Stack Atom Socket (Atom Lite inside) carries an
ENV III (SHT30) and the humidifier's mains relay, so it is both the sensor and
the actuator.  Its firmware exposes the sample on ``GET /status`` and reports
it to the LMAO server as a ``SensorReport`` (Reticulum TCP), keeping the
room's air history.

Split so the decision law is pure and host-testable:

* ``control.py``       dew-point hysteresis + safety limits (no I/O)
* ``socket_client.py`` Atom Socket HTTP client
* ``controller.py``    the poll/decide/act loop + CLI
* ``deploy.py``        build the image, push it, deploy the K8s controllers

    bazel run //lmao_socket_humidifier:controller -- --env-node "baby room" \\
        --socket humidifier-baby.local --sensor-source socket \\
        --token "$HF_BABY_API_TOKEN"
    bazel run //lmao_socket_humidifier:deploy
"""

__all__ = ["control", "socket_client", "controller"]
