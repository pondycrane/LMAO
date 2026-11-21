"""Graph loading, indexing and read accessors.

The on-disk format is ``graphify-out/graph.json``::

    {
      "directed": bool, "multigraph": bool, "built_at_commit": str,
      "nodes": [ {id, label, norm_label, community, community_name,
                  file_type, source_file, source_location, _callable, ...} ],
      "links": [ {source, target, relation, confidence, confidence_score,
                  source_file, source_location, context, weight} ],
      "hyperedges": [ ... ]
    }

Node ids are path-derived (``cardputer_client_chart_draw``), labels are the
symbol text (``draw()``), and ``norm_label`` is the lowercased label used for
resolution. Edges carry ``confidence`` of ``EXTRACTED`` or ``INFERRED``; the
latter are guesses and are worth auditing.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

GRAPH_DIRNAME = "graphify-out"
GRAPH_FILENAME = "graph.json"

# Relation names used by the derived queries in ``graph.queries``.
RATIONALE = "rationale_for"
CALLS = "calls"

_ENV_OUT = "GRAPHIFY_OUT"
_ENV_PROJECT = "GRAPHIFY_PROJECT_DIR"


class GraphError(RuntimeError):
    """Base class for graph-loading problems."""


class GraphNotFoundError(GraphError):
    """No ``graph.json`` at the requested or discovered location."""


class NodeNotFoundError(LookupError):
    """No node matched the requested key."""


class AmbiguousNodeError(LookupError):
    """The key matched more than one node; candidates are in ``candidates``."""

    def __init__(self, key: str, candidates: list[Node]):
        self.key = key
        self.candidates = candidates
        names = ", ".join(f"{c.get('label')!r} ({c.get('id')})" for c in candidates[:8])
        super().__init__(f"{key!r} is ambiguous — {len(candidates)} candidates: {names}")


class Node(dict):
    """A node as a plain dict, with attribute access for convenience.

    ``n["source_file"]`` and ``n.source_file`` are equivalent. Instances stay
    JSON-serialisable, which is the point: query results are meant to be printed
    or summarised, not handed to another library.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def key(self) -> str:
        """The stable node id."""
        return str(self.get("id", ""))

    @property
    def name(self) -> str:
        """Human-facing label, falling back to the id."""
        return str(self.get("label") or self.get("id") or "")

    def where(self) -> str:
        """``file:line`` of the definition, or an empty string."""
        src = self.get("source_file") or ""
        loc = self.get("source_location") or ""
        return f"{src}:{loc}" if loc else str(src)

    def __repr__(self) -> str:
        return f"<Node {self.name!r} {self.get('file_type', '?')} {self.where()}>"


