"""E2E (hardware): Cardputer → LMAO Rust receiver over real LoRa, 500-B resource.

Mirrors the live proof captured on 2026-10-01: the Cardputer firmware
(`rust-client/firmware/`) pushes the payload ``(0..500u16).map(|i| i % 251)``
as an RNS Resource over the held link and the k8s Rust receiver
(`lmao-server-rust-recv`, Heltec RNode on tp4) reassembled it:

    RESOURCE received link=LinkId(…) bytes=500
              sha256=f6b8396506ad2ac31bfe6d73fa0155e090b62b4321043dafe308090296b28d84

This test drives that real RF path and asserts the exact hardware-proven
sha256, cross-checking the deterministic host e2e
(`//rust-client:resource_500b_e2e`, which asserts the same digest in-process).

Hardware requirements:
  * Cardputer on ``/dev/ttyACM0`` running a firmware built from
    ``rust-client/firmware`` with the 500-B payload (the flashed image is left
    untouched — only a USB reset is issued to re-dial `lmao.data`).
  * ``kubectl`` configured for the cluster running ``lmao-server-rust-recv``
    (image ``lmao-server-rust-recv:test`` on node tp4, RNode on /dev/ttyUSB0).

Skips (never fails) when any of those are missing.

Run with::

    bazel test //tests:test_cardputer_500b_resource --test_output=all
"""

import re
import shutil
import subprocess
import time

import pytest

# The payload sha256 the receiver must log (LoRa-proven, 2026-10-01).
EXPECTED_SHA256 = "f6b8396506ad2ac31bfe6d73fa0155e090b62b4321043dafe308090296b28d84"
RECEIVER_LABEL = "app=lmao-server-rust-recv"
WAIT_SECONDS = 120
POLL_INTERVAL = 2.0


def _kubectl(args, timeout=30):
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _hardware_present():
    if not shutil.which("kubectl"):
        return False, "kubectl not found"
    import os

    if not os.path.exists("/dev/ttyACM0"):
        return False, "/dev/ttyACM0 (Cardputer) not present"
    if not os.path.exists("/dev/ttyUSB0"):
        return False, "/dev/ttyUSB0 (RNode) not present"
    r = _kubectl(["get", "deploy", "lmao-server-rust-recv", "-n", "default"])
    if r.returncode != 0:
        return False, "receiver deployment lmao-server-rust-recv not reachable"
    return True, ""


HARDWARE_OK, HARDWARE_REASON = _hardware_present()


def _reset_cardputer():
    """Pulse DTR/RTS to reset the ESP32-S3 Cardputer; it boots to Idle and
    re-dials lmao.data (the firmware auto-starts the resource push on link up)."""
    import serial

    with serial.Serial("/dev/ttyACM0", 115200, timeout=0.2) as s:
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.25)
        s.setDTR(True)
        s.setRTS(False)
        time.sleep(0.1)
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.25)


RECEIVED_RE = re.compile(r"RESOURCE received .* bytes=(\d+) sha256=([0-9a-f]{64})")
LINK_RE = re.compile(r"LINK established")


def _recent_logs():
    r = _kubectl(["logs", "-l", RECEIVER_LABEL, "--tail=2000"])
    return r.stdout if r.returncode == 0 else ""


@pytest.mark.skipif(not HARDWARE_OK, reason=HARDWARE_REASON)
def test_cardputer_500b_resource_reaches_receiver():
    _reset_cardputer()

    deadline = time.time() + WAIT_SECONDS
    saw_link = False
    while time.time() < deadline:
        logs = _recent_logs()
        if not saw_link and LINK_RE.search(logs):
            saw_link = True
        for m in RECEIVED_RE.finditer(logs):
            if m.group(1) == "500":
                assert m.group(2) == EXPECTED_SHA256, (
                    f"receiver got bytes=500 but sha256 {m.group(2)} != "
                    f"LoRa-proven {EXPECTED_SHA256} (firmware payload changed?)"
                )
                return
        time.sleep(POLL_INTERVAL)

    detail = "link established" if saw_link else "no LINK established in logs"
    raise AssertionError(
        f"timed out after {WAIT_SECONDS}s waiting for RESOURCE received bytes=500 "
        f"sha256={EXPECTED_SHA256}; {detail}"
    )
