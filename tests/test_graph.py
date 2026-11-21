"""Tests for the ``graph`` package (issue #315, P4).

The graph under test is synthetic — no dependency on a real ``graphify-out`` —
so these run anywhere, including CI without a built graph. The schema mirrors
what graphify emits: path-derived node ids, ``()``-suffixed callable labels,
``rationale_for`` edges pointing from prose to code, and ``EXTRACTED`` /
``INFERRED`` confidence.
"""

import json
import os

import pytest

import graph
from graph import queries
from graph.model import (
    AmbiguousNodeError,
    GraphNotFoundError,
    NodeNotFoundError,
    resolve_graph_path,
)


def _node(nid, label, community, file_type="code", source_file="", callable_=None, **extra):
    node = {
        "id": nid,
        "label": label,
        "norm_label": label.lower(),
        "community": community,
        "community_name": f"comm-{community}",
        "file_type": file_type,
        "source_file": source_file,
        "source_location": "L1",
    }
    if callable_ is not None:
        node["_callable"] = callable_
    node.update(extra)
    return node


def _edge(source, target, relation, confidence="EXTRACTED", source_file="", location="L1"):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "confidence_score": 1.0 if confidence == "EXTRACTED" else 0.5,
        "source_file": source_file,
        "source_location": location,
        "weight": 1.0,
    }


def _payload():
    return {
        "directed": True,
        "multigraph": False,
        "built_at_commit": "deadbeef",
        "nodes": [
            _node("a_mod_foo", "foo()", 1, source_file="a/mod.py", callable_=True),
            _node("a_mod_bar", "bar()", 1, source_file="a/mod.py", callable_=True),
            _node("b_mod_baz", "baz()", 2, source_file="b/mod.py", callable_=True),
            _node("rat_foo", "foo explains itself", 1, file_type="rationale"),
            _node("doc_notes", "Notes", 2, file_type="document", node_kind="heading"),
            _node("ext_log", "log()", 1, file_type="code", callable_=True, external=True),
        ],
        "links": [
            _edge("a_mod_foo", "a_mod_bar", "calls", source_file="a/mod.py", location="L20"),
            _edge("b_mod_baz", "a_mod_foo", "calls", "INFERRED", source_file="b/mod.py"),
            _edge("rat_foo", "a_mod_foo", "rationale_for"),
            _edge("a_mod_foo", "a_mod_bar", "method"),
            _edge("doc_notes", "b_mod_baz", "references"),
            _edge("doc_notes", "a_mod_foo", "references"),
            _edge("ghost_file", "b_mod_baz", "calls", source_file="c/ghost.py"),
        ],
        "hyperedges": [],
    }


@pytest.fixture
def project(tmp_path):
    """A project dir containing graphify-out/graph.json."""
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(_payload()), encoding="utf-8")
    return tmp_path


@pytest.fixture
def g(project):
    return graph.load(project, use_cache=False)


# ── path resolution ──────────────────────────────────────────────────


def test_load_from_project_dir(project):
    assert graph.load(project, use_cache=False).meta()["nodes"] == 6


def test_load_from_out_dir(project):
    assert graph.load(project / "graphify-out", use_cache=False).meta()["nodes"] == 6


def test_load_from_json_file(project):
    assert graph.load(project / "graphify-out" / "graph.json", use_cache=False).meta()["nodes"] == 6


def test_resolve_walks_up_from_cwd(project, monkeypatch):
    nested = project / "a" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_graph_path().name == "graph.json"


def test_resolve_honours_project_env(project, monkeypatch):
    monkeypatch.chdir("/")
    monkeypatch.setenv("GRAPHIFY_PROJECT_DIR", str(project))
    assert resolve_graph_path().parent.name == "graphify-out"


def test_missing_graph_raises(tmp_path):
    with pytest.raises(GraphNotFoundError):
        graph.load(tmp_path, use_cache=False)


def test_cache_returns_same_instance(project):
    first = graph.load(project)
    second = graph.load(project)
    assert first is second


