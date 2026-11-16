# dsh-superagent — issue → PR driver (issue #303)

`dsh-superagent` is the DeepSeek Harness bundle that turns a GitHub mention
into a Ticket, an Executor Session and a pull request. This document covers
**this deployment**: where it runs, how it is installed, what is local, and
what is still an operator step.

Upstream: `deepseek-ai/dsh-superagent-prototype`, MIT, **research prototype —
not an official DeepSeek product**. Pinned here at
`8c32a9dc5d72c033816c6a8110b37f40cf94fba4`. It is a bundle on top of `dsh`
(no core patches), installed as `dsh plugin add link:<checkout>`.

## Shape

```
GitHub poll (agent account notifications)
        │
   Trigger → Thread → Eval (deterministic gate + model assessment)
                             │ accept (explicit) / would-accept (observe)
                     Ticket + Mandate
                             │ Assignment
                     Executor Session  worker-<ticket>
                             │ prepare: git worktree from a persistent clone
                     bench submit build / flash_capture  (op4:8099)
                             │
                     Channel reply (comment / PR)
```

Workers never talk to people: the router Session owns every human-facing
message. The gate is code (allowed list + admission table), so a prompt
injection in an issue cannot argue past it.

## Where it runs — and why

**selfhost (this host), as a systemd service.** No inbound ingress is needed:
the Source polls the agent account's notifications, so placement is driven by
tooling proximity. The candidates in the issue:

| Host | Verdict |
|---|---|
| tp1 | Rejected: `wlan0` is down and the node has no route out (`No route to host` from the dev box; a hostNetwork pod on tp1 cannot pull an image — DNS fails). It cannot reach GitHub, the registry, or the bench. |
| tp3 | Rejected for now: reaches GitHub/npm/registry, but **not** the bench (`bench:000`), so a worker could not verify hardware. |
| tp4 | Rejected: production RNode + postgres + NATS path (#93 blast radius). |
| selfhost | **Used**: GitHub 200, npm 200, registry 200, bench 200, and the local model endpoint. 41 GB free, 4 cores. |

Because it is not in the cluster, the K8s-specific parts of the issue's
deployment shape do not apply: no PVC, no `CronJob` health check, no
`kubectl port-forward`. The equivalents are a systemd unit (restart policy
instead of a health CronJob) and an SSH tunnel for the panel.

## Everything is local

- **Model**: `http://spark-c34f:8888/v1` (TensorFold **DeepSeek-V4.1-Flash-1017**
  on the LAN). `llm-deepseek` is pointed there by `baseURL`; the credential is
  the literal `dummy-local-noauth` — the endpoint does not authenticate, and
  nothing leaves the network. `agent-default-model` is pinned to the local id,
  because the shipped default (`deepseek-flash`) is not served by this engine.
- **Device verification**: the bench (`cluster-ops/benchd` on op4,
  `BENCH_URL=http://op4:8099`). The worker host needs only `git`, `curl` and
  the `bench` client.
- **The only remote identity** is the agent's GitHub account (comments, pushes,
  PRs). It is a GitHub *user* account, not a GitHub App: the Source reads the
  account's notifications, which an App installation cannot do.

## Isolation: a dedicated OS account

Workers run with `danger-full-access` as the service's OS user. A worker can
execute a program by absolute path and read that user's home, so the wrapper
scripts are **defaults, not a sandbox** (`docs/006-deployment.md`). The
deployment therefore runs as **`dsh-agent`** (uid 1001, local account — *not* a
cloud identity):

- it holds only the agent's credentials (`~/.config/gh-agent`, the pinned
  client identities it needs) — never `pondycrane`'s `gh` login or SSH keys;
- its home is `0750`, so `pondycrane` cannot be read from inside a worker;
- the plugin rewrites process-wide `PATH` and a `worker-gitconfig` that
  replaces global/system git config, which is exactly why it is its own
  account and its own service, never co-tenant with the bench or with a
  personal profile.

