"""Unit tests for the cluster watchdog's classification + recovery guard.

These cover the decisions that are easy to get wrong and expensive to discover
in production: which pod states are actionable, what the guard refuses, and
that pipeline freshness is read from the *server* clock (the device clock is
routinely hours off — that bug shipped in the old pipeline monitor).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.cluster_watchdog import watchdog  # noqa: E402

NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc)


def _pod(name, phase="Running", waiting=None, terminated=None, owner=None, age_min=5, ns="default"):
    created = (NOW - dt.timedelta(minutes=age_min)).isoformat().replace("+00:00", "Z")
    status = {"phase": phase}
    if waiting or terminated:
        state = {}
        if waiting:
            state["waiting"] = {"reason": waiting}
        if terminated:
            state["terminated"] = terminated
        status["containerStatuses"] = [{"name": "c", "state": state}]
    meta = {"name": name, "namespace": ns, "creationTimestamp": created}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1]}]
    return {"metadata": meta, "status": status}


# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------


def test_crashloop_pod_owned_by_replicaset_is_actionable():
    issues = watchdog.classify_pods(
        [_pod("iot-abc", waiting="CrashLoopBackOff", owner=("ReplicaSet", "iot-1"))], NOW
    )
    assert len(issues) == 1
    assert issues[0].severity == "fail"
    assert issues[0].action == ["delete-pod", "default/iot-abc"]


def test_job_owned_pod_targets_the_job_not_the_pod():
    """Deleting a Job's pod just makes the Job retry; the Job is the target."""
    issues = watchdog.classify_pods(
        [_pod("edison-1-xyz", waiting="ImagePullBackOff", owner=("Job", "edison-1"))], NOW
    )
    assert issues[0].action == ["delete-job", "default/edison-1"]


def test_bare_pod_with_no_owner_has_no_action():
    """A bare pod would simply vanish; the watchdog must not delete it."""
    issues = watchdog.classify_pods([_pod("netprobe", waiting="ErrImagePull")], NOW)
    assert issues[0].action is None


def test_pending_pod_is_only_flagged_after_ten_minutes():
    fresh = _pod("new", phase="Pending", age_min=2)
    stale = _pod("old", phase="Pending", age_min=30)
    assert watchdog.classify_pods([fresh], NOW) == []
    issues = watchdog.classify_pods([stale], NOW)
    assert issues and "Pending 30m" in issues[0].detail


def test_succeeded_pods_are_ignored():
    assert watchdog.classify_pods([_pod("done", phase="Succeeded")], NOW) == []


def test_old_failed_job_history_is_a_warning_with_no_action():
    """Failed Job pods pile up hourly; deleting them hides the failure."""
    issues = watchdog.classify_pods(
        [_pod("sync-1", phase="Failed", terminated={"reason": "Error", "exitCode": 1},
              owner=("Job", "sync-1"), age_min=180)],
        NOW,
    )
    assert issues[0].severity == "warn"
    assert issues[0].action is None


def test_cronjob_owned_job_failure_is_chronic_not_actionable():
    """The CronJob recreates it on schedule; deleting buys one quiet tick."""
    issues = watchdog.classify_pods(
        [_pod("watchdog-1-abc", waiting="CrashLoopBackOff", owner=("Job", "watchdog-1"))],
        NOW,
        cronjob_jobs={"watchdog-1"},
    )
    assert issues[0].severity == "warn"
    assert issues[0].action is None
    assert "recurring CronJob watchdog" in issues[0].detail


def test_recent_failed_pod_is_actionable():
    issues = watchdog.classify_pods(
        [_pod("sync-2", phase="Failed", terminated={"reason": "Error", "exitCode": 1},
              owner=("Job", "sync-2"), age_min=5)],
        NOW,
    )
    assert issues[0].severity == "fail"
    assert issues[0].action == ["delete-job", "default/sync-2"]


# ---------------------------------------------------------------------------
# Nodes / workloads
# ---------------------------------------------------------------------------