# ── node resolution ──────────────────────────────────────────────────


def test_resolve_by_id(g):
    assert g.node("a_mod_foo").name == "foo()"


def test_resolve_tolerates_parens_and_case(g):
    assert g.node("foo").key == "a_mod_foo"
    assert g.node("FOO").key == "a_mod_foo"
    assert g.node("foo()").key == "a_mod_foo"


def test_unknown_node_raises(g):
    with pytest.raises(NodeNotFoundError):
        g.node("nope")


def test_ambiguous_node_raises(g, project):
    # Two nodes sharing a label force the ambiguity path.
    payload = _payload()
    payload["nodes"].append(_node("z_mod_foo", "foo()", 3, source_file="z/mod.py"))
    (project / "graphify-out" / "graph.json").write_text(json.dumps(payload), encoding="utf-8")
    fresh = graph.load(project, use_cache=False)
    with pytest.raises(AmbiguousNodeError) as exc:
        fresh.node("foo")
    assert len(exc.value.candidates) == 2


# ── accessors ────────────────────────────────────────────────────────


def test_meta_counts(g):
    meta = g.meta()
    assert meta["nodes"] == 6
    assert meta["edges"] == 7
    assert meta["communities"] == 2
    assert meta["confidence"] == {"EXTRACTED": 6, "INFERRED": 1}
    assert meta["file_types"]["code"] == 4


def test_edges_filter_by_relation_and_confidence(g):
    assert len(g.edges_where(relation="calls")) == 3
    assert len(g.edges_where(relation="calls", confidence="INFERRED")) == 1
    assert len(g.edges_where(confidence="INFERRED")) == 1


def test_edge_location_is_relation_site(g):
    edge = g.edges_where(source="a_mod_foo", target="a_mod_bar", relation="calls")[0]
    assert edge.where() == "a/mod.py:L20"


def test_degree_and_god_nodes(g):
    assert g.degree("a_mod_foo") == 5  # 2 out (calls, method) + 3 in
    top, deg = g.god_nodes(1)[0]
    assert top.key == "a_mod_foo"
    assert deg == 5


def test_dangling_edges_detected(g):
    dangling = g.dangling_edges()
    assert len(dangling) == 1
    assert dangling[0].get("source") == "ghost_file"


def test_nodes_in_file_exact_and_suffix(g):
    assert len(g.nodes_in_file("a/mod.py")) == 2
    # A bare "mod.py" suffix matches two files, so it must fail loudly rather
    # than silently returning the first match.
    with pytest.raises(AmbiguousNodeError):
        g.nodes_in_file("mod.py")
    assert g.nodes_in_file("nope.py") == []


def test_search_ranks_by_connectivity(g):
    hits = g.search("foo")
    assert hits[0].key == "a_mod_foo"


def test_neighbors_skips_unresolvable(g):
    nbrs = g.neighbors("b_mod_baz")
    # ghost_file is not a node, so only doc_notes (in) and foo (out) survive
    assert {n.key for n, _, _ in nbrs} == {"doc_notes", "a_mod_foo"}


# ── derived queries ──────────────────────────────────────────────────


def test_rationale_index_maps_prose_to_code(g):
    result = queries.rationale(g, "foo")
    assert result["count"] == 1
    assert result["rationale"][0]["text"] == "foo explains itself"


def test_rationale_coverage(g):
    result = queries.rationale(g)
    # code nodes: foo, bar, baz, log → 1 documented
    assert result["code_nodes"] == 4
    assert result["documented"] == 1
    assert result["coverage_pct"] == 25.0


def test_undocumented_groups_by_community(g):
    result = queries.undocumented(g)
    assert result["candidates"] == 3  # foo, bar, baz — external `log()` excluded
    assert result["undocumented"] == 2
    assert result["communities_affected"] == 2


def test_undocumented_prefix_scope(g):
    result = queries.undocumented(g, prefix="a/")
    assert result["candidates"] == 2  # foo, bar
    assert result["undocumented"] == 1  # foo is documented