class Edge(dict):
    """A link as a plain dict, with attribute access."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def where(self) -> str:
        """``file:line`` of the *relation site* (the call/import line)."""
        src = self.get("source_file") or ""
        loc = self.get("source_location") or ""
        return f"{src}:{loc}" if loc else str(src)

    def __repr__(self) -> str:
        return (
            f"<Edge {self.get('relation', '?')} {self.get('source')} -> "
            f"{self.get('target')} [{self.get('confidence', '?')}]>"
        )


def _out_dirname() -> str:
    """``GRAPHIFY_OUT`` override, or the default directory name."""
    return os.environ.get(_ENV_OUT) or GRAPH_DIRNAME


def _candidates(base: Path) -> list[Path]:
    """Possible ``graph.json`` locations under ``base``, most specific first."""
    out = _out_dirname()
    out_path = Path(out).expanduser()
    if out_path.is_absolute():
        return [out_path / GRAPH_FILENAME]
    return [base / out / GRAPH_FILENAME, base / GRAPH_FILENAME]


def resolve_graph_path(path: str | Path | None = None) -> Path:
    """Resolve ``path`` to an existing ``graph.json``.

    Accepts a file, a ``graphify-out`` directory, or a project directory. With no
    argument, honours ``GRAPHIFY_PROJECT_DIR`` (which graphify's own MCP server
    reads from the environment but ignores) and then walks up from the CWD
    looking for ``<dir>/graphify-out/graph.json``.
    """
    if path is not None:
        p = Path(path).expanduser()
        if p.is_file():
            return p.resolve()
        for cand in _candidates(p):
            if cand.is_file():
                return cand.resolve()
        raise GraphNotFoundError(f"no {GRAPH_FILENAME} found under {p}")

    env_project = os.environ.get(_ENV_PROJECT)
    if env_project:
        p = Path(env_project).expanduser()
        for cand in _candidates(p):
            if cand.is_file():
                return cand.resolve()

    here = Path.cwd().resolve()
    for base in (here, *here.parents):
        for cand in _candidates(base):
            if cand.is_file():
                return cand.resolve()

    raise GraphNotFoundError(
        f"no {GRAPH_FILENAME} found from {here} upwards; pass a project path or "
        f"set {_ENV_PROJECT}"
    )


_CACHE: dict[str, Graph] = {}


def load(path: str | Path | None = None, *, use_cache: bool = True) -> Graph:
    """Load and index a graph. Returns a cached instance when the file is unchanged."""
    resolved = resolve_graph_path(path)
    stat = resolved.stat()
    stamp = f"{resolved}:{stat.st_mtime_ns}:{stat.st_size}"
    if use_cache:
        cached = _CACHE.get(stamp)
        if cached is not None:
            return cached
    with resolved.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    g = Graph(data, path=resolved)
    if use_cache:
        # Keep exactly one entry per path so a rebuilt graph replaces the stale one.
        for key in [k for k in _CACHE if k.startswith(f"{resolved}:")]:
            del _CACHE[key]
        _CACHE[stamp] = g
    return g


class Graph:
    """An indexed, read-only view over a ``graph.json`` payload."""

    def __init__(self, data: dict, path: Path | str | None = None):
        self.path = str(path) if path is not None else None
        self.raw = data
        self.built_at_commit = data.get("built_at_commit")
        self.directed = bool(data.get("directed", True))
        self.multigraph = bool(data.get("multigraph", False))

        self.nodes_by_id: dict[str, Node] = {}
        self.by_norm: dict[str, list[Node]] = defaultdict(list)
        self.by_label: dict[str, list[Node]] = defaultdict(list)
        self.by_file: dict[str, list[Node]] = defaultdict(list)
        self.by_community: dict[int, list[Node]] = defaultdict(list)

        self.out_edges: dict[str, list[Edge]] = defaultdict(list)
        self.in_edges: dict[str, list[Edge]] = defaultdict(list)
        self.edges_by_relation: dict[str, list[Edge]] = defaultdict(list)
        self.edges_by_confidence: dict[str, list[Edge]] = defaultdict(list)

        self._build_indexes()

    # ── construction ──────────────────────────────────────────────────

    def _build_indexes(self) -> None:
        for raw in self.raw.get("nodes", []):
            node = Node(raw)
            nid = node.key
            self.nodes_by_id[nid] = node
            norm = str(node.get("norm_label") or node.name.lower())
            self.by_norm[norm].append(node)
            self.by_label[node.name].append(node)
            # graphify labels callables with a trailing "()" — index the bare form
            # too so both `foo` and `foo()` resolve to the same node.
            if norm.endswith("()"):
                self.by_norm[norm[:-2]].append(node)
            if node.name.endswith("()"):
                self.by_label[node.name[:-2]].append(node)
            src = node.get("source_file")
            if src:
                self.by_file[str(src)].append(node)
            cid = node.get("community")
            if cid is not None:
                self.by_community[int(cid)].append(node)

        for raw in self.raw.get("links", []):
            edge = Edge(raw)
            self.out_edges[str(edge.get("source"))].append(edge)
            self.in_edges[str(edge.get("target"))].append(edge)
            self.edges_by_relation[str(edge.get("relation"))].append(edge)
            self.edges_by_confidence[str(edge.get("confidence"))].append(edge)

    # ── introspection ─────────────────────────────────────────────────

    @property
    def nodes(self) -> list[Node]:
        return list(self.nodes_by_id.values())

    @property
    def links(self) -> list[Edge]:
        return [e for bucket in self.edges_by_relation.values() for e in bucket]

    def hyperedges(self) -> list[dict]:
        return list(self.raw.get("hyperedges", []))

    def meta(self) -> dict:
        """Counts and breakdowns — a cheap orientation call (~120 tokens)."""
        relations = Counter(e.get("relation") for e in self.links)
        confidence = Counter(e.get("confidence") for e in self.links)
        file_types = Counter(n.get("file_type") for n in self.nodes)
        return {
            "path": self.path,
            "built_at_commit": self.built_at_commit,
            "directed": self.directed,
            "nodes": len(self.nodes_by_id),
            "edges": len(self.links),
            "hyperedges": len(self.hyperedges()),
            "communities": len(self.by_community),
            "relations": dict(relations.most_common()),
            "confidence": dict(confidence.most_common()),
            "file_types": dict(file_types.most_common()),
        }

    def __repr__(self) -> str:
        return (
            f"<Graph {len(self.nodes_by_id)} nodes, {len(self.links)} edges, "
            f"{len(self.by_community)} communities, {self.path}>"
        )

    # ── node lookup ───────────────────────────────────────────────────

    def resolve(self, key: str) -> list[Node]:
        """All nodes matching ``key`` — id, then norm_label, then label.

        Tolerates the ``()`` suffix graphify puts on callable labels, so both
        ``Transport.request`` and ``Transport.request()`` resolve.
        """
        raw = str(key)
        variants = [raw, raw.lower()]
        stripped = raw[:-2] if raw.endswith("()") else raw
        variants += [stripped, stripped.lower()]
        for variant in variants:
            if variant in self.nodes_by_id:
                return [self.nodes_by_id[variant]]
        for table in (self.by_norm, self.by_label):
            for variant in variants:
                hits = table.get(variant)
                if hits:
                    return list(hits)
        return []

    def node(self, key: str) -> Node:
        """Resolve a single node; raises on no match or on ambiguity."""
        hits = self.resolve(key)
        if not hits:
            raise NodeNotFoundError(f"no node matching {key!r}")
        if len(hits) > 1:
            # Prefer an exact label match over a case-insensitive one.
            exact = [h for h in hits if h.name == key]
            if len(exact) == 1:
                return exact[0]
            raise AmbiguousNodeError(key, hits)
        return hits[0]

    def search(self, text: str, limit: int = 20) -> list[Node]:
        """Substring search over label and source_file, most-connected first."""
        needle = text.lower()
        hits = [
            n
            for n in self.nodes_by_id.values()
            if needle in n.name.lower() or needle in str(n.get("source_file", "")).lower()
        ]
        hits.sort(
            key=lambda n: len(self.out_edges[n.key]) + len(self.in_edges[n.key]), reverse=True
        )
        return hits[:limit]

    # ── node queries ──────────────────────────────────────────────────

    def nodes_where(
        self,
        where: Callable[[Node], bool] | None = None,
        *,
        file_type: str | None = None,
        community: int | None = None,
        is_callable: bool | None = None,
        external: bool | None = None,
        source_file: str | None = None,
    ) -> list[Node]:
        """Filter nodes by exact field values and/or a predicate."""
        out = []
        for node in self.nodes_by_id.values():
            if file_type is not None and node.get("file_type") != file_type:
                continue
            if community is not None and node.get("community") != community:
                continue
            if is_callable is not None and bool(node.get("_callable")) != is_callable:
                continue
            if external is not None and bool(node.get("external")) != external:
                continue
            if source_file is not None and node.get("source_file") != source_file:
                continue
            if where is not None and not where(node):
                continue
            out.append(node)
        return out

    def files(self, prefix: str | None = None) -> list[str]:
        """Source files present in the graph, optionally filtered by prefix."""
        keys = sorted(self.by_file)
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys

    def nodes_in_file(self, path: str) -> list[Node]:
        """Nodes defined in ``path`` — exact match, else longest suffix match."""
        if path in self.by_file:
            return list(self.by_file[path])
        matches = [k for k in self.by_file if k.endswith(path.lstrip("./"))]
        if len(matches) == 1:
            return list(self.by_file[matches[0]])
        if len(matches) > 1:
            raise AmbiguousNodeError(path, [n for k in matches for n in self.by_file[k]])
        return []

    def communities(self) -> dict[int, list[Node]]:
        """Community id -> member nodes."""
        return {cid: list(members) for cid, members in self.by_community.items()}

    def community(self, cid: int) -> list[Node]:
        return list(self.by_community.get(int(cid), []))

    def community_name(self, cid: int) -> str:
        members = self.by_community.get(int(cid)) or []
        return str(members[0].get("community_name")) if members else ""

    # ── edge queries ──────────────────────────────────────────────────

    def edges_where(
        self,
        where: Callable[[Edge], bool] | None = None,
        *,
        relation: str | None = None,
        confidence: str | None = None,
        source: str | None = None,
        target: str | None = None,
        context: str | None = None,
    ) -> list[Edge]:
        """Filter edges by exact field values and/or a predicate."""
        if relation is not None:
            pool: Iterable[Edge] = self.edges_by_relation.get(relation, [])
        elif confidence is not None:
            pool = self.edges_by_confidence.get(confidence, [])
        elif source is not None:
            pool = self.out_edges.get(source, [])
        elif target is not None:
            pool = self.in_edges.get(target, [])
        else:
            pool = self.links

        out = []
        for edge in pool:
            if relation is not None and edge.get("relation") != relation:
                continue
            if confidence is not None and edge.get("confidence") != confidence:
                continue
            if source is not None and edge.get("source") != source:
                continue
            if target is not None and edge.get("target") != target:
                continue
            if context is not None and edge.get("context") != context:
                continue
            if where is not None and not where(edge):
                continue
            out.append(edge)
        return out

    def edges(
        self,
        *,
        relation: str | None = None,
        confidence: str | None = None,
        source: str | None = None,
        target: str | None = None,
        context: str | None = None,
    ) -> list[Edge]:
        """Edges matching the given field filters — the common case."""
        return self.edges_where(
            relation=relation,
            confidence=confidence,
            source=source,
            target=target,
            context=context,
        )

    def out_edges_of(self, key: str, relation: str | None = None) -> list[Edge]:
        nid = self.node(key).key
        return self.edges_where(source=nid, relation=relation)

    def in_edges_of(self, key: str, relation: str | None = None) -> list[Edge]:
        nid = self.node(key).key
        return self.edges_where(target=nid, relation=relation)

    def neighbors(
        self, key: str, relation: str | None = None, direction: str = "both"
    ) -> list[tuple[Node, Edge, str]]:
        """``(neighbor_node, edge, "out"|"in")`` triples. Unresolvable targets skipped."""
        nid = self.node(key).key
        out: list[tuple[Node, Edge, str]] = []
        if direction in ("both", "out"):
            for edge in self.edges_where(source=nid, relation=relation):
                nb = self.nodes_by_id.get(str(edge.get("target")))
                if nb is not None:
                    out.append((nb, edge, "out"))
        if direction in ("both", "in"):
            for edge in self.edges_where(target=nid, relation=relation):
                nb = self.nodes_by_id.get(str(edge.get("source")))
                if nb is not None:
                    out.append((nb, edge, "in"))
        return out

    # ── degree ────────────────────────────────────────────────────────

    def degree_map(self, direction: str = "both") -> dict[str, int]:
        """``{node_id: degree}`` for the whole graph."""
        return self._degree_map(direction)

    def degree(self, key: str | None = None, direction: str = "both") -> int | dict[str, int]:
        """Degree of one node (``key`` given), or a ``{id: degree}`` map otherwise."""
        if key is not None:
            return self._degree_map(direction).get(self.node(key).key, 0)
        return self._degree_map(direction)

    def _degree_map(self, direction: str = "both") -> dict[str, int]:
        table: dict[str, int] = {}
        for nid in self.nodes_by_id:
            if direction == "out":
                table[nid] = len(self.out_edges.get(nid, []))
            elif direction == "in":
                table[nid] = len(self.in_edges.get(nid, []))
            else:
                table[nid] = len(self.out_edges.get(nid, [])) + len(self.in_edges.get(nid, []))
        return table

    def god_nodes(self, limit: int = 10, direction: str = "both") -> list[tuple[Node, int]]:
        """The most-connected nodes — the core abstractions of the corpus."""
        table = self._degree_map(direction)
        ranked = sorted(table.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [(self.nodes_by_id[nid], deg) for nid, deg in ranked if nid in self.nodes_by_id]

    def dangling_edges(self) -> list[Edge]:
        """Edges whose source or target is not a known node (extraction artifacts)."""
        return [
            e
            for e in self.links
            if str(e.get("source")) not in self.nodes_by_id
            or str(e.get("target")) not in self.nodes_by_id
        ]

    # ── rendering ─────────────────────────────────────────────────────

    def describe(self, node: Node, *, max_neighbors: int = 8) -> str:
        """One-paragraph text form of a node — for CLI output, not for aggregation."""
        lines = [
            f"{node.name} [{node.get('file_type', '?')}] {node.where()}",
            f"  id={node.key} community={node.get('community')} "
            f"({node.get('community_name', '')}) degree={self.degree(node.key)}",
        ]
        for label, direction in (("out", "out"), ("in", "in")):
            edges = (
                self.out_edges_of(node.key) if direction == "out" else self.in_edges_of(node.key)
            )
            if not edges:
                continue
            rels = Counter(e.get("relation") for e in edges)
            summary = ", ".join(f"{r}×{c}" for r, c in rels.most_common(4))
            lines.append(f"  {label}: {len(edges)} edges ({summary})")
            for edge in edges[:max_neighbors]:
                other_id = str(edge.get("target") if direction == "out" else edge.get("source"))
                other = self.nodes_by_id.get(other_id)
                other_name = other.name if other is not None else other_id
                arrow = "-->" if direction == "out" else "<--"
                lines.append(
                    f"    {arrow} {other_name} [{edge.get('relation')}] "
                    f"[{edge.get('confidence')}] at={edge.where()}"
                )
            if len(edges) > max_neighbors:
                lines.append(f"    ... {len(edges) - max_neighbors} more")
        return "\n".join(lines)


def iter_edges(graph: Graph, relation: str | None = None) -> Iterator[Edge]:
    """Convenience iterator over edges, optionally filtered by relation."""
    yield from graph.edges_where(relation=relation)
