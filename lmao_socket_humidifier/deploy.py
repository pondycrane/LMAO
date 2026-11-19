"""Deploy the humidifier controller into the cluster (one `bazel run`).

    bazel run //lmao_socket_humidifier:deploy

Builds ``Dockerfile.humidifier``, pushes ``lmao-humidifier:latest`` to the
local registry, provisions the socket-token + LXMF-identity Secrets, applies
``k8s/humidifier.yaml``, waits for a rollout and the first control decision,
then stops the host systemd unit so exactly one process drives the relay.

The deploy logic lives in ``tools/install_services.deploy_humidifier`` — this
is the standalone entry point for it (``install_all --include-services`` runs
the same function).
"""

from __future__ import annotations

import sys

from tools.install_services import deploy_humidifier


class _Result:
    """Minimal DeviceResult stand-in: status + the messages it accumulates."""

    def __init__(self, name: str):
        self.name = name
        self.status = "PENDING"
        self.message = ""

    def ok(self, message: str) -> None:
        self.status, self.message = "OK", message

    def fail(self, message: str) -> None:
        self.status, self.message = "FAIL", message

    def skip(self, message: str) -> None:
        self.status, self.message = "SKIP", message


def main() -> int:
    result = _Result("Humidifier Controller")
    deploy_humidifier(result)
    print(f"\n{result.name}: [{result.status}] {result.message}")
    return 0 if result.status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
