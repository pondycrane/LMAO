# LMAO Archon Workflows

Dedicated, version-controlled [Archon](https://archon.diy) workflows for this
repo — hardware-aware, Bazel-native, E2E-mandatory. They exist because the
generic bundled workflows (`archon-fix-github-issue`, `archon-idea-to-pr`, …)
know nothing about LMAO's hard requirements and repeatedly needed manual
rescue (see issue #89 for the full post-mortem of the #87 run).

## Workflows

| Workflow | When to use |
|----------|-------------|
| `lmao-fix-issue` | Fix / implement a GitHub issue end-to-end: classify → investigate/plan → implement → **gates** → draft PR → review → self-fix → simplify → **gates re-run** → report. |
| `lmao-feature-dev` | Feature idea → plan → implement → **gates** → PR → 5-agent review → fixes → **gates re-run** → summary. |

Both run entirely on pi/DeepSeek (`deepseek-v4-pro` for plan/implement/fix,
`deepseek-v4-flash` for the rest). No Claude references anywhere.

```bash
cd /home/pondycrane/LMAO
archon workflow run lmao-fix-issue "Fix issue #87"
archon workflow run lmao-feature-dev "Add humidity graphing to the ingest pod"
```

## The gate chain (what makes these LMAO-specific)

Every mutating phase is fenced by the same three-node gate chain:

```
lmao-validate  →  lmao-hardware-e2e  →  lmao-production-health
```

1. **`lmao-validate`** — Bazel-native validation:
   - **BUILD completeness**: every new/changed `*.py` must resolve to a Bazel
     target (`bazel query`). This is the issue #87 regression class — a test
     file without a `py_test` target silently never runs. Exemptions are
     explicit (vendored code, generated `*_pb2.py`, debug utilities).
   - `bazel build //...`
   - `ruff check` + `ruff format --check` on changed files
   - `mypy` on changed files
   - `bazel test //tests:all --test_tag_filters=-requires_hardware`
2. **`lmao-hardware-e2e`** — mandatory per AGENTS.md:
   - Detects attached devices (Cardputer `303a:8120`, RNode `10c4:ea60`).
   - Runs `bazel test //tests:test_cardputer_e2e --test_output=all --cache_test_results=no`
     and `//tests:test_cardputer_lora_e2e` (both devices) — never cached results.
   - Results table → `$ARTIFACTS_DIR/hardware-e2e.md` → **included in the PR body**.
   - Hardware absent → **loud skip** written into the artifact and the PR, never
     a silent pass.
3. **`lmao-production-health`** — confirms the production Cardputer resumed
   sending `Hello from Cardputer` (via the `lmao-server` journal) before the
   workflow may declare success.

**Fast-pass marker**: the chain writes `$ARTIFACTS_DIR/.gate-head` when it
completes. Re-runs after review/simplify phases skip the full chain *only*
when `HEAD` is unchanged; any new commit forces the full chain again —
including reflashing the hardware. This is the direct fix for "the simplify
phase regressed the e2e test and nothing re-ran it".

**Run-tree discipline**: every gate command starts with a mandatory
"locate the run tree" phase — agents must `cd` into the run worktree
(`~/.archon/workspaces/*/worktrees/archon/task-*`) before any git/bazel
command, and the marker always compares `git -C "$WT" rev-parse HEAD`.
Without this, agents run `git rev-parse HEAD` in the main checkout and the
marker logic silently misfires (found by the issue-#91 live test).

## Hardware safety rules (injected into every implementation/validation prompt)

- **NEVER run esptool on the Cardputer** — flash only via
  `bazel run //cardputer_client:flash` (raw REPL). esptool kills the
  USB-Serial-JTAG interface; recovery needs a physical unplug/replug.
- **NEVER flash the RNode via esptool** — the web flasher
  (https://flasher.rnode.network/) is the only supported method.
- **Leave the production Cardputer running** with restored config/`DEST_HASH`
  after any test session.
- Radio parameters (868 MHz, BW 125 kHz, SF 7, CR 4:5, preamble 24,
  syncword 0x1424) stay in sync between server and client.
- Vendored code (`cardputer_client/lib/urns/**`, `rnode_firmware/esptool.py`)
  is lint-exempt — agents must not "clean it up".
- Bazel runfiles workarounds (`sys.path` bootstraps, `imports = ["."]`,
  "Bazel sandbox" comments) are load-bearing — the simplify command explicitly
  forbids removing them (#87 regression).

## Project commands

The workflows are thin DAGs over project-local commands in
`.archon/commands/` (each is a self-contained prompt; there is no include
mechanism, so the safety preamble is repeated deliberately):

| Command | Adapted from | Key changes |
|---------|--------------|-------------|
| `lmao-validate` | `archon-validate` | Bazel/ruff/mypy instead of npm scripts; BUILD completeness check; fast-pass marker |
| `lmao-hardware-e2e` | *(new)* | Hardware detection, e2e targets, loud skip, results artifact |
| `lmao-production-health` | *(new)* | Server-journal health check, gate marker writer |
| `lmao-fix-issue` | `archon-fix-issue` | Bazel deps/build/test; BUILD-target rule for new files |
| `lmao-create-plan` | `archon-create-plan` | Bazel validation commands in plan template; requirements_lock.txt instead of package.json |
| `lmao-investigate-issue` | `archon-investigate-issue` | Same |
| `lmao-plan-setup` / `lmao-confirm-plan` | bundled | Bazel validation command examples/probes |
| `lmao-implement-tasks` | `archon-implement-tasks` | `bazel build` after each change; tests wired into BUILD |
| `lmao-self-fix-all` | `archon-self-fix-all` | Bazel validation; runfiles-workaround preservation |
| `lmao-simplify-changes` | `archon-simplify-changes` | Full unit gate after changes; explicit list of "never simplify away" patterns |
| `lmao-implement-review-fixes` | `archon-implement-review-fixes` | Bazel validation |
| `lmao-finalize-pr` | `archon-finalize-pr` | PR body must include the hardware E2E table |
| `lmao-sync-pr-with-main` | `archon-sync-pr-with-main` | Bazel validation after rebase |

Also in `.archon/workflows/`: the pi-adapted overrides
(`archon-idea-to-pr`, `archon-fix-github-issue`, `archon-feature-development`,
`archon-plan-to-pr`) moved here from `~/.archon/workflows/` so they're
versioned and shared. The home-directory copies remain as global fallbacks
for other projects; the repo copies are canonical for LMAO (project files
take discovery priority).

## Prompt nodes, not bash, for AI output

Any node that consumes another node's AI output is a `prompt` node, never a
`bash` node with `$node.output` embedded in a script — raw LLM output
(quotes, newlines, backticks) textually substituted into bash killed the
original `fetch-issue` node with `unexpected EOF`. The only `bash` nodes are
pure-file/git operations (`bridge-artifacts`, `verify-pr-base`, ntfy).

## Validating changes

After editing anything under `.archon/`:

```bash
archon validate workflows   # structure + command references
archon validate commands    # frontmatter + format
archon workflow list        # project workflows discovered
```
