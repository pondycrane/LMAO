"""Tests for the shared RoboDog flash plumbing (flash_common.sh).

The three firmware flash scripts (calendar / hermes voice / esp-claw) used to
carry ~40 duplicated lines each and had already drifted: the "an option-shaped
value is an ERROR" fix existed in two of them and not the third, so
`--port --kid-name juliet` silently flashed the default port on that one.
Every script now sources `flash_common.sh`, and the validation must behave
identically for all three.
"""

import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = [
    "robodog_client/flash.sh",
    "robodog_client/xiaozhi_flash.sh",
    "robodog_client/esp-claw/flash_esp_claw.sh",
]


def _run(script, *args, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", os.path.join(REPO, script), *args],
        capture_output=True, text=True, env=merged, timeout=60,
    )


def test_flags_are_parsed_by_the_shared_helper():
    """All three scripts source flash_common.sh instead of copying the
    preamble/arg parsing/esptool check."""
    for script in SCRIPTS:
        with open(os.path.join(REPO, script), encoding="utf-8") as f:
            src = f.read()
        assert "flash_common.sh" in src, script
        assert "rd_flash_init" in src, script
        assert "rd_require_host_esptool" in src, script
        # The old per-script copies must be gone.
        assert "import esptool\" >/dev/null" not in src, script


def test_port_without_value_is_an_error_everywhere():
    """`--port` followed by another flag must FAIL (exit 2), never fall back to
    the default port — the drift this helper removes."""
    for script in SCRIPTS:
        r = _run(script, "--port")
        assert r.returncode == 2, (script, r.stdout, r.stderr)
        assert "--port requires a value" in r.stderr, script


def test_port_with_option_shaped_value_is_an_error():
    for script in SCRIPTS:
        r = _run(script, "--port", "--no-nvs")
        assert r.returncode == 2, (script, r.stderr)
        assert "--port requires a value" in r.stderr, script


def test_kid_name_without_value_is_an_error():
    """Only an explicit `--kid-name ""` may clear a board to family mode; a
    trailing `--kid-name` must not alias onto the destructive empty-NVS wipe."""
    r = _run("robodog_client/flash.sh", "--kid-name")
    assert r.returncode == 2
    assert "--kid-name requires a value" in r.stderr
    assert 'use --kid-name "" to clear to family' in r.stderr


def test_unknown_argument_is_rejected():
    for script in SCRIPTS:
        r = _run(script, "--bogus")
        assert r.returncode == 2, script
        assert "unknown argument" in r.stderr or "unknown arg" in r.stderr, script


def test_nvs_csv_uses_the_firmware_namespace():
    """The NVS namespace/key written by flash.sh must match rd_store.h
    (RD_NVS_NAMESPACE / RD_NVS_KEY_KID), or config_load() never sees kid_name."""
    with open(os.path.join(REPO, "robodog_client/flash.sh"), encoding="utf-8") as f:
        src = f.read()
    assert "robodog,namespace,," in src
    assert "kid_name,data,string" in src

    with open(os.path.join(REPO, "robodog_client/main/rd_store.h"),
              encoding="utf-8") as f:
        hdr = f.read()
    assert '#define RD_NVS_NAMESPACE  "robodog"' in hdr
    assert '#define RD_NVS_KEY_KID    "kid_name"' in hdr
