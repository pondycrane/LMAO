"""One schedule, three implementations — pinned in a single place.

The LMAF dead-letter schedule lives in the shared C++ core (the canonical
table), in the server's Python codec, and in the MicroPython client, which
cannot import either.  Comments asking people to keep three copies equal are
not a mechanism, so this test reads all three and fails on drift — the same
role the golden wire vectors play for the framing bytes.

Each side also asserts the literal sequence in its own suite (the C++ host test
and the server test); this one is what makes a mismatch visible in CI.
"""

import re
from pathlib import Path

from cardputer_client import main as lmao_client
from lma_core import attachment as lmaf

ROOT = Path(__file__).resolve().parents[1]

# The C++ table is the source of truth and cannot be imported from Python.
CPP_HEADER = ROOT / "firmware_common/lma_common/lma_attachment.h"
SERVER_SOURCE = ROOT / "lmao_server/server.py"


def _number(text, pattern, cast=float):
    match = re.search(pattern, text, re.M)
    assert match, f"pattern not found in source: {pattern}"
    return cast(match.group(1))


def _cpp_policy():
    src = CPP_HEADER.read_text()
    return {
        "max_attempts": _number(src, r"max_attempts\s*=\s*(\d+)", int),
        "first_delay_seconds": _number(src, r"first_delay_seconds\s*=\s*([\d.]+)"),
        "backoff_factor": _number(src, r"backoff_factor\s*=\s*([\d.]+)"),
    }


def _python_policy():
    return {
        "max_attempts": lmaf.RETRY_MAX_ATTEMPTS,
        "first_delay_seconds": lmaf.RETRY_FIRST_DELAY_SECONDS,
        "backoff_factor": lmaf.RETRY_BACKOFF_FACTOR,
    }


def _micropython_policy():
    return {
        "max_attempts": lmao_client.RETRY_MAX_ATTEMPTS,
        "first_delay_seconds": lmao_client.RETRY_FIRST_DELAY_SECONDS,
        "backoff_factor": lmao_client.RETRY_BACKOFF_FACTOR,
    }


def test_the_three_implementations_agree():
    cpp = _cpp_policy()
    server = _python_policy()
    device = _micropython_policy()
    assert cpp == server, f"C++ core and Python codec disagree: {cpp} vs {server}"
    assert cpp == device, f"C++ core and MicroPython client disagree: {cpp} vs {device}"


def test_the_three_implementations_compute_the_same_schedule():
    expected = [30.0, 90.0, 270.0]
    assert [lmaf.retry_delay_seconds(i) for i in range(3)] == expected
    assert [lmao_client._retry_delay_seconds(i) for i in range(3)] == expected
    # The C++ side asserts these same literals in //firmware_common:lma_tx_queue_test
    # and //firmware_common:lma_lmaf_rx_test, so all three are pinned to them.


def test_server_defaults_come_from_the_shared_policy():
    """The server must not restate the numbers: its defaults are the policy."""
    src = SERVER_SOURCE.read_text()
    assert 'os.environ.get("LMAO_LMAF_MAX_RETRIES", str(RETRY_MAX_ATTEMPTS))' in src
    assert "LMAO_LMAF_RETRY_SECONDS" not in src, (
        "the schedule belongs to the shared policy, not to a second knob"
    )


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
