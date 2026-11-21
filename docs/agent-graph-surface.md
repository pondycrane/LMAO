# Agent graph surface (`graph/`)

A read-only, stdlib-only Python package over `graphify-out/graph.json`, so an
agent can query the code graph as **data** instead of reading budget-capped text
reports.

This is phase P4 of [#315](https://github.com/pondycrane/LMAO/issues/315) — the
code-mode design. It was sequenced first because it is the cheapest place to
prove the pattern: read-only, local, no radio, no credentials, no airtime.

## Why

`graphify`'s MCP server exposes 10 tools that answer in prose — one line per edge,
truncated at `token_budget` (default 2000). Multi-hop questions therefore cost
several calls and leave the model doing set arithmetic over English. The LMAO
graph is large enough that this bites immediately:

| Metric | Value (LMAO, 2026-11-19) |
|---|---|
| nodes | 10,135 |
| links | 16,474 |
| communities | 757 |
| relation types | 14 (`calls` 4,147 · `contains` 3,970 · `method` 2,438 · `rationale_for` 2,151) |
| confidence | `EXTRACTED` 15,584 · `INFERRED` 890 (459 of them `calls`) |
| `GRAPH_REPORT.md` | 97,221 chars ≈ 24.3K tokens if read whole |
| out-degree >10 | 279 nodes (>30: 25, max 111) |

Tool schemas are only ~2.1K tokens per server, so the win here is **not** schema
saving — it is the *shape of the result*. A join over dicts returns one answer;
a prose dump returns a page the model must mentally union.

## Use

```python
import graph

g = graph.load("~/LMAO")          # project dir, graphify-out dir, or graph.json
g.meta()                          # counts, relations, confidence breakdown
g.node("Transport")               # resolve by id, norm_label or label; "foo" == "foo()"
g.edges(relation="calls", confidence="INFERRED")
g.in_edges_of("_patch_leaf_node_path_request_delivery", relation="calls")
```

Path resolution accepts a project directory, a `graphify-out` directory, a
`graph.json` file, or walks up from the CWD. `GRAPHIFY_PROJECT_DIR` and
`GRAPHIFY_OUT` are honoured — note that graphify's own MCP server reads
`GRAPHIFY_PROJECT_DIR` from the environment but **ignores** it, which is why its
default graph path resolves against the CWD.

### Derived queries

`graph.queries` holds the questions worth asking as set operations:

| Function | Question |
|---|---|
| `rationale(g, key)` | the prose attached to a symbol, or coverage statistics |
| `undocumented(g, prefix=)` | code with no `rationale_for` edge, ranked by community |
| `gaps(g)` | communities with **zero** rationale coverage but real code |
| `inferred_calls(g)` | `INFERRED` edges ranked by caller out-degree (audit queue) |
| `callers(g, key)` / `callees(g, key)` | is this reached, and from where |
| `impact(g, files)` | changed files → communities → core abstractions hit |
| `relation_matrix(g)` | which relations cross which community boundaries |

### Provenance — the graph is a moving target

`graph.json` is rewritten by **two** writers on this machine, not one:

| Writer | Trigger | Builds from |
|---|---|---|
| `graphify hook install` post-commit hook | every commit in a checkout | that branch |
| `graph-watch-branch.sh` (cron, every 15 min) | `origin/master` advancing | a detached worktree at `origin/master`, then `rsync` over the canonical `graphify-out/` |

So the graph can be at *your branch* one minute and *master* the next, and it is
routinely a few commits behind either. `built_at_commit` is the only way to tell,
and querying `impact()` against a graph built from a different commit silently
describes code that has moved.

```python
import graph
g = graph.load("~/LMAO")
graph.provenance.describe(graph.provenance.staleness(g, "origin/master"))
```

```bash
$ python -m graph --ref origin/master staleness --dirty
graph is current with HEAD (95b1f0918)
  graph commit: 95b1f0918...
  ahead=0 behind=0
  uncommitted files (also unseen by the graph): 16

$ python -m graph --ref origin/master summary
provenance: graph DIVERGED from origin/master (1 ahead, 2 behind) at 95b1f0918
```

Statuses: `current`, `behind` (ref has commits the graph has not seen),
`ahead` (graph built from commits not reachable from ref — e.g. your branch),
`diverged`, `unknown` (no git, no `built_at_commit`, or the graph commit is not
in this clone). It degrades to `unknown` rather than raising, so provenance is
never a hard dependency of the read path. `check_dirty=True` also counts
uncommitted files, which the graph cannot reflect either — the hook only fires
*after* a commit.

`queries.impact()` returns `graph_commit` in its result for the same reason: the
answer carries its own provenance.

### The graph is a snapshot, not a live view

A loaded `Graph` does not mutate when a rebuild lands. Call `load()` again to pick
one up — the cache keys on `(path, mtime_ns, size)`, so it reloads only when the
file actually changed. Both writers replace `graph.json` atomically
(`_write_json_atomic` / `rsync` temp-then-rename), so a read never sees a torn
file.

### CLI

```bash
python -m graph --ref origin/master staleness --dirty
python -m graph summary
python -m graph undocumented --prefix lma_core/
python -m graph gaps
python -m graph rationale _patch_leaf_node_path_request_delivery
python -m graph callers _patch_leaf_node_path_request_delivery
python -m graph inferred-calls --min-out-degree 20
python -m graph impact lma_core/rns_init.py lma_core/storage.py
python -m graph relations
python -m graph node NatsQueue
```

## What it answers today

```
$ python -m graph summary
nodes: 10135  edges: 16474  communities: 757
confidence: EXTRACTED=15584, INFERRED=890

$ python -m graph rationale
rationale coverage: 2143/5915 code nodes (36.2%), 2151 rationale_for edges

$ python -m graph callers _patch_leaf_node_path_request_delivery
_patch_leaf_node_path_request_delivery() at lma_core/rns_init.py:L41: 1 callers
  <- init_rns_and_lxmf() [EXTRACTED] at=lma_core/rns_init.py:L231

$ python -m graph gaps
communities with zero rationale coverage: 86
  community    0 interrupt_stubs.rs    96 code nodes
  community   17 radio-interface/src/lib.rs  56 code nodes
```

The `callers` answer is the one that motivated the package: a correctness
question about `rns_init.py`'s leaf-node patch, answered in one call, where the
prose interface would need `get_neighbors` on a 164-edge god node and manual
filtering.

## Design constraints

- **stdlib only.** No `networkx`. Plain dict indexes answer these queries faster
  than a general graph library and keep the package importable under a
  default-deny import surface, which matters once it runs inside a sandbox.
- **read-only.** `graph.json` and `GRAPH_REPORT.md` are build artifacts. Nothing
  here writes them.
- **never truncated.** Every accessor returns complete results with counts, so
  generated code can tell "no matches" apart from "budget ran out". This is the
  opposite of the MCP tools' `[!] TRUNCATED` behaviour, which is a real footgun
  when aggregating.
- **fail loudly on ambiguity.** `node("foo")` raises `AmbiguousNodeError` with
  candidates rather than silently picking the first match. `nodes_in_file`
  behaves the same for suffix collisions.
- **external symbols excluded** from documentation-coverage counts — they cannot
  be documented in this repository.

## Not in scope

- Writing or rebuilding the graph. Two writers own that: the post-commit hook and
  the cron watcher. `graphify-out/` is build output with a
  `.graphify_labels.json.sig` sidecar; treat it as an input.
- Deciding *when* to rebuild. The watcher already skips no-op polls (it compares
  the fetched tip against a state file) and prunes dated snapshots to 7.
- GitHub API access (`list_prs`, `get_pr_impact`, `triage_prs`). Those need auth
  and belong behind typed functions, never a raw HTTP client in a sandbox
  namespace.
- The MCP resources (`graphify://report` etc.). Nobody should pull 24.3K tokens
  of report into context; targeted queries replace them.

## Tests

```bash
python -m pytest tests/test_graph.py tests/test_graph_provenance.py
```

55 tests. The query suite runs against a synthetic graph, so it needs no built
`graphify-out`; the provenance suite builds a throwaway git repo and skips
cleanly when git is absent.
