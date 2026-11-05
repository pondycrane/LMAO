"""Tests for the shared RoboDog flash plumbing (flash_common.sh).

The three firmware flash scripts (calendar / hermes voice / esp-claw) used to
carry ~40 duplicated lines each and had already drifted: the "an option-shaped
value is an ERROR" fix existed in two of them and not the third, so
`--port --kid-name juliet` silently flashed the default port on that one.
Every script now sources `flash_common.sh`, and the validation must behave
identically for all three.
"""

import os
import sys
import shutil
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


# ── NVS provisioning gate ──────────────────────────────────────────────────
# An EXPLICIT NVS flag must be what writes NVS.  A credential that merely
# EXISTS (env or the local local_secrets.h) is a value source, not a request:
# keying the wipe off "we happen to have a SSID" made a plain
# `flash_firmware --port ...` wipe the 24 KB partition.

def _fake_workspace(tmp_path, *, secrets=False):
    """A throwaway workspace with the real scripts + stubbed python3/docker."""
    app = tmp_path / "robodog_client"
    (app / "build").mkdir(parents=True)
    for name in ("flash.sh", "flash_common.sh"):
        shutil.copy(os.path.join(REPO, "robodog_client", name), app / name)
    if secrets:
        (app / "main").mkdir()
        (app / "main" / "local_secrets.h").write_text(
            '#define RD_WIFI_SSID_DEFAULT   "Hankerker"\n'
            '#define RD_WIFI_PASS_DEFAULT   "secret"\n'
            '#define RD_SERVER_HOST_DEFAULT "tp4"\n'
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'echo "SHIM python3 $*"\n'
    )
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\necho \"SHIM docker $*\"\n"
    )
    for name in ("python3", "docker"):
        (bin_dir / name).chmod(0o755)
    return app, bin_dir


def _run_in_workspace(app, bin_dir, *args, env=None):
    merged = dict(os.environ)
    merged["BUILD_WORKSPACE_DIRECTORY"] = str(app.parent)
    merged["PATH"] = f"{bin_dir}:{merged['PATH']}"
    merged.pop("LMAO_WIFI_SSID", None)
    merged.pop("LMAO_WIFI_PASS", None)
    merged.pop("LMAO_SERVER_HOST", None)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(app / "flash.sh"), *args],
        capture_output=True, text=True, env=merged, timeout=60,
    )


def test_plain_flash_never_writes_nvs(tmp_path):
    """No NVS flags at all leaves the 24 KB partition untouched — even when a
    local local_secrets.h happens to exist (the regression that made a plain
    `flash_firmware --port ...` wipe the partition)."""
    app, bin_dir = _fake_workspace(tmp_path, secrets=True)
    r = _run_in_workspace(app, bin_dir, "--port", "/dev/ttyACM0")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "SHIM python3 -m esptool" in r.stdout
    assert "0x9000" not in r.stdout
    assert "SHIM docker" not in r.stdout
    assert "NVS untouched" in r.stdout


def test_nvs_write_without_credentials_refuses(tmp_path):
    app, bin_dir = _fake_workspace(tmp_path, secrets=False)
    r = _run_in_workspace(app, bin_dir, "--port", "/dev/ttyACM0",
                          "--kid-name", "juliet")
    assert r.returncode == 2
    assert "writing NVS needs WiFi credentials" in r.stderr
    assert "SHIM python3 -m esptool" not in r.stdout


def test_kid_name_flash_carries_credentials_and_kid(tmp_path):
    app, bin_dir = _fake_workspace(tmp_path, secrets=True)
    r = _run_in_workspace(app, bin_dir, "--port", "/dev/ttyACM0",
                          "--kid-name", "juliet")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "0x9000" in r.stdout
    assert "kid_name='juliet'" in r.stdout
    # The credentials came from the local secrets file (a value source only).
    assert "server_host=tp4" in r.stdout


def test_explicit_wifi_flags_trigger_the_nvs_write(tmp_path):
    app, bin_dir = _fake_workspace(tmp_path, secrets=False)
    r = _run_in_workspace(app, bin_dir, "--port", "/dev/ttyACM0",
                          "--wifi-ssid", "ssid", "--wifi-pass", "pw")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "0x9000" in r.stdout
    assert "NVS untouched" not in r.stdout


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