## Layout

| Path | What |
|---|---|
| `/opt/node24` | Node 24.22.2 (symlink to `/opt/node-v24.22.2-linux-<arch>`) |
| `/home/dsh-agent/.local/bin/dsh` | DeepSeek Harness 0.2.1-rc.3 |
| `/home/dsh-agent/src/dsh-superagent` | bundle checkout, pinned commit |
| `/home/dsh-agent/.dsh/profiles/superagent` | profile: `cordis.patch.yml` (machine config) |
| `/home/dsh-agent/.dsh/state/superagent` | state dir: `orchestrator.sqlite`, `worker-bin`, `worker-gitconfig`, `work/` |
| `/home/dsh-agent/src/LMAO` | persistent clone the prepare command adds worktrees from |
| `/home/dsh-agent/.local/bin/gh-agent` | `gh` with `GH_CONFIG_DIR=~/.config/gh-agent` |
| `/home/dsh-agent/.local/bin/bench` | bench client (`cluster-ops/benchd/bench`) |
| `/etc/lmao/dsh-superagent.env` | `PATH`, `HOME`, `DEEPSEEK_API_KEY`, `BENCH_URL` (mode 600) |
| `/etc/systemd/system/dsh-superagent.service` | the service |

## Install

```bash
sudo bash tools/dsh_superagent_install.sh          # idempotent
sudo bash tools/dsh_superagent_install.sh --force-config   # replace machine config
```

It creates the OS account, installs Node/pnpm/dsh, clones and builds the
bundle at the pinned commit, creates the `superagent` profile from the Web
template (never clobbering an existing one), installs the wrappers and the
persistent clone, writes the unit + env file, starts the service, and applies
the settings.

Settings are applied through the local Management API by
`tools/dsh_superagent_settings.py` (also `--check` to print the plan):

```bash
python3 tools/dsh_superagent_settings.py
```

What it writes:

- **Policy** — accept mode **`observe`** (shadow mode: a positive assessment is
  recorded as "would accept", nothing is accepted); allowed list
  `[pondycrane]`; three rules: `copy`, `robodog-device` (bench-verified device
  work), `lmao-server` (server/protocol fixes).
- **Executor** — `maxWorkers 2`; prepare command creating one worktree per
  Ticket from `/home/dsh-agent/src/LMAO`; `blockedCommands`: `esptool`,
  `esptool.py`, `espsecure(.py)`, `espefuse(.py)`, `mpremote`, `capture.py`,
  `flash.sh`.
- **Prompts** — the default Communication/Scope sections plus the **device
  contract** appended to Delivery requirements: verification is `bench submit
  build` + `bench submit flash_capture`; pass is the bench verdict; never run
  `esptool`, `flash.sh` or `capture.py` locally; exit 75 = busy (retry), exit
  78 = quarantined (report `blocked`, the board needs a physical replug).
- **GitHub Source** — `pondycrane/LMAO`, agent CLI
  `/home/dsh-agent/.local/bin/gh-agent`. Saving this runs the setup check
  (agent account readable + push access), so it is the step that cannot
  complete until the agent account exists.

## Operate

```bash
systemctl status dsh-superagent
journalctl -u dsh-superagent -f

# The panel is loopback-only. Tunnel it, then open the tokenized URL printed
# at startup (the token is a process credential — do not paste it anywhere):
ssh -N -L 3082:127.0.0.1:3082 selfhost
journalctl -u dsh-superagent -n 40 | grep -o 'http://127.0.0.1:3082/?token=[A-Za-z0-9_-]*' | tail -1

curl -s -H 'x-orchestrator-request: 1' http://127.0.0.1:3082/orchestrator/api/state
```

`scripts/dsh-superagent status|start|stop` (upstream) manage the tmux-based
deployment; **do not use them here** — the unit owns the process, and a tmux
copy would be a second instance on the same port. A graceful stop waits up to
`TimeoutStopSec=330` for Executors, matching the upstream 5-minute rule.

