"""Read-only, stdlib-only access to a ``graphify`` graph.

This package exists so an agent can query the code graph as **data** instead of
reading budget-capped text reports. ``graphify``'s MCP server answers questions
as prose (one line per edge, truncated at ``token_budget``); multi-hop questions
therefore cost several calls and leave the model doing set arithmetic over
English. Here the same questions are joins over dicts.

Design constraints (see issue #315):

* **stdlib only.** No ``networkx``. The graph is 10k nodes / 16k edges; plain
  dict indexes answer the interesting queries faster than a general graph
  library and keep the package importable inside a sandbox with a default-deny
  import surface.
* **read-only.** ``graph.json`` and ``GRAPH_REPORT.md`` are build artifacts.
  Nothing here writes them.
* **no truncation.** Every accessor returns complete results, with counts, so
  generated code can tell "no matches" apart from "budget ran out".
* **explicit paths.** ``load()`` resolves a project dir, a ``graphify-out`` dir,
  a ``graph.json`` file, or walks up from the CWD — the MCP server's
  CWD-relative default is why every call to it currently fails on this machine.
* **a moving target.** The graph is rewritten by a post-commit hook (your
  branch) and a cron watcher (``origin/master``), so a loaded ``Graph`` is a
  point-in-time snapshot. Call ``load()`` again to pick up a rebuild — the cache
  keys on mtime+size, so it reloads only when the file actually changed — and
  use :mod:`graph.provenance` to check which commit it reflects.

Typical use::

    import graph
    g = graph.load("~/LMAO")
    g.meta()                      # counts, relations, confidence breakdown
    g.node("Transport")           # resolve by id, norm_label or label
    g.edges_where(relation="calls", confidence="INFERRED")
    g.in_edges_of("_patch_leaf_node_path_request_delivery", relation="calls")
    graph.provenance.describe(graph.provenance.staleness(g, "origin/master"))

Derived analyses live in :mod:`graph.queries` — see ``queries.undocumented``
and friends for the questions that motivated this package.
"""

from __future__ import annotations

from graph import provenance, queries
from graph.model import (
    GRAPH_DIRNAME,
    GRAPH_FILENAME,
    AmbiguousNodeError,
    Edge,
    Graph,
    GraphError,
    GraphNotFoundError,
    Node,
    NodeNotFoundError,
    load,
    resolve_graph_path,
)

__all__ = [
    "GRAPH_DIRNAME",
    "GRAPH_FILENAME",
    "AmbiguousNodeError",
    "Edge",
    "Graph",
    "GraphError",
    "GraphNotFoundError",
    "Node",
    "NodeNotFoundError",
    "load",
    "provenance",
    "queries",
    "resolve_graph_path",
]

__version__ = "0.1.0"
