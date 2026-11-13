#!/usr/bin/env python3
"""Cluster watchdog: deterministic health checks + agentic recovery.

Two layers, deliberately separated (the same split as cluster-ops/doctor vs
cluster-ops/recovery-agent):

1. **Deterministic checks** (``check``) — kubectl/ssh/HTTP probes only, no
   model.  Cheap enough to run every few minutes, and its verdict never depends
   on an LLM being reachable.
2. **Agentic recovery** (``watch``) — runs only when the checks fail.  A
   headless agent gets the runbook, the report, and a *whitelisted* action CLI
   (``recover``), so judgment calls ("wait vs restart") stay with the agent
   while the blast radius stays in this file.

Every recovery action passes through :class:`RecoveryGuard` (cooldown + attempt
cap), so a wedged workload cannot be restarted in a loop.

Usage::

    python3 tools/cluster_watchdog/watchdog.py check
    python3 tools/cluster_watchdog/watchdog.py check --json
    python3 tools/cluster_watchdog/watchdog.py recover restart-deployment default/lmao-server
    python3 tools/cluster_watchdog/watchdog.py recover delete-pod default/iot-ingest-consumer-abc
    python3 tools/cluster_watchdog/watchdog.py watch [--dry-run]

Exit codes: ``check`` → 0 healthy / 1 degraded; ``recover`` → 0 acted (or
dry-run) / 1 refused; ``watch`` → 0 always (a timer must not flap).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Configuration (environment; see watchdog.env.example)
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY = "op4:5000"
DEFAULT_QUERY_POD = "deploy/iot-ingest-consumer"
DEFAULT_PIPELINE_THRESHOLD_H = 3.0
DEFAULT_DISK_WARN_PCT = 85
DEFAULT_COOLDOWN_SEC = 900
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_AGENT_MODEL = "spark-73dd/qwen3.8-flash-next"
DEFAULT_AGENT_FALLBACKS = "kimi-code/k3"
DEFAULT_AGENT_TIMEOUT = 280
DEFAULT_ALERT_REPEAT_SEC = 21600  # re-alert about an unchanged failure after 6 h

# Recovery actions the agent may request.  Anything else is refused — this is
# the whitelist the runbook refers to.
ALLOWED_ACTIONS = {
    "restart-deployment",
    "delete-pod",
    "delete-job",
    "restart-registry",
    "restart-k3s",
    "renew-network",
}

# Workloads whose availability defines "the stack is up".  Scaled-to-zero
# deployments (the parked rust trio) are checked dynamically instead.
EXPECTED_WORKLOADS = (
    "deploy/default/lmao-server",
    "deploy/default/iot-ingest-consumer",
    "deploy/default/nats-server",
    "sts/default/postgres",
)

CLUSTER_NODES = ("tp1", "tp2", "tp3", "tp4")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """One failed check, with the recovery action that would address it."""

    check: str
    severity: str  # "fail" | "warn"
    detail: str
    action: list[str] | None = None  # e.g. ["restart-deployment", "default/lmao-server"]


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "fail" for i in self.issues)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "checks": self.checks,
                "issues": [asdict(i) for i in self.issues],
            },
            indent=2,
        )

    def to_text(self) -> str:
        lines = [f"cluster-watchdog: {'OK' if self.ok else 'DEGRADED'}"]
        for cid, state in self.checks.items():
            lines.append(f"  {state:<4} {cid}")
        shown_warn: dict[str, int] = {}
        for i in self.issues:
            act = f"  -> {' '.join(i.action)}" if i.action else ""
            if i.severity == "warn":
                # Old Job history repeats every hour; keep the text readable.
                key = i.detail.split(" (")[0].rsplit("-", 2)[0]
                shown_warn[key] = shown_warn.get(key, 0) + 1
                if shown_warn[key] > 1:
                    continue
            lines.append(f"  {i.severity.upper():<4} {i.check}: {i.detail}{act}")
        for key, n in shown_warn.items():
            if n > 1:
                lines.append(f"  WARN {key}: {n - 1} more of the same (history)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run *cmd*, returning ``(returncode, combined output)``; never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.SubprocessError, OSError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def kubectl(*args: str, timeout: int = 30) -> tuple[int, str]:
    return _run(["kubectl", *args], timeout=timeout)


def ssh(node: str, script: str, timeout: int = 20) -> tuple[int, str]:
    return _run(
        ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=6", node, script],
        timeout=timeout,
    )


def http_status(url: str, timeout: int = 5) -> str:
    """HTTP status code for *url* as a string, or ``000`` when unreachable."""
    rc, out = _run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", str(timeout), url],
        timeout=timeout + 2,
    )
    code = out.strip()
    return code if rc == 0 and code else "000"


# ---------------------------------------------------------------------------
# Pure classifiers (unit-tested)
# ---------------------------------------------------------------------------


def classify_pods(
    pods: list[dict[str, Any]],
    now: dt.datetime,
    cronjob_jobs: set[str] | None = None,
) -> list[Issue]:
    """Flag pods that are visibly broken (never Completed/Succeeded ones).

    *cronjob_jobs* holds the names of Jobs owned by a CronJob.  Their failures
    are chronic: the CronJob recreates them on schedule, so deleting the Job
    only buys one quiet tick.  Those are reported as warnings for an operator
    to fix the CronJob, never as an action.
    """
    cronjob_jobs = cronjob_jobs or set()
    issues: list[Issue] = []
    for pod in pods:
        name = pod.get("metadata", {}).get("name", "?")
        ns = pod.get("metadata", {}).get("namespace", "default")
        phase = pod.get("status", {}).get("phase", "?")
        if phase in ("Succeeded", "Completed"):
            continue

        reasons: list[str] = []
        for cs in pod.get("status", {}).get("containerStatuses") or []:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError"):
                reasons.append(reason)
            elif reason == "ContainerCreating":
                pass
            term = (cs.get("state") or {}).get("terminated") or {}
            if term.get("reason") == "Error":
                reasons.append(f"Error(exit {term.get('exitCode')})")

        if phase == "Pending":
            created = pod.get("metadata", {}).get("creationTimestamp")
            age = _age(created, now)
            if age is not None and age > dt.timedelta(minutes=10):
                reasons.append(f"Pending {int(age.total_seconds() // 60)}m")
        if phase == "Failed":
            age = _age(pod.get("metadata", {}).get("creationTimestamp"), now)
            # A failed Job pod older than an hour is history the CronJob's
            # failedJobsHistoryLimit owns — deleting it would only make the
            # failure invisible, so report it as a warning with no action.
            reasons.append(f"Failed {int(age.total_seconds() // 60)}m ago" if age else "Failed")

        if not reasons:
            continue
        stale_failure = phase == "Failed" and _age(
            pod.get("metadata", {}).get("creationTimestamp"), now
        ) is not None and _age(pod.get("metadata", {}).get("creationTimestamp"), now) > dt.timedelta(hours=1)
        # Restarting a pod only helps if a controller owns it.  A bare pod
        # (e.g. a probe) has no owner and would simply vanish.  A Job-owned pod
        # is retried by its Job, so the actionable target is the Job itself.
        owners = pod.get("metadata", {}).get("ownerReferences") or []
        owned = bool(owners)
        job_owner = next((o.get("name") for o in owners if o.get("kind") == "Job"), None)
        chronic = job_owner in cronjob_jobs
        issues.append(
            Issue(
                check="pods",
                severity="warn" if (stale_failure or chronic) else "fail",
                detail=(
                    f"{ns}/{name} ({phase}): {', '.join(sorted(set(reasons)))}"
                    + (f" — recurring CronJob {job_owner.rsplit('-', 1)[0]}" if chronic else "")
                ),
                action=(
                    None if stale_failure or chronic or not owned
                    else (["delete-job", f"{ns}/{job_owner}"] if job_owner
                          else ["delete-pod", f"{ns}/{name}"])
                ),
            )
        )
    return issues


def classify_nodes(nodes: list[dict[str, Any]]) -> list[Issue]:
    """Flag nodes whose Ready condition is not True."""
    issues: list[Issue] = []
    for node in nodes:
        name = node.get("metadata", {}).get("name", "?")
        ready = None
        for cond in node.get("status", {}).get("conditions") or []:
            if cond.get("type") == "Ready":
                ready = cond.get("status")
        if ready != "True":
            issues.append(
                Issue(
                    check="nodes",
                    severity="fail",
                    detail=f"{name} Ready={ready}",
                    action=["restart-k3s", name],
                )
            )
    return issues


def classify_deployments(deploys: list[dict[str, Any]], expected: tuple[str, ...]) -> list[Issue]:
    """Flag expected workloads with fewer available replicas than desired.

    *deploys* carries both Deployments and StatefulSets; *expected* entries are
    ``<kind>/<namespace>/<name>`` with kind in ``deploy``/``sts``.
    """
    by_key = {
        f"{d.get('kind', 'Deployment').lower().replace('deployment', 'deploy').replace('statefulset', 'sts')}"
        f"/{d['metadata'].get('namespace', 'default')}/{d['metadata'].get('name')}": d
        for d in deploys
    }
    issues: list[Issue] = []
    for key in expected:
        d = by_key.get(key)
        if d is None:
            issues.append(
                Issue(check="deployments", severity="fail", detail=f"{key} missing")
            )
            continue
        desired = (d.get("spec") or {}).get("replicas", 1) or 0
        available = (d.get("status") or {}).get("availableReplicas", 0) or 0
        if desired > 0 and available < desired:
            issues.append(
                Issue(
                    check="deployments",
                    severity="fail",
                    detail=f"{key} available {available}/{desired}",
                    action=["restart-deployment", "/".join(key.split("/")[1:])],
                )
            )
    return issues


def parse_dest_hash(text: str) -> str | None:
    """Extract the server's ``DEST_HASH`` from its log lines."""
    m = re.search(r"Delivery destination \(client DEST_HASH\):\s*([0-9a-f]{32})", text)
    return m.group(1) if m else None


