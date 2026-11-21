"""Provenance: which commit a graph was built from, and whether that is current.

``graph.json`` is not a static artifact. On this machine it is rewritten by two
independent writers:

* a **post-commit hook** in the dev checkout (``graphify hook install``), which
  rebuilds from the branch you just committed to;
* a **cron watcher** (``graph-watch-branch.sh``, every 15 min) that rebuilds in a
  detached worktree at ``origin/master`` and rsyncs the result over the same
  ``graphify-out/`` directory.

So the canonical graph can be at *your branch* one minute and at *master* the
next, and it can be a few commits behind either. ``built_at_commit`` is the only
way to tell. Querying ``impact()`` against a graph built from a different commit
silently describes code that has moved — which is exactly the "confident and
wrong" failure this package exists to avoid.

``staleness()`` answers that question without the caller having to think about
it. It degrades to ``status="unknown"`` when git is unavailable, so it never
becomes a hard dependency of the read path.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from graph.model import Graph

_GIT = shutil.which("git")


def _git(args: list[str], cwd: Path, timeout: float = 10.0) -> str | None:
    """Run a git command; return stripped stdout, or None on any failure."""
    if _GIT is None:
        return None
    try:
        proc = subprocess.run(
            [_GIT, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def repo_root(g: Graph | None = None, path: str | Path | None = None) -> Path | None:
    """The git work tree containing the graph, or None.

    ``graph.json`` lives at ``<project>/graphify-out/graph.json``, so the project
    directory is normally the repo root — but resolve it properly anyway, since a
    graph can be synced somewhere else (as the cron watcher does).
    """
    if path is None and g is not None and g.path:
        path = g.path
    if path is None:
        return None
    start = Path(path).expanduser().resolve()
    if start.is_file():
        start = start.parent
    top = _git(["rev-parse", "--show-toplevel"], start)
    return Path(top) if top else None


def resolve_commit(ref: str = "HEAD", cwd: str | Path | None = None) -> str | None:
    """Full commit hash for ``ref``, or None if it does not resolve."""
    base = Path(cwd).expanduser() if cwd else Path.cwd()
    # ``--verify`` + ``^{commit}`` rejects non-commit refs and stops a ref that
    # looks like an option from being parsed as one.
    out = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], base)
    return out or None


def _count(cwd: Path, spec: str) -> int | None:
    out = _git(["rev-list", "--count", spec], cwd)
    try:
        return int(out) if out is not None else None
    except ValueError:
        return None


def staleness(
    g: Graph,
    ref: str = "HEAD",
    *,
    repo: str | Path | None = None,
    check_dirty: bool = False,
) -> dict:
    """Compare the graph's build commit with ``ref``.

    Returns a dict with ``status`` in ``current`` / ``behind`` / ``ahead`` /
    ``diverged`` / ``unknown``, plus commit ids and commit counts in each
    direction. ``behind`` means ``ref`` has commits the graph has not seen —
    the graph is describing older code. ``ahead`` means the graph was built from
    commits not reachable from ``ref`` (e.g. it was built from your branch while
    ``ref`` is ``origin/master``). ``diverged`` is both.

    ``check_dirty`` adds the count of uncommitted files, which the graph also
    cannot reflect: the hook only rebuilds *after* a commit.
    """
    graph_commit = g.built_at_commit
    root = Path(repo).expanduser() if repo else repo_root(g)
    result: dict = {
        "graph": g.path,
        "graph_commit": graph_commit,
        "ref": ref,
        "ref_commit": None,
        "status": "unknown",
        "behind": None,
        "ahead": None,
        "repo": str(root) if root else None,
    }

    if graph_commit is None:
        result["reason"] = "graph has no built_at_commit"
        return result
    if root is None:
        result["reason"] = "no git work tree"
        return result

    ref_commit = resolve_commit(ref, root)
    if ref_commit is None:
        result["reason"] = f"{ref!r} does not resolve"
        return result

    result["ref_commit"] = ref_commit

    # Tolerate a short hash in either field: graphify records whatever rev-parse
    # gave it, and older graphs may carry an abbreviated id.
    if ref_commit.startswith(graph_commit) or graph_commit.startswith(ref_commit):
        result.update(status="current", behind=0, ahead=0)
    else:
        behind = _count(root, f"{graph_commit}..{ref_commit}")
        ahead = _count(root, f"{ref_commit}..{graph_commit}")
        result["behind"] = behind
        result["ahead"] = ahead
        if behind is None or ahead is None:
            # The graph commit is unknown to this repo (different clone, or
            # rewritten history) — say so rather than guessing.
            result["status"] = "unknown"
            result["reason"] = "graph commit not reachable from this repo"
        elif behind and ahead:
            result["status"] = "diverged"
        elif behind:
            result["status"] = "behind"
        elif ahead:
            result["status"] = "ahead"
        else:
            result["status"] = "current"

    if check_dirty:
        porcelain = _git(["status", "--porcelain"], root)
        result["uncommitted_files"] = len(porcelain.splitlines()) if porcelain else 0

    return result


def describe(result: dict) -> str:
    """One-line, human-readable summary of a :func:`staleness` result."""
    status = result.get("status")
    graph_commit = result.get("graph_commit")
    short = graph_commit[:9] if graph_commit else "<none>"
    if status == "current":
        return f"graph is current with {result.get('ref')} ({short})"
    if status == "behind":
        return (
            f"graph is {result.get('behind')} commit(s) BEHIND {result.get('ref')} "
            f"— built at {short}, ref at {str(result.get('ref_commit'))[:9]}"
        )
    if status == "ahead":
        return (
            f"graph is {result.get('ahead')} commit(s) AHEAD of {result.get('ref')} "
            f"— built at {short} (likely from another branch)"
        )
    if status == "diverged":
        return (
            f"graph DIVERGED from {result.get('ref')} "
            f"({result.get('ahead')} ahead, {result.get('behind')} behind) at {short}"
        )
    return f"graph provenance unknown ({result.get('reason', 'no reason given')})"