def test_gaps_finds_undocumented_communities(g):
    result = queries.gaps(g, min_code_nodes=1)
    assert result["count"] == 1
    assert result["communities"][0]["community"] == 2


def test_inferred_calls_ranked_by_out_degree(g):
    result = queries.inferred_calls(g)
    assert result["total_inferred"] == 1
    assert result["items"][0]["caller"] == "baz()"
    assert result["items"][0]["callee"] == "foo()"


def test_callers_of_node(g):
    result = queries.callers(g, "foo")
    assert result["count"] == 1
    assert result["callers"][0]["caller"] == "baz()"
    assert result["callers"][0]["confidence"] == "INFERRED"


def test_impact_reports_risk_and_core_abstractions(g):
    result = queries.impact(g, ["a/mod.py"])
    assert result["files_matched"] == 1
    assert result["communities_touched"] == 1
    assert result["core_abstractions_hit"][0]["name"] == "foo()"
    assert result["risk"].startswith("high")


def test_impact_flags_missing_files(g):
    result = queries.impact(g, ["a/mod.py", "does/not/exist.py"])
    assert result["files_missing"] == ["does/not/exist.py"]


def test_relation_matrix_excludes_intra_community(g):
    result = queries.relation_matrix(g)
    # cross-community: baz(2) -> foo(1) calls, doc_notes(2) -> foo(1) references
    assert result["cross_community_edges"] == 2
    assert {(row["from"], row["to"]) for row in result["top"]} == {(2, 1)}


# ── completeness (the whole point) ───────────────────────────────────


def test_results_are_never_truncated(g):
    """Counts must agree with the returned list — no budget-capped dumps."""
    edges = g.edges_where(relation="calls")
    assert len(edges) == 3
    result = queries.inferred_calls(g, limit=1000)
    assert result["shown"] == len(result["items"]) == result["total_inferred"]


def test_nodes_and_edges_are_json_serialisable(g):
    payload = {
        "nodes": [dict(n) for n in g.nodes],
        "edges": [dict(e) for e in g.links],
        "meta": g.meta(),
    }
    assert json.loads(json.dumps(payload))["meta"]["nodes"] == 6


# ── CLI smoke ────────────────────────────────────────────────────────


def test_cli_summary(project, capsys):
    from graph.__main__ import main

    assert main(["--graph", str(project), "summary"]) == 0
    out = capsys.readouterr().out
    assert "nodes: 6" in out
    assert "commit: deadbeef" in out


def test_cli_unknown_graph_exits_nonzero(tmp_path, capsys):
    from graph.__main__ import main

    assert main(["--graph", str(tmp_path), "summary"]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_impact(project, capsys):
    from graph.__main__ import main

    assert main(["--graph", str(project), "impact", "a/mod.py"]) == 0
    assert "risk high" in capsys.readouterr().out


def test_env_out_override(project, tmp_path, monkeypatch):
    """GRAPHIFY_OUT relocates the output directory, as graphify's own tooling expects."""
    alt = tmp_path / "alt-out"
    alt.mkdir()
    (alt / "graph.json").write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setenv("GRAPHIFY_OUT", "alt-out")
    monkeypatch.chdir(tmp_path)
    assert resolve_graph_path() == (alt / "graph.json").resolve()
    monkeypatch.delenv("GRAPHIFY_OUT")


def test_absolute_graphify_out(project, tmp_path, monkeypatch):
    alt = tmp_path / "abs-out"
    alt.mkdir()
    (alt / "graph.json").write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setenv("GRAPHIFY_OUT", str(alt))
    assert resolve_graph_path(project) == (alt / "graph.json").resolve()
    monkeypatch.delenv("GRAPHIFY_OUT")


def test_module_import_surface():
    for name in ("load", "queries", "Graph", "Node", "Edge", "GraphNotFoundError"):
        assert hasattr(graph, name)
    assert os.path.basename(graph.__file__) == "__init__.py"
