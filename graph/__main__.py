"""CLI over the graph package: ``python -m graph <command> [args]``.

Exists so the same queries can be run by hand and pasted into a PR or an issue
without writing a script. Output is plain text, complete (never budget-capped),
and ends with counts so a reader can tell "none" from "truncated".
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from graph import queries
from graph.model import Graph, GraphError, load


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False))


def _print_meta(g: Graph) -> None:
    meta = g.meta()
    print(f"graph: {meta['path']}")
    print(f"commit: {meta['built_at_commit']}")
    print(f"nodes: {meta['nodes']}  edges: {meta['edges']}  communities: {meta['communities']}")
    print("relations: " + ", ".join(f"{k}={v}" for k, v in meta["relations"].items()))
    print("confidence: " + ", ".join(f"{k}={v}" for k, v in meta["confidence"].items()))
    print("file_types: " + ", ".join(f"{k}={v}" for k, v in meta["file_types"].items()))


def _print_undocumented(result: dict) -> None:
    print(
        f"undocumented callables: {result['undocumented']} of {result['candidates']} "
        f"in scope {result['scope']} across {result['communities_affected']} communities"
    )
    for row in result["by_community"]:
        print(
            f"  community {row['community']:>4} {row['name'][:28]:<28} "
            f"{row['undocumented']:>4}/{row['total']:<4} e.g. "
            + ", ".join(s["name"] for s in row["sample"])
        )


def _print_gaps(result: dict) -> None:
    print(f"communities with zero rationale coverage: {result['count']}")
    for row in result["communities"]:
        print(
            f"  community {row['community']:>4} {row['name'][:28]:<28} "
            f"{row['code_nodes']:>4} code nodes  files={row['files'][:2]}"
        )


def _print_inferred(result: dict) -> None:
    print(
        f"INFERRED edges: {result['total_inferred']} of {result['total_edges']} "
        f"({', '.join(f'{k}={v}' for k, v in result['by_relation'].items())})"
    )
    print(f"  ranked by caller out-degree >= {result['min_out_degree']}:")
    for row in result["items"]:
        print(
            f"    {row['caller'][:32]:<32} -> {row['callee']} "
            f"[{row['relation']}] out={row['caller_out_degree']} at={row['where']}"
        )


def _print_rationale(result: dict) -> None:
    if "rationale" in result:
        print(f"{result['node']} ({result['id']}) at {result['where']}: {result['count']} notes")
        for note in result["rationale"]:
            print(f"  - {note['text'][:160]}")
        return
    print(
        f"rationale coverage: {result['documented']}/{result['code_nodes']} code nodes "
        f"({result['coverage_pct']}%), {result['rationale_edges']} rationale_for edges"
    )


def _print_impact(result: dict) -> None:
    print(
        f"files: {result['files_matched']}/{result['files_requested']} matched, "
        f"{result['nodes_touched']} nodes, {result['communities_touched']} communities "
        f"-> risk {result['risk']}"
    )
    if result["files_missing"]:
        print(f"  not in graph: {result['files_missing']}")
    for row in result["communities"]:
        print(
            f"  community {row['community']:>4} {row['name'][:28]:<28} "
            f"{row['nodes']}/{row['total_in_community']} nodes"
        )
    if result["core_abstractions_hit"]:
        print("  core abstractions hit:")
        for row in result["core_abstractions_hit"]:
            print(f"    {row['name'][:40]:<40} degree={row['degree']} at={row['where']}")


def _print_matrix(result: dict) -> None:
    print(f"cross-community edges: {result['cross_community_edges']}")
    for row in result["top"]:
        print(
            f"  {row['from']:>4} {row['from_name'][:22]:<22} -> "
            f"{row['to']:>4} {row['to_name'][:22]:<22} [{row['relation']}] ×{row['count']}"
        )


def _print_callers(result: dict) -> None:
    print(f"{result['node']} ({result['id']}) at {result['where']}: {result['count']} callers")
    for row in result["callers"]:
        print(f"  <- {row['caller']} [{row['confidence']}] at={row['where']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m graph", description=__doc__)
    parser.add_argument("--graph", dest="graph_path", default=None,
                        help="project dir, graphify-out dir, or graph.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="counts and breakdowns")
    sub.add_parser("god-nodes", help="most-connected nodes")

    p_und = sub.add_parser("undocumented", help="code with no rationale_for edge")
    p_und.add_argument("--prefix", default=None, help="limit to files under this prefix")
    p_und.add_argument("--all-nodes", action="store_true", help="include non-callable nodes")

    p_gap = sub.add_parser("gaps", help="communities with zero rationale coverage")
    p_gap.add_argument("--min-code-nodes", type=int, default=5)

    p_inf = sub.add_parser("inferred-calls", help="INFERRED edges ranked by blast radius")
    p_inf.add_argument("--min-out-degree", type=int, default=0)
    p_inf.add_argument("--limit", type=int, default=30)

    p_rat = sub.add_parser("rationale", help="rationale coverage, or one node's rationale")
    p_rat.add_argument("node", nargs="?", default=None)

    p_imp = sub.add_parser("impact", help="communities and core abstractions touched by files")
    p_imp.add_argument("files", nargs="+")

    sub.add_parser("relations", help="cross-community relation matrix")

    p_call = sub.add_parser("callers", help="who calls a node")
    p_call.add_argument("node")
    p_call.add_argument("--relation", default="calls")

    p_search = sub.add_parser("search", help="substring search over labels and files")
    p_search.add_argument("text")
    p_search.add_argument("--limit", type=int, default=15)

    p_node = sub.add_parser("node", help="describe one node")
    p_node.add_argument("key")

    args = parser.parse_args(argv)

    try:
        g = load(args.graph_path)
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "summary":
        _print_meta(g)
    elif args.command == "god-nodes":
        for node, deg in g.god_nodes(15):
            print(f"  {deg:>5}  {node.name[:44]:<44} {node.where()}")
    elif args.command == "undocumented":
        _print_undocumented(
            queries.undocumented(
                g, prefix=args.prefix, callable_only=not args.all_nodes
            )
        )
    elif args.command == "gaps":
        _print_gaps(queries.gaps(g, min_code_nodes=args.min_code_nodes))
    elif args.command == "inferred-calls":
        _print_inferred(
            queries.inferred_calls(g, min_out_degree=args.min_out_degree, limit=args.limit)
        )
    elif args.command == "rationale":
        _print_rationale(queries.rationale(g, args.node))
    elif args.command == "impact":
        _print_impact(queries.impact(g, args.files))
    elif args.command == "relations":
        _print_matrix(queries.relation_matrix(g))
    elif args.command == "callers":
        _print_callers(queries.callers(g, args.node, relation=args.relation))
    elif args.command == "search":
        hits = g.search(args.text, limit=args.limit)
        print(f"{len(hits)} matches (most-connected first):")
        for node in hits:
            print(f"  {g.degree(node.key):>5}  {node.name[:44]:<44} {node.where()}")
    elif args.command == "node":
        print(g.describe(g.node(args.key)))
    else:  # pragma: no cover - argparse enforces the choices
        print(f"unknown command {args.command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