def _node(name, ready):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


def test_not_ready_node_gets_k3s_restart_action():
    issues = watchdog.classify_nodes([_node("tp1", "True"), _node("tp2", "False")])
    assert len(issues) == 1
    assert issues[0].action == ["restart-k3s", "tp2"]


def _workload(kind, name, desired, available, ns="default"):
    return {
        "kind": kind,
        "metadata": {"name": name, "namespace": ns},
        "spec": {"replicas": desired},
        "status": {"availableReplicas": available},
    }


def test_deployments_and_statefulsets_are_matched_by_kind():
    expected = ("deploy/default/app", "sts/default/db")
    workloads = [_workload("Deployment", "app", 1, 1), _workload("StatefulSet", "db", 1, 0)]
    issues = watchdog.classify_deployments(workloads, expected)
    assert len(issues) == 1
    assert "sts/default/db" in issues[0].detail
    assert issues[0].action == ["restart-deployment", "default/db"]


def test_missing_workload_is_reported_without_an_action():
    issues = watchdog.classify_deployments([], ("deploy/default/app",))
    assert issues[0].detail == "deploy/default/app missing"
    assert issues[0].action is None


def test_scaled_to_zero_workload_is_not_a_failure():
    issues = watchdog.classify_deployments([_workload("Deployment", "parked", 0, 0)], ("deploy/default/parked",))
    assert issues == []


# ---------------------------------------------------------------------------
# lmao-server identity + pipeline freshness
# ---------------------------------------------------------------------------


def test_parse_dest_hash_from_server_log():
    log = "[INFO] Delivery destination (client DEST_HASH): dad35b80164b25f7b1474be86e443702\n"
    assert watchdog.parse_dest_hash(log) == "dad35b80164b25f7b1474be86e443702"
    assert watchdog.parse_dest_hash("no hash here") is None


def test_pipeline_age_uses_server_ingested_at_not_device_timestamp():
    """A device clock hours behind must not read as freshness."""
    rows = [["2026-11-13 11:30:00.500+00"], ["2026-11-13 11:59:00.000+00"]]
    assert watchdog.pipeline_age(rows, NOW) == dt.timedelta(minutes=1)


def test_pipeline_age_ignores_null_rows():
    assert watchdog.pipeline_age([[None]], NOW) is None
    assert watchdog.pipeline_age([], NOW) is None


# ---------------------------------------------------------------------------
# Recovery guard
# ---------------------------------------------------------------------------


def _guard(tmp_path, cooldown=900, attempts=3):
    return watchdog.RecoveryGuard(tmp_path / "state.json", cooldown, attempts)


def test_guard_allows_first_action(tmp_path):
    assert _guard(tmp_path).refuse_reason("restart-deployment:default/x", now=1000) is None


def test_guard_enforces_cooldown(tmp_path):
    g = _guard(tmp_path)
    g.record("a", now=1000)
    assert "cooldown" in g.refuse_reason("a", now=1100)


def test_guard_cooldown_expires(tmp_path):
    g = _guard(tmp_path, cooldown=900)
    g.record("a", now=1000)
    assert g.refuse_reason("a", now=1901) is None


def test_guard_gives_up_after_attempt_cap(tmp_path):
    g = _guard(tmp_path, cooldown=0, attempts=3)
    for t in (1000, 1100, 1200):
        assert g.refuse_reason("a", now=t) is None
        g.record("a", now=t)
    assert "attempt cap" in g.refuse_reason("a", now=1300)


def test_guard_cap_resets_after_an_hour(tmp_path):
    g = _guard(tmp_path, cooldown=0, attempts=1)
    g.record("a", now=1000)
    assert "attempt cap" in g.refuse_reason("a", now=1200)
    assert g.refuse_reason("a", now=1000 + 3601) is None


# ---------------------------------------------------------------------------
# recover()
# ---------------------------------------------------------------------------


