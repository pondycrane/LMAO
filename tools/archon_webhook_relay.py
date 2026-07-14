#!/usr/bin/env python3
"""
Archon GitHub Webhook Polling Relay

Polls the GitHub API for new issue/PR comments mentioning @archon,
constructs valid webhook payloads, and forwards them to the local
Archon server.  No public endpoint needed — the machine stays dark.

Usage:
    python3 archon_webhook_relay.py              # one-shot poll
    python3 archon_webhook_relay.py --daemon     # poll every 15s
    python3 archon_webhook_relay.py --once       # explicit one-shot
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Config ──────────────────────────────────────────────────────────────────

REPO = "pondycrane/LMAO"
ARCHON_WEBHOOK_URL = "http://localhost:3090/webhooks/github"
STATE_FILE = Path.home() / ".archon" / "relay-state.json"
POLL_INTERVAL = 15  # seconds
USER_AGENT = "archon-webhook-relay/1.0"

# Read env
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")


# ── State persistence ──────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"cursor": 0, "processed": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── GitHub API helpers ─────────────────────────────────────────────────────

def gh_api(path: str, method: str = "GET") -> dict | list:
    """Call the GitHub REST API and return parsed JSON."""
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    }
    req = Request(url, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            # Handle paginated responses / next-link
            return json.loads(data)
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        log.warning("GitHub API %s %s: %s %s", method, path, e.code, body[:200])
        raise
    except URLError as e:
        log.warning("GitHub API unreachable: %s", e)
        raise


def get_recent_comments(page: int = 1, per_page: int = 30) -> list[dict]:
    """Fetch recent issue/PR comments."""
    # We use the repo-level issues/comments endpoint which returns
    # comments on BOTH issues and pull requests.
    return gh_api(
        f"/repos/{REPO}/issues/comments"
        f"?sort=created&direction=desc&per_page={per_page}&page={page}"
    )


def get_issue(issue_number: int) -> dict:
    """Fetch issue or PR metadata."""
    return gh_api(f"/repos/{REPO}/issues/{issue_number}")


def get_timeline(issue_number: int) -> list:
    """Fetch issue/PR timeline events."""
    return gh_api(f"/repos/{REPO}/issues/{issue_number}/timeline")


# ── Webhook payload construction ───────────────────────────────────────────

def build_issue_comment_payload(comment: dict) -> tuple[str, bytes]:
    """
    Build a minimal but valid GitHub issue_comment webhook payload.

    Returns (event_type, json_body).
    """
    issue_url = comment.get("issue_url", "")
    # Parse issue number from the issue_url
    # e.g. https://api.github.com/repos/pondycrane/LMAO/issues/42
    issue_number = int(issue_url.rstrip("/").rsplit("/", 1)[-1])

    # Fetch issue metadata for richer context
    issue = get_issue(issue_number)

    # Determine if it's a PR (pull_request key present)
    is_pr = "pull_request" in issue

    payload = {
        "action": "created",
        "issue": {
            "number": issue_number,
            "title": issue.get("title", ""),
            "state": issue.get("state", "open"),
            "body": issue.get("body", ""),
            "user": issue.get("user", {}),
            "labels": issue.get("labels", []),
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
            "html_url": issue.get("html_url", f"https://github.com/{REPO}/issues/{issue_number}"),
        },
        "comment": {
            "id": comment["id"],
            "body": comment.get("body", ""),
            "user": comment.get("user", {}),
            "created_at": comment.get("created_at", ""),
            "updated_at": comment.get("updated_at", ""),
            "html_url": comment.get("html_url", ""),
            "issue_url": issue_url,
        },
        "repository": {
            "full_name": REPO,
            "name": REPO.split("/")[1],
            "owner": {"login": REPO.split("/")[0]},
            "html_url": f"https://github.com/{REPO}",
        },
        "sender": comment.get("user", {}),
    }

    # If it's a PR, add PR-specific fields
    if is_pr:
        payload["issue"]["pull_request"] = {
            "url": issue.get("pull_request", {}).get("url", ""),
            "html_url": issue.get("pull_request", {}).get("html_url",
                       f"https://github.com/{REPO}/pull/{issue_number}"),
        }
        # Also add a minimal pull_request field for PR comment context
        payload["pull_request"] = {
            "number": issue_number,
            "title": issue.get("title", ""),
            "state": issue.get("state", "open"),
            "user": issue.get("user", {}),
            "html_url": issue.get("html_url", f"https://github.com/{REPO}/pull/{issue_number}"),
            "body": issue.get("body", ""),
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
            "head": {"ref": issue.get("head", {}).get("ref", "unknown") if isinstance(issue.get("head"), dict) else "unknown"},
            "base": {"ref": issue.get("base", {}).get("ref", "unknown") if isinstance(issue.get("base"), dict) else "unknown"},
        }

    return "issue_comment", json.dumps(payload, ensure_ascii=False).encode("utf-8")


def sign_payload(secret: str, body: bytes) -> str:
    """Compute HMAC-SHA256 signature (matching GitHub's X-Hub-Signature-256)."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def deliver(event_type: str, body: bytes) -> bool:
    """POST the webhook payload to the local Archon server."""
    sig = sign_payload(WEBHOOK_SECRET, body)
    delivery_id = str(uuid.uuid4())

    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
        "User-Agent": USER_AGENT,
    }

    req = Request(ARCHON_WEBHOOK_URL, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            response = resp.read().decode()
            log.info("Delivered %s event (comment #%s) → %s", event_type, delivery_id[:8], response.strip())
            return True
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        log.warning("Delivery failed: HTTP %s — %s", e.code, body[:200])
        return False
    except URLError as e:
        log.warning("Delivery failed: %s", e)
        return False


# ── Polling logic ──────────────────────────────────────────────────────────

def should_process(comment: dict, state: dict) -> bool:
    """Check if a comment is new, mentions @archon, and hasn't been processed."""
    cid = comment["id"]
    body = comment.get("body", "")
    curs = state.get("cursor", 0)

    # Already processed or behind cursor?
    if cid in state.get("processed", []):
        return False
    if curs != 0 and cid <= curs:
        return False

    # Must mention @archon
    if "@archon" not in body.lower():
        return False

    # Ignore comments by the bot itself (it posts as pondycrane via PAT).
    # Bot comments from Archon tend to be long and structured.
    stripped = body.strip()
    if stripped.startswith("#") or stripped.startswith("|"):
        return False
    if len(body) > 400 and ("##" in body or "# 🔍" in body or "# ⚡" in body):
        return False
    if stripped.startswith("##"):
        return False

    return True


def mark_processed(comment: dict, state: dict):
    """Record a comment as processed."""
    cid = comment["id"]
    state.setdefault("processed", []).append(cid)
    # Keep processed list bounded
    state["processed"] = state["processed"][-500:]
    if cid > state.get("cursor", 0):
        state["cursor"] = cid
    save_state(state)


def poll_once(state: dict) -> int:
    """Single poll cycle. Returns number of comments processed."""
    try:
        comments = get_recent_comments()
    except Exception as e:
        log.warning("Poll failed: %s", e)
        return 0

    if not comments:
        return 0

    # Advance cursor past the highest comment ID seen this cycle
    max_id = max(c["id"] for c in comments)
    if max_id > state.get("cursor", 0):
        state["cursor"] = max_id
        save_state(state)

    processed = 0
    # Process in chronological order (oldest first)
    for comment in reversed(comments):
        if not should_process(comment, state):
            continue
        log.info(
            "New @archon mention: comment #%d on %s",
            comment["id"],
            comment.get("html_url", "unknown")[:60],
        )
        try:
            event_type, body = build_issue_comment_payload(comment)
            if deliver(event_type, body):
                mark_processed(comment, state)
                processed += 1
        except Exception as e:
            log.warning("Failed to process comment #%d: %s", comment["id"], e)

    return processed


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Archon GitHub webhook relay")
    parser.add_argument(
        "--daemon", action="store_true",
        help=f"Run continuously, polling every {POLL_INTERVAL}s",
    )
    parser.add_argument(
        "--once", "--oneshot", action="store_true", dest="once",
        help="Run one poll cycle and exit",
    )
    args = parser.parse_args()

    # Default to daemon mode if no flag given
    daemon_mode = args.daemon or not args.once

    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN or GH_TOKEN not set")
        sys.exit(1)
    if not WEBHOOK_SECRET:
        log.error("WEBHOOK_SECRET not set")
        sys.exit(1)

    state = load_state()
    log.info(
        "Relay started (cursor=%d, processed=%d)",
        state.get("cursor", 0),
        len(state.get("processed", [])),
    )

    if daemon_mode:
        log.info("Daemon mode, polling every %ds", POLL_INTERVAL)
        poll_count = 0
        while True:
            try:
                n = poll_once(state)
                poll_count += 1
                if n:
                    log.info("Processed %d comment(s)", n)
                if poll_count % 20 == 0:  # every ~5 min
                    log.info("Tick: %d polls, cursor=%d, processed=%d",
                             poll_count, state.get("cursor", 0),
                             len(state.get("processed", [])))
            except KeyboardInterrupt:
                log.info("Shutting down")
                break
            except Exception:
                log.exception("Unhandled error in poll loop")
            time.sleep(POLL_INTERVAL)
    else:
        n = poll_once(state)
        if n:
            log.info("Processed %d comment(s)", n)
        else:
            log.info("No new comments to process")


if __name__ == "__main__":
    main()