"""Tests for the calendar-sync CronJob deployment path.

The calendar sync moved from a host Hermes cron
(``~/.hermes/scripts/lmao_kids_sync.sh``) into the cluster.  What must hold:

  - ``install_all --include-services`` deploys it (and ``--skip-calendar-sync``
    opts out), so a plain "deploy the services" keeps the calendar refreshing;
  - the Google token is never committed — it is copied into the
    ``lmao-gcal-token`` Secret from outside the repo, and a missing token fails
    loudly instead of deploying a CronJob that cannot read Google;
  - the manifest's schedule/pins/credentials stay what the panel depends on.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_install_all_deploys_calendar_sync():
    src = _read("tools/install_all.py")
    assert "deploy_calendar_sync" in src
    # Deployed on the same path as the other services...
    assert re.search(r"deploy_calendar_sync\(cal_result\)", src)
    # ...with its own result row in the summary table.
    assert 'DeviceResult("Calendar Sync CronJob")' in src


def test_skip_flag_exists():
    src = _read("tools/install_all.py")
    assert '"--skip-calendar-sync"' in src
    assert "cal_result.skip(\"--skip-calendar-sync\")" in src


def test_cronjob_manifest_contract():
    """The manifest values the panel's refresh cadence depends on."""
    src = _read("k8s/calendar-sync.yaml")
    assert "kind: CronJob" in src
    assert "name: lmao-calendar-sync" in src
    # Hourly: bounds how stale an event's forecast can be (see README).
    assert re.search(r'schedule:\s*"0 \* \* \* \*"', src)
    assert "concurrencyPolicy: Forbid" in src
    # Registry image, pinned to the node whose k3s has the registry mirror.
    assert "op4:5000/lmao-calendar-sync:latest" in src
    assert "kubernetes.io/hostname: tp4" in src
    # Both stages of the retired host cron run, in order.
    assert "tools/calendar_sync.py --window-days 14" in src
    assert "tools/calendar_pics_sync.py" in src
    assert src.index("tools/calendar_sync.py") < src.index("tools/calendar_pics_sync.py")


def test_manifest_takes_credentials_from_secret_not_literals():
    src = _read("k8s/calendar-sync.yaml")
    # Postgres parts come from the shared Secret.
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert f"key: {key}" in src
    # Google token is a mounted read-only Secret file, never inline.
    assert "secretName: lmao-gcal-token" in src
    assert "readOnly: true" in src
    assert 'value: "/secrets/google_token.json"' in src
    # No token material in the manifest: a real token is a JSON object with
    # these keys — none may appear as literal values here.
    for key in ('"client_secret"', '"refresh_token":', '"access_token"'):
        assert key not in src


def test_dockerfile_has_the_stages_dependencies():
    src = _read("Dockerfile.calendar-sync")
    # calendar_sync.py imports the Google client libs...
    assert "google-api-python-client" in src
    assert "google-auth" in src
    # ...and calendar_pics_sync.py needs Pillow + the shared lma_core store.
    assert "Pillow" in src
    assert "psycopg" in src
    assert "COPY lma_core/" in src
    assert "COPY tools/" in src
    # Deliberately not the server image: the INSTALLED dependency set carries
    # no RNS/LXMF/gRPC (the comment may mention them; the pip list may not).
    pip_block = src.split("RUN pip install", 1)[1]
    for dep in ("rns", "lxmf", "grpcio"):
        assert dep not in pip_block.lower()


def test_deploy_helper_refreshes_token_secret_idempotently():
    src = _read("tools/install_services.py")
    # create --dry-run=client | apply: re-deploys must UPDATE, not fail on
    # "already exists" (the token rotates).
    assert "--dry-run=client" in src
    assert '["kubectl", "apply", "-f", "-"]' in src
    # Missing token is a hard FAIL, not a silent skip.
    assert "Google token not found" in src


def test_missing_token_fails_loudly(tmp_path, monkeypatch):
    """A deploy with no token must FAIL — otherwise the CronJob ships and
    silently never reads Google Calendar."""
    import tools.install_services as svc

    monkeypatch.setenv("LMAO_GCAL_TOKEN", str(tmp_path / "absent.json"))
    monkeypatch.setattr(svc.shutil, "which", lambda _n: "/usr/bin/true")

    class _Result:
        status = None
        message = ""

        def fail(self, msg):
            self.status = "FAIL"
            self.message = msg

    r = _Result()
    assert svc._ensure_gcal_token_secret(r) is False
    assert r.status == "FAIL"
    assert "Google token not found" in r.message


def test_gcal_token_default_path_is_outside_the_repo():
    """The token lives in the operator's home, never in the repo tree."""
    import tools.install_services as svc

    path = os.path.expanduser(svc.GCAL_TOKEN_DEFAULT)
    assert not path.startswith(REPO_ROOT + os.sep), (
        f"token default {path} must not be inside the repo"
    )
    assert path.endswith("google_token.json")
