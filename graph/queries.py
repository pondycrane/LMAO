"""Derived analyses over a graph — the questions worth asking as *data*.

Each function returns a plain dict (JSON-serialisable) with counts alongside the
items, so a caller can tell "nothing matched" apart from "I truncated". Nothing
here prints; formatting is the caller's job (:mod:`graph.__main__` does it for
the CLI).

Why these exist: graphify's MCP tools answer in prose, one line per edge, capped
by ``token_budget``. A question like "which subsystems have no rationale
coverage?" needs a set difference across 10k nodes and 16k edges — cheap in code,
expensive as five text dumps the model must mentally union.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from graph.model import CALLS, RATIONALE, Graph, Node


def _key_set(nodes: Iterable[Node]) -> set[str]:
    return {n.key for n in nodes}


def rationale_index(g: Graph) -> dict[str, list[Node]]:
    """``{node_id: [rationale nodes]}`` — every ``rationale_for`` target indexed.

    ``rationale_for`` edges point *from* a rationale node (its ``label`` is the
    prose description) *to* the code node it explains. So a node's rationale is
    its **in**-edges of that relation.
    """
    index: dict[str, list[Node]] = defaultdict(list)
    for edge in g.edges_where(relation=RATIONALE):
        target = str(edge.get("target"))
        src = g.nodes_by_id.get(str(edge.get("source")))
        if src is not None:
            index[target].append(src)
    return dict(index)


def rationale(g: Graph, key: str | None = None) -> dict:
    """Rationale prose attached to one node, or coverage statistics for all."""
    index = rationale_index(g)
    if key is not None:
        node = g.node(key)
        notes = index.get(node.key, [])
        return {
            "node": node.name,
            "id": node.key,
            "where": node.where(),
            "count": len(notes),
            "rationale": [
                {"text": n.name, "id": n.key, "where": n.where()} for n in notes
            ],
        }

    code = g.nodes_where(file_type="code")
    documented = _key_set(n for n in code if n.key in index)
    total_code = len(code)
    return {
        "code_nodes": total_code,
        "documented": len(documented),
        "undocumented": total_code - len(documented),
        "coverage_pct": round(100.0 * len(documented) / total_code, 1) if total_code else 0.0,
        "rationale_edges": sum(len(v) for v in index.values()),
    }


def undocumented(
    g: Graph,
    *,
    prefix: str | None = None,
    limit: int = 25,
    callable_only: bool = True,
    include_external: bool = False,
) -> dict:
    """Code nodes with no ``rationale_for`` edge, grouped by community.

    External symbols (stdlib imports, vendored packages) are excluded by
    default: they cannot be documented in this repository, so counting them as
    "undocumented" inflates the number without pointing at anything actionable.
    """
    index = rationale_index(g)
    where = None
    if prefix:
        where = lambda n: str(n.get("source_file") or "").startswith(prefix)  # noqa: E731

    candidates = g.nodes_where(
        where,
        file_type="code",
        is_callable=callable_only or None,
        external=False if not include_external else None,
    )
    missing = [n for n in candidates if n.key not in index]

    by_community: dict[int, list[Node]] = defaultdict(list)
    for node in missing:
        cid = node.get("community")
        if cid is not None:
            by_community[int(cid)].append(node)

    ranked = sorted(by_community.items(), key=lambda kv: len(kv[1]), reverse=True)
    return {
        "scope": prefix or "<all>",
        "callable_only": callable_only,
        "candidates": len(candidates),
        "undocumented": len(missing),
        "communities_affected": len(ranked),
        "by_community": [
            {
                "community": cid,
                "name": g.community_name(cid),
                "undocumented": len(members),
                "total": len(g.community(cid)),
                "sample": [{"name": n.name, "where": n.where()} for n in members[:3]],
            }
            for cid, members in ranked[:limit]
        ],
    }


def gaps(g: Graph, *, min_code_nodes: int = 5, limit: int = 20) -> dict:
    """Communities with **zero** rationale coverage but real code.

    These are the subsystems nobody explained — the ones a new contributor (or an
    agent) has to reverse-engineer from call sites alone.
    """
    index = rationale_index(g)
    out: list[dict[str, Any]] = []
    for cid, members in g.by_community.items():
        code = [n for n in members if n.get("file_type") == "code"]
        if len(code) < min_code_nodes:
            continue
        documented = [n for n in code if n.key in index]
        if documented:
            continue
        files = sorted({str(n.get("source_file")) for n in code if n.get("source_file")})
        out.append(
            {
                "community": cid,
                "name": g.community_name(cid),
                "code_nodes": len(code),
                "files": files[:5],
                "sample": [n.name for n in code[:5]],
            }
        )
    out.sort(key=lambda d: d["code_nodes"], reverse=True)
    return {"min_code_nodes": min_code_nodes, "count": len(out), "communities": out[:limit]}


def inferred_calls(
    g: Graph, *, min_out_degree: int = 0, limit: int = 50, include_non_calls: bool = False
) -> dict:
    """``INFERRED`` edges — the extractor's guesses, ranked by blast radius.

    ``EXTRACTED`` edges come from a real AST match; ``INFERRED`` ones are
    plausible-but-unverified. Filtering to callers with many outgoing edges turns
    890 guesses into a bounded review queue.
    """
    relations = {CALLS} if not include_non_calls else None
    edges = g.edges_where(confidence="INFERRED", relation=CALLS if relations else None)
    out_deg = g.degree_map(direction="out")

    items: list[dict[str, Any]] = []
    for edge in edges:
        src = str(edge.get("source"))
        deg = out_deg.get(src, 0)
        if deg < min_out_degree:
            continue
        src_node = g.nodes_by_id.get(src)
        dst_node = g.nodes_by_id.get(str(edge.get("target")))
        items.append(
            {
                "caller": src_node.name if src_node else src,
                "callee": dst_node.name if dst_node else edge.get("target"),
                "relation": edge.get("relation"),
                "where": edge.where(),
                "caller_out_degree": deg,
            }
        )
    items.sort(key=lambda d: d["caller_out_degree"], reverse=True)

    by_relation = Counter(e.get("relation") for e in edges)
    return {
        "total_inferred": len(edges),
        "total_edges": len(g.links),
        "by_relation": dict(by_relation.most_common()),
        "min_out_degree": min_out_degree,
        "shown": len(items[:limit]),
        "items": items[:limit],
    }


def callers(g: Graph, key: str, *, confidence: str | None = None, relation: str = CALLS) -> dict:
    """Who calls / references ``key`` — the join that answers "is this reached?"."""
    node = g.node(key)
    edges = g.in_edges_of(node.key, relation=relation)
    if confidence:
        edges = [e for e in edges if e.get("confidence") == confidence]
    items: list[dict[str, Any]] = []
    for edge in edges:
        src = g.nodes_by_id.get(str(edge.get("source")))
        items.append(
            {
                "caller": src.name if src else edge.get("source"),
                "caller_community": src.get("community") if src else None,
                "where": edge.where(),
                "confidence": edge.get("confidence"),
            }
        )
    items.sort(key=lambda d: (d["confidence"] or "", d["caller"] or ""))
    return {
        "node": node.name,
        "id": node.key,
        "where": node.where(),
        "relation": relation,
        "count": len(items),
        "callers": items,
    }


def callees(g: Graph, key: str, *, relation: str = CALLS) -> dict:
    """What ``key`` calls or references."""
    node = g.node(key)
    edges = g.out_edges_of(node.key, relation=relation)
    items = []
    for edge in edges:
        dst = g.nodes_by_id.get(str(edge.get("target")))
        items.append(
            {
                "callee": dst.name if dst else edge.get("target"),
                "where": edge.where(),
                "confidence": edge.get("confidence"),
            }
        )
    return {"node": node.name, "id": node.key, "relation": relation, "count": len(items),
            "callees": items}


def impact(g: Graph, paths: Iterable[str], *, god_limit: int = 10) -> dict:
    """What a set of changed files actually touches — files -> nodes -> communities.

    Answers "this PR touches 7 files" with the follow-up that matters: *which*
    communities, and whether any of the graph's core abstractions are involved.
    """
    god = {n.key: deg for n, deg in g.god_nodes(god_limit)}
    god_by_id = {n.key: n for n, _ in g.god_nodes(god_limit)}

    paths = list(paths)
    communities: dict[int, list[str]] = defaultdict(list)
    touched_nodes: list[Node] = []
    missing: list[str] = []

    for path in paths:
        nodes = g.nodes_in_file(path)
        if not nodes:
            missing.append(path)
            continue
        touched_nodes.extend(nodes)
        for node in nodes:
            cid = node.get("community")
            if cid is not None:
                communities[int(cid)].append(node.name)

    core = [
        {"name": god_by_id[nid].name, "degree": deg, "where": god_by_id[nid].where()}
        for nid, deg in god.items()
        if any(n.key == nid for n in touched_nodes)
    ]
    ranked = sorted(communities.items(), key=lambda kv: len(kv[1]), reverse=True)

    return {
        "files_requested": len(paths),
        "files_matched": len({str(n.get("source_file")) for n in touched_nodes}),
        "files_missing": missing,
        "nodes_touched": len(touched_nodes),
        "communities_touched": len(ranked),
        # Provenance travels with the answer: the graph is rebuilt by a hook and
        # a cron watcher, so "which commit was this measured against" is part of
        # the result, not something the caller has to remember to ask.
        "graph_commit": g.built_at_commit,
        "communities": [
            {
                "community": cid,
                "name": g.community_name(cid),
                "nodes": len(names),
                "total_in_community": len(g.community(cid)),
                "sample": names[:4],
            }
            for cid, names in ranked
        ],
        "core_abstractions_hit": core,
        "risk": _risk_label(ranked, core, len(g.by_community)),
    }


def _risk_label(ranked: list[tuple[int, list[str]]], core: list[dict], n_communities: int) -> str:
    """A coarse, explainable risk signal — not a model judgement."""
    if not ranked:
        return "none (no files matched)"
    if core:
        return "high (touches a core abstraction)"
    if len(ranked) >= max(3, n_communities // 20):
        return "medium (spans many communities)"
    return "low (localised)"


def relation_matrix(g: Graph, *, limit: int = 10) -> dict:
    """Which relations connect which communities — the cross-cutting map.

    Surprising pairs here are where an architectural boundary is being crossed
    without anyone writing it down.
    """
    node_to_comm = {n.key: n.get("community") for n in g.nodes_by_id.values()}
    pairs: Counter[tuple[int, int, str]] = Counter()
    for edge in g.links:
        src = node_to_comm.get(str(edge.get("source")))
        dst = node_to_comm.get(str(edge.get("target")))
        if src is None or dst is None or src == dst:
            continue
        pairs[(int(src), int(dst), str(edge.get("relation")))] += 1

    items = [
        {
            "from": s,
            "from_name": g.community_name(s),
            "to": d,
            "to_name": g.community_name(d),
            "relation": rel,
            "count": count,
        }
        for (s, d, rel), count in pairs.most_common(limit)
    ]
    return {"cross_community_edges": sum(pairs.values()), "top": items}
