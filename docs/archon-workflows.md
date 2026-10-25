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
| `smart-irrigation-dev` | **⚠️ NOT READY TO RUN** (pending refinement in [#122](https://github.com/pondycrane/LMAO/issues/122)) — do not run until refined. When ready: Smart Irrigation node (Sprout) work from the blueprint in `smart_irrigation/docs/development-plan.md`: phase features, or the mandatory Phase 0 algorithm evaluation. Firmware work is hard-blocked until `smart_irrigation/docs/algorithm-evaluation.md` exists (blueprint Appendix 13). |
| `robodog-client-dev` | Feature idea → plan → implement → **gates** → PR → 5-agent review → fixes → **gates re-run** → summary, for the **RoboDog** device (Waveshare ESP32-S3 RLCD-4.2 / ST7305 family-calendar display). Verification is **pixel-driven**: the mandatory hardware gate builds + flashes the real board and **screen-captures the exact ST7305 pixels** (`capture.py` streams the live u8g2 buffer over USB-JTAG), then reads the render back with the **vision model** (Qwen2.5-VL via `capture.py --describe`) — the PR carries the captured frames + verbatim transcription, not a re-render. Loud skip when the board (VID `0x303A` PID `0x1001`) is not attached. |
| `robodog-hermes-review` | Review-entry twin of `robodog-hermes-dev` for an **existing** PR on the hermes voice device. The PR must exist first (create it manually via `gh pr create` from the feature branch). Flow: `checkout-pr` (resolves "review PR #N" from ARGUMENTS, checks the PR head out in the isolated worktree) → verify-pr-base → review-scope → sync → 5 parallel review agents → synthesize → implement-fixes → full gate chain re-run (validate → hermes screen-capture e2e → voice-loop e2e → production-health) → summary. Run: `archon workflow run robodog-hermes-review "review PR #N"`. Same board/board-rules as `robodog-hermes-dev`. |
| `robodog-hermes-dev` | Feature idea → plan → implement → **gates** → PR → 5-agent review → fixes → **gates re-run** → summary, for the **RoboDog hermes voice device** — the SAME Waveshare board running the XiaoZhi voice firmware (`robodog_client/voice`, self-built; RD_SHOT capture hook + **RD_LISTEN software button**) + the voice bridge (ASR → Hermes brain → Kokoro TTS) + the acoustic **voice-loop E2E** ([#267](https://github.com/pondycrane/LMAO/issues/267), `robodog-voice-e2e`). Verification is **voice-loop-driven**: the mandatory gates flash the hermes image and drive RD_LISTEN → WS session → test-speaker command → board-mic ASR → Hermes reply → test-mic **ring closure** → RLCD **subtitle readback**, plus the hermes screen capture. **Same board, mutually exclusive with the calendar firmware** — never run both workflows at once, and never the calendar gates during hermes work. |
| `robodog-claw-review` | Review-entry twin of `robodog-claw-dev` for an **existing** PR on the RoboDog **ESP-Claw standalone agent** (the same `0x303A:0x1001` board, `robodog_client/esp-claw/`, self-built pinned upstream + LMAO overlay). The PR must exist first (`gh pr create`). Flow: `checkout-pr` → verify-pr-base → review-scope → sync → 5 parallel review agents → synthesize → implement-fixes (rebuild + **re-flash via `flash_esp_claw.sh`** when firmware changed — the claw e2e gate never reflashes) → full gate chain re-run → summary. Run: `archon workflow run robodog-claw-review "review PR #N"`. |
| `robodog-claw-dev` | Feature idea → plan → implement → **gates** → PR → 5-agent review → fixes → **gates re-run** → summary, for the **RoboDog ESP-Claw standalone agent** — the SAME Waveshare board running the esp-claw edge_agent (`robodog_client/esp-claw/`, self-built pinned upstream + LMAO overlay: text-only UI + RD_SHOT + the **Phase 2 native voice loop** `rd_listen → ES7210 mic → energy-VAD → Whisper ASR → claw_agent_mgr → Kokoro TTS → ES8311 speaker → RLCD render`; the board calls Whisper/LLM/Kokoro on its own, no bridge). Verification is **voice-loop-driven**: `robodog-claw-e2e` does serial-console asserts (OPEN_SESSION / ASR_MATCH / REPLY / TTS / RING / SUBTITLE) + test-mic ring re-ASR + RD_SHOT subtitle readback. Mutually exclusive with the calendar/hermes firmware. |

Both run entirely on pi routing to the in-house GX10 box — every node uses
the local DeepSeek V4 Flash model (`gx10/deepseek-ai/DeepSeek-V4-Flash-0731`,
such as `gx10-f156.local:8000/v1`, defined in `~/.pi/agent/models.json`). The
cloud DeepSeek endpoint is no longer used by default. No Claude anywhere (in
`robodog-client-dev` the agent model is GX10 too; the separate Qwen2.5-VL
vision readback inside the capture gate is a fixed pipeline, not the agent
model).

```bash
cd /home/pondycrane/LMAO
archon workflow run lmao-fix-issue "Fix issue #87"
archon workflow run lmao-feature-dev "Add humidity graphing to the ingest pod"
archon workflow run robodog-client-dev "Add a shopping-list icon + re-tune the row layout"

# Smart Irrigation — ⚠️ workflow NOT ready yet (#122): do not run it.
# Phase 0 (algorithm evaluation) is currently a MANUAL gate — write
# smart_irrigation/docs/algorithm-evaluation.md directly (blueprint App 13).
# Manual hardware E2E:  bazel test //tests:test_sprout_e2e --test_output=all
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
  adds Atom Lite checks using the **verified fingerprint** in
  `lma_core/device_detect.py` (FTDI `0403:6001`, product `M5stack`) or
  `IRRIGATION_PORT` — generic FT232 bridges are never blind-probed. The
  primary check is the read-only device probe
  `mpremote connect <port> run smart_irrigation/firmware/tools/probe_hardware.py`
  (I2C mux + SHT30 + DTU AT identity), compared against
  `smart_irrigation/docs/hardware-verification.md`. The gate also enforces the
  **pump safety precondition** (watering module disconnected; no pump pin is
  ever driven). Missing hardware is a loud SKIP/UNVERIFIABLE written into the
  PR body.
- **`irrigation-production-health`** — owns `$ARTIFACTS_DIR/.gate-head`, checks
  the production Cardputer (journal or K8s) and, when the irrigation node is
  deployed, SensorReport/DB/JetStream health. Honest UNVERIFIABLE is recorded
  loudly.

### The robodog pixel-driven gate chain

`robodog-client-dev` uses a display-oriented variant of the chain:

```
robodog-validate  →  robodog-hardware-e2e  →  robodog-production-health
```

- **`robodog-validate`** — the LMAO host gate (BUILD completeness, `bazel
  build //...`, ruff/mypy, `bazel test //tests:all
  --test_tag_filters=-requires_hardware`) plus robodog host units: the
  dependency-free wire codec selftest (`cc` build + run of
  `main/lma_proto_selftest.c`), a `capture.py --help` sanity check, and — when
  C sources changed — a real firmware compile via `bazel run
  //robodog_client:build_firmware` (ESP-IDF Docker; `bazel build //...` skips
  the manual-tagged firmware targets, so C errors surface before the board is
  touched). `robodog_client/capture.py` joins the BUILD-completeness exemption
  list as a standalone host tool. No hardware is touched here.
- **`robodog-hardware-e2e`** — the verification gate. It builds + flashes the
  real RoboDog board (Waveshare ESP32-S3 RLCD-4.2; verified fingerprint
  `0x303A:0x1001` from `lma_core/device_detect.py`, or the `ROBODOG_PORT`
  override — **never** the Cardputer `0x8120` or RNode), then **screen-captures
  the exact ST7305 pixels**: `capture.py --reset --n 4 --timeout 120 --describe`
  streams the live u8g2 frame buffer (300×400 panel memory) over USB-JTAG and
  reconstructs `panel_raw_portrait.png` / `panel_landscape.png`, and `--describe`
  sends the frame to the same self-hosted vision model hermes uses (Qwen2.5-VL
  at `spark-eebf`, `~/.hermes/config.yaml "vision"`) which **transcribes** the
  header clock + every row. The agent asserts `HEADER_PRESENT` (sync date/time
  in the readback), `ROWS_RENDERED` (≥1 calendar row), `NO_UNEXPECTED_STATUS`
  (no `no wifi`/`stale` on the synced frame unless that *is* the feature) and,
  best-effort, `ROW_MATCH` against the server's `CalendarBundle`. The evidence
  (verbatim transcriptions + frame PNG paths) is written to the canonical
  `$ARTIFACTS_DIR/hardware-e2e.md` that the PR gate consumes — no re-render, no
  model guess. No board → **loud skip** (plus offline `--i1` decode evidence
  when a prior dump exists).
- **`robodog-production-health`** — owns `$ARTIFACTS_DIR/.gate-head`. After the
  E2E flash it confirms the panel is at rest healthy: one non-destructive
  `capture.py --n 1 --timeout 60 --describe` frame (no reset, no flash) showing
  a live sync, plus a best-effort probe that the LMAO server (the panel's
  `GetCalendar` data source) is serving. Honest UNVERIFIABLE is recorded loudly.

### The robodog hermes voice-loop gate chain

`robodog-hermes-dev` is the **same board** (Waveshare ESP32-S3 RLCD-4.2,
`0x303A:0x1001`) running the **XiaoZhi voice firmware instead of the calendar
app** — the two are mutually exclusive flashes, so the hermes chain runs its OWN
gates (never `robodog-hardware-e2e`) and this whole section assumes the hermes
image is on the board:

```
robodog-hermes-validate → robodog-hermes-e2e → robodog-voice-e2e →
robodog-production-health
```

- **`robodog-hermes-validate`** — the same LMAO host gate (BUILD completeness,
  `bazel build //...`, ruff/mypy, `bazel test //tests:all
  --test_tag_filters=-requires_hardware`) but the firmware-compile step is
  `bazel run //robodog_client:build_robodog_hermes` (XiaoZhi, ESP-IDF Docker)
  and it adds a `robodog_client/voice/` corpus sanity
  (`make_corpus --verify`). Coverage uses reverse-dep matching so the voice
  `*.py` wired as `data` of the `sh_binary` targets count as covered.
- **`robodog-hermes-e2e`** — builds + flashes the hermes image via
  `bazel run //robodog_client:build_robodog_hermes` /
  `:flash_robodog_hermes`, then **screen-captures the exact ST7305 pixels**
  (`capture.py --port <port> --reset --n 8 --timeout 180 --describe`; RD_SHOT
  frames → `panel_landscape.png` → Qwen2.5-VL readback). Writes
  `$ARTIFACTS_DIR/hermes-hardware-e2e.md` (verbatim transcriptions + PNGs).
- **`robodog-voice-e2e`** — the acoustic **voice loop** (issue #267):
  `bazel run //robodog_client:hermes_voice_e2e` drives `RD_LISTEN` (software
  button) → `device connected`/`listen start` → test speaker plays a pinned
  corpus command into the board mic → `ASR text=` normalized match → `Hermes …
  reply:` non-empty → test-mic **ring closure** re-ASR → `capture.py --describe`
  subtitle readback. Five assertions reported per loop (`OPEN_SESSION`,
  `ASR_MATCH`, `BRAIN_REPLY`, `RING`, `SUBTITLE`); flaky real-speech ASR
  loops fall back to the documented `E2E_STT` injection (`POST /xiaozhi/inject`)
  and are reported verbatim. Loud skip when the board or the ALSA test card is
  absent. Writes `$ARTIFACTS_DIR/robodog-voice-loop-e2e.md`.
- **`robodog-production-health`** — owns `.gate-head`, shared with the calendar
  workflow (never written by any gate before it).

**PR contract**: `robodog-hermes-finalize-pr` requires `hermes-hardware-e2e.md`
**and** `robodog-voice-loop-e2e.md` (not the calendar `hardware-e2e.md`);
embedding both verbatim in the PR body is mandatory.

### The robodog ESP-Claw voice-loop gate chain

`robodog-claw-dev` / `robodog-claw-review` target the **same Waveshare board**
(Waveshare ESP32-S3 RLCD-4.2, `0x303A:0x1001`) running the **esp-claw edge_agent**
firmware (`robodog_client/esp-claw/`) — the third firmware image for the board,
so the claw chain runs its OWN gates (never `robodog-hardware-e2e` / the hermes
gates) and is mutually exclusive with the calendar and hermes flashes:

```
lmao-validate  →  robodog-claw-e2e  →  robodog-production-health
```

- **`lmao-validate`** — the LMAO host gate
  (BUILD completeness, `bazel build //...`, ruff/mypy, unit tests). The claw
  firmware is compiled for the board by `build_esp_claw.sh`
  (`espressif/idf:v5.5.4` Docker, esp-claw pinned upstream + LMAO overlay).
- **`robodog-claw-e2e`** — the **voice loop** on the real board: it runs the
  fixed corpus WAV into the ES7210 mic through the ALSA test speaker, then
  asserts the full device-initiated chain from the **device serial console**
  (OPEN_SESSION `listen started` / ASR_MATCH `ASR text=` / REPLY `agent reply:`
  / TTS `TTS start`+`TTS stop`), re-ASRs the board's spoken reply via the test
  mic (RING), and reads the RLCD reply back with the Qwen2.5-VL vision model
  (SUBTITLE). It **never reflashes** — re-flash via
  `flash_esp_claw.sh`; loud skip when the board or the ALSA test card is
  absent. Writes `$ARTIFACTS_DIR/robodog-claw-e2e/robodog-claw-e2e.md` plus a
  top-level `hardware-e2e.md` summary.
- **`robodog-production-health`** — owns `.gate-head`, shared across the
  calendar/hermes/claw chains (never written by any gate before it).

**PR contract**: `lmao-finalize-pr` requires
`robodog-claw-e2e/robodog-claw-e2e.md` (via top-level `hardware-e2e.md`);



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
command it delegates to; also used by `lmao-feature-dev` and
`robodog-client-dev`) requires
`$ARTIFACTS_DIR/hardware-e2e.md` to exist and aborts before any `git add`
if it is missing — a hard block against creating a PR with no hardware evidence.
`robodog-hardware-e2e` writes that same canonical filename with robodog capture
evidence (frame transcriptions + PNG paths), so the existing PR gate holds
unchanged.

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
| `robodog-create-plan` | `lmao-create-plan` | RoboDog domain: capture-first task decomposition (pixel-driven, never re-render), wire/pictures/server slices |
| `robodog-implement-tasks` / `robodog-implement-review-fixes` | `lmao-implement-tasks` / `lmao-implement-review-fixes` | Explicit that host esptool via `flash.sh` is the SANCTIONED robodog-board flash (Cardputer/RNode stay banned); display changes verified from captured frames |
| `robodog-validate` | `lmao-validate` | + `lma_proto_selftest` host build/run; `capture.py` exempt; firmware compile via `bazel run //robodog_client:build_firmware` when C changed |
| `robodog-hardware-e2e` | *(new)* | Build + flash the board, **screen-capture the exact ST7305 pixels** (`capture.py --describe`) and **verify via the Qwen2.5-VL readback**; loud skip on `0x303A:0x1001` absence; writes canonical `hardware-e2e.md` |
| `robodog-production-health` | `lmao-production-health` | One non-destructive panel frame post-E2E + calendar-server probe; `.gate-head` owner |

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
