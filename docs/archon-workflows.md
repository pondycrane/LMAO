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
| `smart-irrigation-dev` | Smart Irrigation node (Atom Lite + STM32WLE5CC DTU) work from the blueprint in `smart_irrigation/docs/development-plan.md`: phase features, or the mandatory Phase 0 algorithm evaluation. Firmware work is hard-blocked until `smart_irrigation/docs/algorithm-evaluation.md` exists (blueprint Appendix 13). |

Both run entirely on pi/DeepSeek (`deepseek-v4-pro` for plan/implement/fix,
`deepseek-v4-flash` for the rest). No Claude references anywhere.

```bash
cd /home/pondycrane/LMAO
archon workflow run lmao-fix-issue "Fix issue #87"
archon workflow run lmao-feature-dev "Add humidity graphing to the ingest pod"

# Smart Irrigation — run Phase 0 first (algorithm evaluation), then phases:
archon workflow run smart-irrigation-dev "Phase 0: algorithm evaluation"
archon workflow run smart-irrigation-dev "Phase 2: PCA9548A + sensor drivers"
```

## The gate chain (what makes these LMAO-specific)

Every mutating phase is fenced by the same three-node gate chain:

```
lmao-validate  →  lmao-hardware-e2e  →  lmao-production-health
```

`smart-irrigation-dev` uses the irrigation variant of the chain:

```
irrigation-validate  →  irrigation-hardware-e2e  →  irrigation-production-health
```

- **`irrigation-validate`** — the LMAO host gate (BUILD completeness, `bazel
  build //...`, ruff/mypy, unit tests) plus irrigation checks: MicroPython
  syntax (`mpy-cross`/`py_compile`), vendored `firmware/lib/urns/**` integrity
  against `cardputer_client/`, control-engine sweep/safety-override evidence,
  and protobuf round-trip checks.
- **`irrigation-hardware-e2e`** — reuses the LMAO Cardputer/RNode phases
  (including the production-path fallback from `lmao-hardware-e2e.md`) and
  adds Atom Lite checks only when `IRRIGATION_PORT` (or a verified fingerprint
  in `lma_core/device_detect.py`) is set — never auto-probes serial ports,
  since the Atom Lite bridge can share VID/PID `10c4:ea60` with the RNode.
  Missing hardware is a loud SKIP/UNVERIFIABLE written into the PR body.
- **`irrigation-production-health`** — owns `$ARTIFACTS_DIR/.gate-head`, checks
  the production Cardputer (journal or K8s) and, when the irrigation node is
  deployed, SensorReport/DB/JetStream health. Honest UNVERIFIABLE is recorded
  loudly.

### The irrigation evaluation-first gate

`smart-irrigation-dev` additionally enforces the blueprint's Appendix 13
mandate: if a request touches firmware/algorithm/protocol code and
`smart_irrigation/docs/algorithm-evaluation.md` does not exist, the workflow
fails at its deterministic `preflight` node with instructions to run Phase 0
first. Server/docs/host-script requests may proceed without it.

**Notifications**: `smart-irrigation-dev` sends start and completion/failure
messages through Hermes to the Telegram home channel
(`hermes send --to telegram`, credentials/target from `~/.hermes/.env`).
The completion message includes the PR URL and the hardware-E2E/validation
statuses when available. Notification failure is a warning only — it never
masks the workflow result.

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
     a silent pass. When only the Cardputer is attached (RNode on K8s, issue #93),
     the gate instead actively verifies the production LoRa path (server log +
     JetStream consumer health) — no skip.
3. **`lmao-production-health`** — confirms the production Cardputer resumed
   sending `Hello from Cardputer` (via the `lmao-server` journal) before the
   workflow may declare success.

**Fast-pass marker**: `lmao-production-health` writes `$ARTIFACTS_DIR/.gate-head`
when the full gate chain completes successfully. No other gate node may write
this marker — doing so causes downstream hardware gates to fast-pass without
executing (issue #100). Fast-pass in hardware-e2e and production-health also
requires the gate's own artifact to exist (defense-in-depth). Re-runs after review/simplify phases skip the full chain *only* when
`HEAD` is unchanged; any new commit forces the full chain again —
including reflashing the hardware. This is the direct fix for "the simplify
phase regressed the e2e test and nothing re-ran it".

PR creation (`lmao-fix-issue.yaml` create-pr node and the `lmao-finalize-pr`
command it delegates to; also used by `lmao-feature-dev`) requires
`$ARTIFACTS_DIR/hardware-e2e.md` to exist and aborts before any `git add`
if it is missing — a hard block against creating a PR with no hardware evidence.

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
| `lmao-finalize-pr` | `archon-finalize-pr` | PR body must include the hardware E2E table; blocks PR creation unless `$ARTIFACTS_DIR/hardware-e2e.md` exists |
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