## Verify

Proven on this host:

| Check | Evidence |
|---|---|
| Runtime on Node ≥ 24.9 | `node -v` → `v24.22.2`; `dsh --version` → `0.2.1-rc.3` |
| Service up | `systemctl is-active dsh-superagent` → `active` |
| Panel reachable | `GET /?token=…` → `303` (cookie exchange), then `GET /` → `200` |
| Setup resolved | `GET state` → `setup.problem: null`, `setup.accounts: {repository: pondycrane/LMAO, agent: ecodinquaqua}` |
| Health | every part `ready` — `runtime, relay, notice, launch, delivery, assessment, reply, evals, models, github` |
| GitHub Source polling | `GET sources` → `state: ready`, `0 notifications; next poll ≥ 60s` |
| Local model through the harness | throwaway headless profile with the same `llm-deepseek` + `agent-default-model` rows → `LOCAL-MODEL-OK` |
| Settings persisted | `GET settings`: `mode=observe`, rules `copy/robodog-device/lmao-server`, `allowed=[pondycrane]`, 8 blocked commands, prepare command, prompts rev 7 |
| Prepare command | ran it as `dsh-agent` with the real env: fetched `HEAD → refs/dsh-superagent/smoke-1`, `git worktree add` → branch `agent/smoke-one` at `3c5f5da` (then removed) |
| Worker command shims | `worker-bin/` holds `gh`, `lark-cli` and 8 fail-shims; `esptool`, `mpremote`, `capture.py`, `flash.sh` all answer `… is blocked in this installation`; `gh` resolves to the agent CLI |
| Bench reachable as the agent account | `runuser -u dsh-agent -- bench boards` → `robodog-1 ready claw /dev/bench/robodog-0 head=6d5c9cec` |

### Intake needs an OAuth token, not a fine-grained PAT

The Source reads the agent account's **notifications**. A fine-grained personal
access token cannot (`gh api notifications` → `HTTP 403 Resource not accessible
by personal access token`), so the first attempt left the Source in
`4 consecutive failed polls`. Signing `gh-agent` in through the **device flow**
with the `notifications` scope fixes it — the token now reports
`Token scopes: 'gist', 'notifications', 'read:org', 'repo'` and the poll
returns `200`.

## Agent account — `ecodinquaqua` (distinct from the operator)

The agent is a **separate GitHub user**, `ecodinquaqua`, invited as a `push`
collaborator of `pondycrane/LMAO`; the operator stays the only entry on the
policy's allowed list. A distinct identity is a structural prerequisite, not a
configuration preference:

- the Source **skips the agent account's own messages** (`sameName(comment.user.login, login) → continue`
  in `intakeComments`, and `human: … && !sameName(user.login, agent)` in the
  trigger builder), so a mention posted by the agent identity never becomes a
  trigger;
- with `pondycrane` as both the agent and the only allowed account, every
  mention in this repo was a self-mention and nothing could reach the gate.

A bot actor cannot stand in for that second identity either, which is worth
recording so nobody retries it:

- `github-actions[bot]` is the only bot this repo can produce without a GitHub
  App, and it needs a workflow that comments — but a personal access token
  without the `workflow` scope cannot push `.github/workflows/*`
  (`refusing to allow a Personal Access Token to create or update workflow …`),
  and the deploy key has no access to `pondycrane/LMAO`. A `github-actions[bot]`
  comment would otherwise have been the cheapest reachable trigger (listed as a
  bot in the policy, it becomes an *inferred* candidate, which `observe` assesses
  and records as "would accept").
- A GitHub App installation would post as `<app>[bot]`, but creating an App is an
  account-level change, and the issue rules an App out as the *agent* anyway.