def pipeline_age(rows: list[list[Any]], now: dt.datetime) -> dt.timedelta | None:
    """Age of the newest ``ingested_at`` value returned by the query API.

    ``ingested_at`` is the *server's* clock (``CURRENT_TIMESTAMP``), so a client
    whose own clock is skewed cannot fake freshness — the device-side
    ``timestamp_ms`` values are frequently hours off.
    """
    newest: dt.datetime | None = None
    for row in rows:
        if not row or row[0] is None:
            continue
        ts = _parse_ts(str(row[0]))
        if ts is None:
            continue
        if newest is None or ts > newest:
            newest = ts
    return None if newest is None else now - newest


def _parse_ts(value: str) -> dt.datetime | None:
    value = value.strip().replace("+00", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        ts = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _age(created: str | None, now: dt.datetime) -> dt.timedelta | None:
    if not created:
        return None
    ts = _parse_ts(created.replace("Z", "+00:00"))
    return None if ts is None else now - ts


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_api(r: Report) -> bool:
    rc, out = kubectl("version", "--output=json")
    if rc != 0 or "serverVersion" not in out:
        r.checks["api"] = "FAIL"
        r.issues.append(
            Issue(check="api", severity="fail", detail=f"kubectl cannot reach the API: {out.strip()[:200]}")
        )
        return False
    r.checks["api"] = "ok"
    return True


def check_nodes(r: Report) -> None:
    rc, out = kubectl("get", "nodes", "-o", "json")
    if rc != 0:
        r.checks["nodes"] = "FAIL"
        r.issues.append(Issue(check="nodes", severity="fail", detail="kubectl get nodes failed"))
        return
    nodes = json.loads(out).get("items", [])
    issues = classify_nodes(nodes)
    r.checks["nodes"] = "FAIL" if issues else "ok"
    r.issues.extend(issues)


def check_workloads(r: Report) -> None:
    rc_d, out_d = kubectl("get", "deployments", "-A", "-o", "json")
    rc_s, out_s = kubectl("get", "statefulsets", "-A", "-o", "json")
    if rc_d != 0 or rc_s != 0:
        r.checks["deployments"] = "FAIL"
        r.issues.append(Issue(check="deployments", severity="fail", detail="kubectl get deployments/statefulsets failed"))
        return
    workloads = json.loads(out_d).get("items", []) + json.loads(out_s).get("items", [])
    issues = classify_deployments(workloads, EXPECTED_WORKLOADS)
    r.checks["deployments"] = (
        "FAIL" if any(i.severity == "fail" for i in issues)
        else "WARN" if issues else "ok"
    )
    r.issues.extend(issues)

    rc, out = kubectl("get", "pods", "-A", "-o", "json")
    if rc != 0:
        r.checks["pods"] = "FAIL"
        r.issues.append(Issue(check="pods", severity="fail", detail="kubectl get pods failed"))
        return
    rc_j, out_j = kubectl("get", "jobs", "-A", "-o", "json")
    cronjob_jobs: set[str] = set()
    if rc_j == 0:
        for job in json.loads(out_j).get("items", []):
            owners = job.get("metadata", {}).get("ownerReferences") or []
            if any(o.get("kind") == "CronJob" for o in owners):
                cronjob_jobs.add(job["metadata"]["name"])

    now = dt.datetime.now(dt.timezone.utc)
    pod_issues = classify_pods(json.loads(out).get("items", []), now, cronjob_jobs)
    r.checks["pods"] = (
        "FAIL" if any(i.severity == "fail" for i in pod_issues)
        else "WARN" if pod_issues else "ok"
    )
    r.issues.extend(pod_issues)


def check_registry(r: Report, registry: str) -> None:
    """Registry reachable from this host *and* from every node."""
    host = http_status(f"http://{registry}/v2/")
    if host != "200":
        r.checks["registry"] = "FAIL"
        r.issues.append(
            Issue(
                check="registry",
                severity="fail",
                detail=f"{registry} not answering from this host (HTTP {host})",
                action=["restart-registry"],
            )
        )
        return
    unreachable = []
    for node in CLUSTER_NODES:
        rc, out = ssh(
            node,
            f"timeout 5 curl -s -o /dev/null -w '%{{http_code}}' http://{registry}/v2/",
        )
        if rc != 0 or out.strip() != "200":
            unreachable.append(f"{node}({out.strip()[:40] or rc})")
    if unreachable:
        r.checks["registry"] = "FAIL"
        r.issues.append(
            Issue(
                check="registry",
                severity="fail",
                detail=f"{registry} unreachable from: {', '.join(unreachable)}",
            )
        )
    else:
        r.checks["registry"] = "ok"


def check_lmao_server(r: Report, expected_hash: str | None) -> None:
    rc, out = kubectl("-n", "default", "get", "pods", "-l", "app=lmao-server", "-o", "json")
    if rc != 0:
        r.checks["lmao-server"] = "FAIL"
        r.issues.append(Issue(check="lmao-server", severity="fail", detail="cannot list server pods"))
        return
    pods = [p for p in json.loads(out).get("items", []) if p["status"].get("phase") == "Running"]
    if not pods:
        r.checks["lmao-server"] = "FAIL"
        r.issues.append(
            Issue(
                check="lmao-server",
                severity="fail",
                detail="no Running lmao-server pod",
                action=["restart-deployment", "default/lmao-server"],
            )
        )
        return

    # The server re-announces every 10 s, so a tail window only covers minutes;
    # the startup lines that prove RNode/DEST_HASH are at the head of the stream.
    rc, logs = _run(
        ["bash", "-c", "kubectl -n default logs deploy/lmao-server --tail=-1 | head -n 600"],
        timeout=60,
    )
    if rc != 0:
        r.checks["lmao-server"] = "FAIL"
        r.issues.append(Issue(check="lmao-server", severity="fail", detail="cannot read server logs"))
        return

    problems = []
    if "RNodeInterface" not in logs or "configured and powered up" not in logs:
        problems.append("RNode not powered up")
    if "Listening for LXMF messages" not in logs:
        problems.append("LXMF router not listening")
    actual = parse_dest_hash(logs)
    if actual is None:
        problems.append("no delivery destination logged")
    elif expected_hash and actual != expected_hash:
        # Identity is on a PVC; a mismatch means the PVC was replaced.  That is
        # an operator decision, never something to restart into.
        problems.append(f"DEST_HASH drift: expected {expected_hash}, got {actual}")

    if problems:
        r.checks["lmao-server"] = "FAIL"
        r.issues.append(
            Issue(
                check="lmao-server",
                severity="fail",
                detail="; ".join(problems),
                action=None if any("DEST_HASH" in p for p in problems) else ["restart-deployment", "default/lmao-server"],
            )
        )
    else:
        r.checks["lmao-server"] = "ok"


def check_pipeline(r: Report, threshold_h: float) -> None:
    """Pipeline freshness from the server-side ingest clock.

    Runs the query inside the ingest pod (its query API listens on loopback and
    the DuckDB file is on a RWO volume, so an outside port-forward would need
    the same pod anyway).
    """
    probe = (
        "import json,urllib.request\n"
        "out=[]\n"
        "for t in ('sensor_readings','text_messages'):\n"
        "    b=json.dumps({'sql':f'SELECT max(ingested_at) FROM {t}'}).encode()\n"
        "    r=urllib.request.Request('http://127.0.0.1:8080/query',data=b,"
        "headers={'Content-Type':'application/json'})\n"
        "    out.append(json.loads(urllib.request.urlopen(r,timeout=15).read())['rows'])\n"
        "print(json.dumps(out))\n"
    )
    rc, out = kubectl(
        "-n", "default", "exec", DEFAULT_QUERY_POD, "--", "python3", "-c", probe, timeout=60
    )
    if rc != 0:
        r.checks["pipeline"] = "FAIL"
        r.issues.append(
            Issue(
                check="pipeline",
                severity="fail",
                detail=f"query API unreachable in {DEFAULT_QUERY_POD}: {out.strip()[:160]}",
                action=["restart-deployment", "default/iot-ingest-consumer"],
            )
        )
        return
    try:
        tables = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        r.checks["pipeline"] = "FAIL"
        r.issues.append(Issue(check="pipeline", severity="fail", detail="unparseable query result"))
        return

    now = dt.datetime.now(dt.timezone.utc)
    ages = [a for a in (pipeline_age(rows, now) for rows in tables) if a is not None]
    if not ages:
        r.checks["pipeline"] = "FAIL"
        r.issues.append(
            Issue(check="pipeline", severity="fail", detail="no ingested rows at all (pipeline never ran)")
        )
        return
    age = min(ages)
    hours = age.total_seconds() / 3600.0
    if hours > threshold_h:
        r.checks["pipeline"] = "FAIL"
        r.issues.append(
            Issue(
                check="pipeline",
                severity="fail",
                detail=f"no message ingested for {hours:.1f}h (threshold {threshold_h:.1f}h) — "
                "device/LoRa side, not recoverable from here",
            )
        )
    else:
        r.checks["pipeline"] = "ok"


def check_disk(r: Report, warn_pct: int) -> None:
    hot = []
    for node in CLUSTER_NODES:
        rc, out = ssh(node, "df -P / | tail -1 | awk '{print $5}'")
        if rc != 0:
            hot.append(f"{node}(unreachable)")
            continue
        pct = out.strip().rstrip("%")
        if pct.isdigit() and int(pct) >= warn_pct:
            hot.append(f"{node}({pct}%)")
    if hot:
        r.checks["disk"] = "WARN"
        r.issues.append(
            Issue(check="disk", severity="warn", detail=f"root filesystem >= {warn_pct}%: {', '.join(hot)}")
        )
    else:
        r.checks["disk"] = "ok"


def collect(cfg: dict[str, Any] | None = None) -> Report:
    """Run every deterministic check and return the report."""
    cfg = cfg or load_config()
    r = Report()
    if not check_api(r):
        return r
    check_nodes(r)
    check_workloads(r)
    check_registry(r, cfg["registry"])
    check_lmao_server(r, cfg["expected_dest_hash"])
    check_pipeline(r, cfg["pipeline_threshold_h"])
    check_disk(r, cfg["disk_warn_pct"])
    return r


# ---------------------------------------------------------------------------
# Recovery guard + actions
# ---------------------------------------------------------------------------


class RecoveryGuard:
    """Cooldown + attempt cap per action key, persisted across runs."""

    def __init__(self, state_file: Path, cooldown: int, max_attempts: int):
        self.state_file = state_file
        self.cooldown = cooldown
        self.max_attempts = max_attempts

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    def refuse_reason(self, key: str, now: float | None = None) -> str | None:
        """Why *key* must not run now, or ``None`` when it may."""
        now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        entry = self._load().get(key) or {}
        last = float(entry.get("last", 0))
        count = int(entry.get("count", 0))
        if count >= self.max_attempts and now - last < 3600:
            return f"attempt cap reached ({count}/{self.max_attempts} in the last hour) — operator decision"
        if now - last < self.cooldown:
            return f"cooldown: last ran {int(now - last)}s ago (min {self.cooldown}s)"
        return None

    def record(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        state = self._load()
        entry = state.get(key) or {}
        last = float(entry.get("last", 0))
        count = int(entry.get("count", 0))
        # A new hour resets the cap; otherwise the entry would stay exhausted
        # forever.
        if now - last >= 3600:
            count = 0
        state[key] = {"last": now, "count": count + 1}
        self._save(state)


def _act_restart_deployment(target: str, dry: bool) -> tuple[bool, str]:
    ns, _, name = target.partition("/")
    if not name:
        return False, f"expected <namespace>/<deployment>, got {target!r}"
    if dry:
        return True, f"[dry-run] kubectl -n {ns} rollout restart deployment/{name}"
    rc, out = kubectl("-n", ns, "rollout", "restart", f"deployment/{name}", timeout=60)
    if rc != 0:
        return False, out.strip()[:200]
    rc2, out2 = kubectl("-n", ns, "rollout", "status", f"deployment/{name}", "--timeout=240s", timeout=260)
    return rc2 == 0, (out2.strip().splitlines()[-1] if out2.strip() else "restarted")


def _act_delete_job(target: str, dry: bool) -> tuple[bool, str]:
    ns, _, name = target.partition("/")
    if not name:
        return False, f"expected <namespace>/<job>, got {target!r}"
    if dry:
        return True, f"[dry-run] kubectl -n {ns} delete job {name}"
    rc, out = kubectl("-n", ns, "delete", "job", name, "--wait=false", timeout=60)
    return rc == 0, out.strip()[:200]


def _act_delete_pod(target: str, dry: bool) -> tuple[bool, str]:
    ns, _, name = target.partition("/")
    if not name:
        return False, f"expected <namespace>/<pod>, got {target!r}"
    if dry:
        return True, f"[dry-run] kubectl -n {ns} delete pod {name}"
    rc, out = kubectl("-n", ns, "delete", "pod", name, "--wait=false", timeout=60)
    return rc == 0, out.strip()[:200]


def _act_restart_registry(_target: str, dry: bool) -> tuple[bool, str]:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "docker" / "registry" / "manage.sh"
    if dry:
        return True, f"[dry-run] {script} start"
    rc, out = _run([str(script), "start"], timeout=120)
    return rc == 0, out.strip().splitlines()[-1] if out.strip() else "started"


def _act_restart_k3s(node: str, dry: bool) -> tuple[bool, str]:
    if node not in CLUSTER_NODES:
        return False, f"{node!r} is not a cluster node"
    unit = "k3s" if node == "tp3" else "k3s-agent"
    if dry:
        return True, f"[dry-run] ssh {node} systemctl restart {unit}"
    rc, out = ssh(node, f"sudo -n systemctl restart {unit}", timeout=120)
    return rc == 0, out.strip()[:200] or f"{unit} restarted on {node}"


def _act_renew_network(node: str, dry: bool) -> tuple[bool, str]:
    if node not in CLUSTER_NODES:
        return False, f"{node!r} is not a cluster node"
    if dry:
        return True, f"[dry-run] ssh {node} nmcli con up Hankerker"
    rc, out = ssh(node, "nmcli con up Hankerker", timeout=90)
    return rc == 0, out.strip()[:200] or f"network renewed on {node}"


ACTIONS: dict[str, Callable[[str, bool], tuple[bool, str]]] = {
    "restart-deployment": _act_restart_deployment,
    "delete-pod": _act_delete_pod,
    "delete-job": _act_delete_job,
    "restart-registry": _act_restart_registry,
    "restart-k3s": _act_restart_k3s,
    "renew-network": _act_renew_network,
}


def recover(action: str, target: str, *, dry_run: bool, cfg: dict[str, Any]) -> int:
    """Run one whitelisted action through the guard."""
    if action not in ALLOWED_ACTIONS:
        print(f"REFUSED unknown action {action!r}; allowed: {', '.join(sorted(ALLOWED_ACTIONS))}")
        return 1
    key = f"{action}:{target}" if target else action
    guard = RecoveryGuard(
        Path(cfg["state_dir"]) / "recovery-state.json",
        cfg["cooldown_sec"],
        cfg["max_attempts"],
    )
    reason = guard.refuse_reason(key)
    if reason and not dry_run:
        print(f"REFUSED {key}: {reason}")
        return 1
    ok, detail = ACTIONS[action](target, dry_run)
    print(f"{'OK' if ok else 'FAIL'} {action} {target}: {detail}")
    if ok and not dry_run:
        guard.record(key)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Agentic escalation
# ---------------------------------------------------------------------------


def run_agent(prompt: str, cfg: dict[str, Any], workdir: Path) -> tuple[int, str, str]:
    """Run the headless agent, trying the primary model then fallbacks."""
    models = [cfg["agent_model"], *cfg["agent_fallbacks"].split()]
    last = ""
    for model in models:
        cmd = [
            cfg["omp_bin"], "-p", "--no-session", "--model", model,
            "--cwd", str(workdir), prompt,
        ]
        rc, out = _run(cmd, timeout=cfg["agent_timeout"])
        if rc == 0 and not re.search(
            r"exceeded_current_quota|insufficient_balance|401 unauthorized|429 ", out, re.I
        ):
            return rc, out, model
        last = out
        print(f"agent model {model} failed (exit {rc}): {out.strip().splitlines()[:1]}", file=sys.stderr)
    return 1, last, ""


def should_deliver(report: Report, state_dir: Path, repeat_sec: int) -> bool:
    """Rate-limit repeats of the *same* failure so a 15-min timer is not noise.

    A changed failure set alerts immediately; an unchanged one re-alerts only
    after *repeat_sec*.  Recovery actions still run every tick — only the
    message is deduplicated.
    """
    fingerprint = sorted(f"{i.check}:{i.detail}" for i in report.issues if i.severity == "fail")
    path = state_dir / "last-alert.json"
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if previous.get("fingerprint") == fingerprint and now - float(previous.get("at", 0)) < repeat_sec:
        return False
    path.write_text(json.dumps({"fingerprint": fingerprint, "at": now}))
    return True


def deliver(text: str, cfg: dict[str, Any]) -> None:
    """Send an alert through the same path every other cluster alert uses."""
    target = cfg.get("alert_target") or ""
    if not target or not cfg.get("hermes_bin"):
        print("ALERT (no delivery target configured):\n" + text)
        return
    rc, out = _run([cfg["hermes_bin"], "send", "-q", "-t", target, text], timeout=60)
    if rc != 0:
        print(f"ALERT delivery failed (exit {rc}): {out.strip()[:200]}\n{text}")


def watch(cfg: dict[str, Any], *, dry_run: bool) -> int:
    """Check → escalate to the agent only when unhealthy → deliver."""
    report = collect(cfg)
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last-report.json").write_text(report.to_json())
    (state_dir / "last-report.txt").write_text(report.to_text() + "\n")

    if report.ok:
        print(report.to_text())
        return 0

    print(report.to_text())
    workdir = Path(__file__).resolve().parent
    prompt = (
        "You are the LMAO cluster recovery agent. Read runbook.md in the cwd, then "
        f"read {state_dir / 'last-report.txt'} — that is the deterministic probe result; "
        "do not re-probe blindly. Act only through "
        f"`python3 {workdir / 'watchdog.py'} recover <action> <target>`"
        + (" (append --dry-run; recovery is DRY-RUN in this invocation)" if dry_run else "")
        + ". Finish with terse ALERT/WARN/OK lines."
    )
    rc, out, model = run_agent(prompt, cfg, workdir)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    keep = [
        ln for ln in clean.splitlines()
        if re.match(r"^(ALERT|WARN|OK|RECOVERED|RESTART|DOWN|REFUSED|GAVE-UP)\b", ln.strip())
    ]
    summary = "\n".join(keep[:20]) or clean.strip()[:2000]
    log = state_dir / f"agent-{dt.date.today().isoformat()}.log"
    with log.open("a") as fh:
        fh.write(f"===== {dt.datetime.now(dt.timezone.utc).isoformat()} exit={rc} model={model or 'none'} dry_run={dry_run} =====\n")
        fh.write(clean + "\n")

    if rc != 0:
        deliver(f"ALERT cluster-watchdog: agent run failed (exit {rc}) — see {log}\n{report.to_text()}", cfg)
        return 0
    if not should_deliver(report, state_dir, cfg["alert_repeat_sec"]):
        print(f"report unchanged since the last alert — delivery suppressed (log: {log})")
        return 0
    deliver(f"cluster-watchdog [{model}] dry_run={dry_run}\n{summary}", cfg)
    return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _resolve_bin(value: str, name: str) -> str:
    """Expand ``~`` and resolve a bare command name to its absolute path.

    systemd starts the watchdog with a minimal PATH, so a bare ``hermes`` in
    the env file would fail even though it works in an interactive shell.
    """
    path = os.path.expanduser(value)
    if os.path.sep in path:
        return path
    return shutil.which(path) or path


def load_config() -> dict[str, Any]:
    env_file = os.environ.get(
        "WATCHDOG_ENV", os.path.expanduser("~/.config/lmao/cluster-watchdog.env")
    )
    if os.path.isfile(env_file):
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    # The Matrix target lives in the same file every other alert path reads.
    target = os.environ.get("ALERT_TARGET", "")
    matrix_env = os.path.expanduser("~/spark-recovery/matrix-target.env")
    if not target and os.path.isfile(matrix_env):
        for line in Path(matrix_env).read_text().splitlines():
            if line.startswith("MATRIX_ALERT_TARGET="):
                target = line.partition("=")[2].strip().strip('"').strip("'")
    return {
        "registry": os.environ.get("REGISTRY", DEFAULT_REGISTRY),
        "expected_dest_hash": os.environ.get("EXPECTED_DEST_HASH") or None,
        "pipeline_threshold_h": float(os.environ.get("PIPELINE_THRESHOLD_H", DEFAULT_PIPELINE_THRESHOLD_H)),
        "disk_warn_pct": int(os.environ.get("DISK_WARN_PCT", DEFAULT_DISK_WARN_PCT)),
        "cooldown_sec": int(os.environ.get("RECOVERY_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)),
        "max_attempts": int(os.environ.get("RECOVERY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        "state_dir": os.path.expanduser(
            os.environ.get("STATE_DIR", "~/.local/state/lmao-watchdog")
        ),
        "agent_model": os.environ.get("AGENT_MODEL", DEFAULT_AGENT_MODEL),
        "agent_fallbacks": os.environ.get("AGENT_MODEL_FALLBACKS", DEFAULT_AGENT_FALLBACKS),
        "agent_timeout": int(os.environ.get("AGENT_TIMEOUT_SEC", DEFAULT_AGENT_TIMEOUT)),
        "omp_bin": _resolve_bin(os.environ.get("OMP_BIN", "~/.local/bin/omp"), "omp"),
        "hermes_bin": _resolve_bin(os.environ.get("HERMES_BIN", "hermes"), "hermes"),
        "alert_target": target,
        "alert_repeat_sec": int(os.environ.get("ALERT_REPEAT_SEC", DEFAULT_ALERT_REPEAT_SEC)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="run the deterministic checks")
    p_check.add_argument("--json", action="store_true", help="emit JSON instead of text")

    p_rec = sub.add_parser("recover", help="run one whitelisted recovery action")
    p_rec.add_argument("action", choices=sorted(ALLOWED_ACTIONS))
    p_rec.add_argument("target", nargs="?", default="", help="e.g. default/lmao-server or tp1")
    p_rec.add_argument("--dry-run", action="store_true")

    p_watch = sub.add_parser("watch", help="check, then escalate to the agent when unhealthy")
    p_watch.add_argument("--dry-run", action="store_true", help="agent may not mutate anything")

    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd == "check":
        report = collect(cfg)
        print(report.to_json() if args.json else report.to_text())
        return 0 if report.ok else 1
    if args.cmd == "recover":
        return recover(args.action, args.target, dry_run=args.dry_run, cfg=cfg)
    return watch(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
