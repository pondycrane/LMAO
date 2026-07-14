# Archon GitHub Webhook Relay

Instead of exposing a public webhook endpoint (ngrok / Tailscale Funnel), this machine runs a **local polling relay** that checks GitHub for `@archon` mentions and delivers them to the local Archon server.

## Architecture

```
┌──────────────┐     poll every 15s      ┌──────────────┐
│   GitHub     │◄────────────────────────│   Relay      │
│  API (REST)  │     GET issues/comments  │  (systemd)   │
└──────────────┘                          │              │
        │                                 │  POST signed │
        └───── @archon mention ──────────►│  webhook     │
                                          │  payload     │
                                          └──────┬───────┘
                                                 │
                                          ┌──────▼───────┐
                                          │   Archon     │
                                          │  localhost:   │
                                          │    3090       │
                                          └──────────────┘
```

No public internet exposure required. The relay reads comments via the GitHub REST API, constructs a signed webhook payload matching what GitHub would send, and forwards it to the local Archon server. The relay never exposes a port.

## Components

| Component | Path |
|-----------|------|
| Relay script | `tools/archon_webhook_relay.py` |
| Systemd unit | `~/.config/systemd/user/archon-webhook-relay.service` |
| State file | `~/.archon/relay-state.json` |
| Env vars | `~/.archon/.env` (`GITHUB_TOKEN`, `WEBHOOK_SECRET`) |

## How to use

Comment `@archon` on any issue or PR in `pondycrane/LMAO`:

```
@archon review this PR
@archon can you analyze this bug?
@archon prime the codebase
@archon create follow-up issues
```

The relay picks it up within **15 seconds** and delivers it to Archon. Archon replies as a new comment on the same issue/PR.

## Service management

```bash
# Status
systemctl --user status archon-webhook-relay.service

# Live logs
journalctl --user -u archon-webhook-relay.service -f

# Restart (after editing the script)
systemctl --user restart archon-webhook-relay.service

# Stop
systemctl --user stop archon-webhook-relay.service

# One-shot poll (debug)
cd ~/LMAO && GITHUB_TOKEN=$(grep ^GITHUB_TOKEN ~/.archon/.env | cut -d= -f2) \
  WEBHOOK_SECRET=$(grep ^WEBHOOK_SECRET ~/.archon/.env | cut -d= -f2) \
  python3 tools/archon_webhook_relay.py --once
```

## How it works

1. **Poll** — Every 15 seconds, the relay calls `GET /repos/pondycrane/LMAO/issues/comments?sort=created&direction=desc&per_page=30`
2. **Filter** — Skips comments that don't contain `@archon`, and skips bot-generated comments (long structured output starting with `#`, `##`, `|`)
3. **Fetch context** — For qualifying comments, fetches the parent issue/PR metadata via `GET /repos/pondycrane/LMAO/issues/{number}`
4. **Sign** — Constructs a GitHub-compatible webhook payload and signs it with HMAC-SHA256 using `WEBHOOK_SECRET`
5. **Deliver** — POSTs to `http://localhost:3090/webhooks/github` with proper `X-GitHub-Event`, `X-GitHub-Delivery`, and `X-Hub-Signature-256` headers
6. **Track** — Records processed comment IDs in `~/.archon/relay-state.json` so duplicates are never sent

## State file format

```json
{
  "cursor": 4958822584,
  "processed": [4863525231, ...]
}
```

- `cursor` — highest comment ID seen (advances on every poll, so old comments are never re-scanned)
- `processed` — comment IDs that were successfully delivered (last 500)

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Relay not running | Service stopped or failed | `systemctl --user restart archon-webhook-relay.service` |
| "GITHUB_TOKEN not set" | Env not loaded | Check `~/.archon/.env` has `GITHUB_TOKEN=...` |
| "WEBHOOK_SECRET not set" | Env not loaded | Check `~/.archon/.env` has `WEBHOOK_SECRET=...` |
| Cursor not advancing | GitHub API auth failure | `gh auth status` — check PAT is valid |
| `@archon` not being picked up | Bot comment filter | Check the comment body doesn't start with `#` or `|` |
| Archon not responding | Server not running | `curl http://localhost:3090/api/health` |

## Adding more repos

To monitor additional repos, edit `tools/archon_webhook_relay.py` and change the `REPO` variable — or extend it to support multiple repos. Then restart:
```bash
systemctl --user restart archon-webhook-relay.service
```

## Alternatives (if you need real webhooks)

If you later want real GitHub webhooks (instant delivery, no polling), you'll need a public endpoint:

- **Tailscale Funnel** — `tailscale funnel --bg 3090` (requires plan upgrade, see `tailscale funnel status`)
- **ngrok** — `ngrok http 3090` (ephemeral URLs on free tier)
- **Cloudflare Tunnel** — persistent URL, free

Then configure the webhook in GitHub repo Settings → Webhooks → Add webhook:
- **Payload URL**: `https://your-url/webhooks/github`
- **Content type**: `application/json`
- **Secret**: your `WEBHOOK_SECRET`
- **Events**: Issues, Issue comments, Pull requests