def _cfg(tmp_path):
    return {"state_dir": str(tmp_path), "cooldown_sec": 900, "max_attempts": 3}


def test_recover_refuses_unknown_action(tmp_path, capsys):
    assert watchdog.recover("rm-rf-everything", "", dry_run=False, cfg=_cfg(tmp_path)) == 1
    assert "REFUSED unknown action" in capsys.readouterr().out


def test_recover_dry_run_does_not_record_state(tmp_path, monkeypatch, capsys):
    calls = []

    def fake(target, dry):
        calls.append((target, dry))
        return True, "[dry-run]"

    monkeypatch.setitem(watchdog.ACTIONS, "delete-pod", fake)
    assert watchdog.recover("delete-pod", "default/x", dry_run=True, cfg=_cfg(tmp_path)) == 0
    assert calls == [("default/x", True)]
    assert not (tmp_path / "recovery-state.json").exists()


def test_recover_records_state_on_success(tmp_path, monkeypatch):
    monkeypatch.setitem(watchdog.ACTIONS, "delete-pod", lambda t, d: (True, "ok"))
    assert watchdog.recover("delete-pod", "default/x", dry_run=False, cfg=_cfg(tmp_path)) == 0
    state = json.loads((tmp_path / "recovery-state.json").read_text())
    assert state["delete-pod:default/x"]["count"] == 1


def test_recover_refuses_when_cooldown_active(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(watchdog.ACTIONS, "delete-pod", lambda t, d: (True, "ok"))
    cfg = _cfg(tmp_path)
    watchdog.recover("delete-pod", "default/x", dry_run=False, cfg=cfg)
    assert watchdog.recover("delete-pod", "default/x", dry_run=False, cfg=cfg) == 1
    assert "REFUSED delete-pod:default/x" in capsys.readouterr().out


def test_recover_failure_does_not_consume_the_cap(tmp_path, monkeypatch):
    monkeypatch.setitem(watchdog.ACTIONS, "delete-pod", lambda t, d: (False, "boom"))
    assert watchdog.recover("delete-pod", "default/x", dry_run=False, cfg=_cfg(tmp_path)) == 1
    assert not (tmp_path / "recovery-state.json").exists()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def test_report_ok_ignores_warnings_but_not_failures():
    warn = watchdog.Report(issues=[watchdog.Issue("pods", "warn", "history")])
    fail = watchdog.Report(issues=[watchdog.Issue("pods", "fail", "broken")])
    assert warn.ok
    assert not fail.ok


def test_should_deliver_alerts_once_for_an_unchanged_failure(tmp_path):
    report = watchdog.Report(issues=[watchdog.Issue("pods", "fail", "default/x broken")])
    assert watchdog.should_deliver(report, tmp_path, repeat_sec=21600)
    assert not watchdog.should_deliver(report, tmp_path, repeat_sec=21600)


def test_should_deliver_again_when_the_failure_set_changes(tmp_path):
    first = watchdog.Report(issues=[watchdog.Issue("pods", "fail", "default/x broken")])
    second = watchdog.Report(issues=[watchdog.Issue("nodes", "fail", "tp2 NotReady")])
    assert watchdog.should_deliver(first, tmp_path, repeat_sec=21600)
    assert watchdog.should_deliver(second, tmp_path, repeat_sec=21600)


def test_should_deliver_repeats_after_the_repeat_window(tmp_path):
    report = watchdog.Report(issues=[watchdog.Issue("pods", "fail", "default/x broken")])
    assert watchdog.should_deliver(report, tmp_path, repeat_sec=0)
    assert watchdog.should_deliver(report, tmp_path, repeat_sec=0)


def test_report_text_collapses_repeated_history():
    issues = [
        watchdog.Issue("pods", "warn", f"default/sync-{i} (Failed): Error(exit 1)") for i in range(4)
    ]
    text = watchdog.Report(issues=issues).to_text()
    assert "3 more of the same" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