Verified criteria: **1** (boots on Node 24.22, `state` returns `setup` resolved,
panel reachable over a tunnel), **2** (`setup.accounts.agent` →
`ecodinquaqua`, `repos/pondycrane/LMAO` → `permissions.push: true`), **3**
(allowed list + `observe` + three rules), and **4–8** exercised end to end: a
mention from `pondycrane` on PR #311 produced an `explicitRouted` decision and
Ticket `8e569510…` ("Read-only review of PR #311 …") within one poll.

### Host patch: Thread keys with a suffix

A review Thread is keyed `github:<n>-review`, so `Number(thread.ref.id)` is
`NaN` and `pollThread` threw `Subscribed thread has no GitHub issue or PR
number` **every round** — the whole Source sat in `2125 consecutive failed
polls` from 2026-11-09 until the fix, so no intake at all. `threadNumber()` now
reads the leading digits (`Number.parseInt(ref.id, 10)`) in
`src/sources/github.ts` and in the built `lib/github-watch.js`. A suffixed
Thread is then covered by the canonical Thread's read of the same number each
round, so the skip callback also clears a stale `pollError` instead of leaving
`github:<n>-review` stuck at `error` in the panel. The service runs `lib/`, so
**the patch must be re-applied after `pnpm build` or a package upgrade** (the
`src` copy keeps it across rebuilds).

Done on this host (2026-11-16) — recorded so a re-provision follows the same
order:

```bash
BOT=ecodinquaqua

# 1. Agent account created on github.com (email + CAPTCHA, operator step) and
#    invited from the operator account with write access:
gh api -X PUT "repos/pondycrane/LMAO/collaborators/$BOT" -f permission=push

# 2. Interim credentials replaced: the wrapper's config dir holds the agent
#    account ONLY, never the operator's token. Logout is local only —
#    revoke the old OAuth grant at github.com/settings/applications.
sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent auth logout --hostname github.com
sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent auth login \
    --hostname github.com --git-protocol https --skip-ssh-key --web --scopes notifications
sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent api user --jq .login   # → $BOT

# 3. Invitation accepted as the agent; push access confirmed (this is exactly
#    the check the Source runs on save):
ID=$(sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent api \
    user/repository_invitations --jq '.[0].id')
sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent api -X PATCH \
    "user/repository_invitations/$ID"
sudo runuser -u dsh-agent -- /home/dsh-agent/.local/bin/gh-agent api \
    repos/pondycrane/LMAO --jq .permissions.push                            # → true

# 4. GitHub Source saved (setup check passed) and setup confirmed resolved:
python3 tools/dsh_superagent_settings.py
curl -s -H 'x-orchestrator-request: 1' http://127.0.0.1:3082/orchestrator/api/state | jq .setup

# 5. The allowed list stays [pondycrane] — the human, never the agent. Mention
#    the agent from the operator account on an issue or PR:
#    "@$BOT <request>"  → a Ticket appears within one poll (~60 s)
```

An explicit mention from an allowed sender is admitted in **any** accept mode
(`policy.gate`: `allowed && trigger.addressed → strength: explicit`), so the
`observe` setting needs no change for step 5; only rule-gated *inferred*
candidates wait for `auto`. On a PR the ticket's prepare command fetches
`refs/pull/<n>/head`, so the worker starts on the PR branch, not `HEAD`.

## Risks kept open

| Risk | Status here |
|---|---|
| `danger-full-access` | Dedicated OS account with only the agent's credentials; not a sandbox |
| `PATH`/git mutation | Own account + own profile; never co-tenant with the bench |
| **Two autonomous loops** | The Archon workflows already implement → gate → PR. Superagent is installed in **`observe`** and must not be flipped to `auto` while Archon drives the same repo and board — pick one driver first |
| Prompt injection | The gate decides *who may ask*, not what a file says; start in observe and review the recorded Decisions before `auto` |
| No dedupe | Repeated deliveries can create duplicate Tickets |
| Fire-and-forget | A crash loses rule calls that have not admitted a prompt; the unit restarts, but there is no replay |
