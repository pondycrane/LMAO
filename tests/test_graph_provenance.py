"""Tests for ``graph.provenance`` — which commit the graph reflects.

Two writers rewrite ``graph.json`` on this machine: a post-commit hook (the dev
branch) and a cron watcher (``origin/master``). So "is this graph describing the
code I am looking at?" is a real question, not a nicety.

Most cases monkeypatch the git runner so the suite stays hermetic. The
end-to-end cases build a throwaway git repo and are skipped when git is absent.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import graph
from graph import provenance
from graph.model import Graph

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _payload(commit):
    return {
        "directed": True,
        "multigraph": False,
        "built_at_commit": commit,
        "nodes": [
            {
                "id": "a_mod_foo",
                "label": "foo()",
                "norm_label": "foo()",
                "community": 1,
                "community_name": "comm-1",
                "file_type": "code",
                "source_file": "a/mod.py",
                "source_location": "L1",
            }
        ],
        "links": [],
        "hyperedges": [],
    }


def _write_graph(root: Path, commit):
    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(_payload(commit)), encoding="utf-8")
    return root


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A git repo with two commits; returns (root, first_commit, head_commit)."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    # Real repos gitignore graphify-out (LMAO does), so the graph artifact must
    # not show up as an uncommitted file in the dirty check.
    (tmp_path / ".gitignore").write_text("graphify-out/\n", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "mod.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "a" / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "second")
    head = _git(tmp_path, "rev-parse", "HEAD")
    return tmp_path, first, head


# ── real-git end to end ──────────────────────────────────────────────


@needs_git
def test_current_when_built_at_head(repo):
    root, _, head = repo
    _write_graph(root, head)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g)
    assert result["status"] == "current"
    assert result["behind"] == 0
    assert result["ahead"] == 0


@needs_git
def test_behind_when_head_advanced(repo):
    root, first, _ = repo
    _write_graph(root, first)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g)
    assert result["status"] == "behind"
    assert result["behind"] == 1


@needs_git
def test_ahead_when_graph_built_from_other_branch(repo):
    root, _, head = repo
    # Build a commit not reachable from the first commit, then point the graph
    # at it while comparing against the older ref: graph is "ahead".
    _git(root, "checkout", "-q", "-b", "other")
    (root / "a" / "mod.py").write_text("def foo():\n    return 2\n", encoding="utf-8")
    _git(root, "commit", "-qam", "third")
    other = _git(root, "rev-parse", "HEAD")
    _write_graph(root, other)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g, head)
    assert result["status"] == "ahead"
    assert result["ahead"] == 1
    assert result["behind"] == 0


@needs_git
def test_diverged(repo):
    root, first, head = repo
    _git(root, "checkout", "-q", "-b", "side", first)
    (root / "a" / "mod.py").write_text("def foo():\n    return 3\n", encoding="utf-8")
    _git(root, "commit", "-qam", "side")
    side = _git(root, "rev-parse", "HEAD")
    _write_graph(root, side)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g, head)
    assert result["status"] == "diverged"
    assert result["ahead"] == 1 and result["behind"] == 1


@needs_git
def test_short_hash_tolerated(repo):
    root, _, head = repo
    _write_graph(root, head[:9])
    g = graph.load(root, use_cache=False)
    assert provenance.staleness(g)["status"] == "current"


@needs_git
def test_dirty_counts_uncommitted(repo):
    root, _, head = repo
    _write_graph(root, head)
    (root / "a" / "mod.py").write_text("def foo():\n    return 4\n", encoding="utf-8")
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g, check_dirty=True)
    assert result["uncommitted_files"] == 1


@needs_git
def test_unknown_commit_reported_not_guessed(repo):
    root, _, _ = repo
    _write_graph(root, "0" * 40)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g)
    assert result["status"] == "unknown"
    assert "reason" in result


# ── degraded paths (no git / no commit) ─────────────────────────────


def test_missing_built_at_commit_is_unknown(tmp_path):
    _write_graph(tmp_path, None)
    g = graph.load(tmp_path, use_cache=False)
    result = provenance.staleness(g)
    assert result["status"] == "unknown"
    assert "built_at_commit" in result["reason"]


def test_bad_ref_is_unknown(repo):
    root, _, head = repo
    _write_graph(root, head)
    g = graph.load(root, use_cache=False)
    result = provenance.staleness(g, "definitely-not-a-ref")
    assert result["status"] == "unknown"
    assert "does not resolve" in result["reason"]


def test_no_git_binary_degrades(monkeypatch, tmp_path):
    _write_graph(tmp_path, "a" * 40)
    monkeypatch.setattr(provenance, "_GIT", None)
    g = graph.load(tmp_path, use_cache=False)
    result = provenance.staleness(g)
    assert result["status"] == "unknown"


def test_describe_covers_every_status():
    base = {"ref": "HEAD", "graph_commit": "abcdef1234567890", "ref_commit": "1234567890abcdef"}
    assert "current" in provenance.describe({**base, "status": "current"})
    assert "BEHIND" in provenance.describe({**base, "status": "behind", "behind": 2})
    assert "AHEAD" in provenance.describe({**base, "status": "ahead", "ahead": 1})
    assert "DIVERGED" in provenance.describe(
        {**base, "status": "diverged", "ahead": 1, "behind": 1}
    )
    assert "unknown" in provenance.describe({"status": "unknown", "reason": "nope"})


# ── the graph is a moving target: cache must follow rebuilds ─────────


def test_cache_invalidates_when_graph_rebuilt(tmp_path):
    _write_graph(tmp_path, "a" * 40)
    first = graph.load(tmp_path)
    assert first.built_at_commit == "a" * 40

    # Simulate a rebuild landing in place (hook or cron rsync).
    (tmp_path / "graphify-out" / "graph.json").write_text(
        json.dumps(_payload("b" * 40)), encoding="utf-8"
    )
    second = graph.load(tmp_path)
    assert second is not first
    assert second.built_at_commit == "b" * 40


def test_cache_reuses_when_graph_unchanged(tmp_path):
    _write_graph(tmp_path, "a" * 40)
    assert graph.load(tmp_path) is graph.load(tmp_path)


def test_loaded_graph_is_a_stable_snapshot(tmp_path):
    """A Graph already in hand must not mutate when the file is replaced."""
    _write_graph(tmp_path, "a" * 40)
    g = graph.load(tmp_path, use_cache=False)
    (tmp_path / "graphify-out" / "graph.json").write_text(
        json.dumps(_payload("c" * 40)), encoding="utf-8"
    )
    assert g.built_at_commit == "a" * 40  # unchanged in memory
    assert graph.load(tmp_path, use_cache=False).built_at_commit == "c" * 40


def test_impact_carries_graph_commit(tmp_path):
    from graph import queries

    _write_graph(tmp_path, "d" * 40)
    g = graph.load(tmp_path, use_cache=False)
    result = queries.impact(g, ["a/mod.py"])
    assert result["graph_commit"] == "d" * 40


# ── CLI ─────────────────────────────────────────────────────────────


@needs_git
def test_cli_staleness(repo, capsys):
    root, _, head = repo
    _write_graph(root, head)
    from graph.__main__ import main

    assert main(["--graph", str(root), "staleness"]) == 0
    out = capsys.readouterr().out
    assert "current" in out
    assert head[:9] in out


def test_cli_summary_includes_provenance(tmp_path, capsys):
    _write_graph(tmp_path, "e" * 40)
    from graph.__main__ import main

    assert main(["--graph", str(tmp_path), "summary"]) == 0
    assert "provenance:" in capsys.readouterr().out


def test_graph_class_has_no_hidden_git_dependency(tmp_path):
    """Importing and querying must work with no repo at all."""
    _write_graph(tmp_path, "f" * 40)
    g = Graph(_payload("f" * 40), path=tmp_path / "graphify-out" / "graph.json")
    assert g.meta()["nodes"] == 1
    assert provenance.staleness(g)["status"] == "unknown"
