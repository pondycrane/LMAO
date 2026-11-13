#!/usr/bin/env python3
"""Apply the LMAO dsh-superagent settings (issue #303) through the local Management API.

The panel is loopback-only on the superagent profile's port (default 3082), so
this runs on the host that owns the profile. Every block is read first so the
save carries the current revision; a stale revision is refused, never guessed.

    python3 tools/dsh_superagent_settings.py            # apply
    python3 tools/dsh_superagent_settings.py --check    # print, change nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("DSH_SUPERAGENT_PORT", "3082"))
API = f"http://127.0.0.1:{PORT}/orchestrator/api"

CLONE = os.environ.get("DSH_SUPERAGENT_CLONE", "/home/dsh-agent/src/LMAO")
GH_AGENT = os.environ.get("DSH_SUPERAGENT_GH_AGENT", "/home/dsh-agent/.local/bin/gh-agent")
REPOSITORY = os.environ.get("DSH_SUPERAGENT_REPOSITORY", "pondycrane/LMAO")

# The device contract the issue fixes: the bench owns the board, so a worker
# never builds/flashes/captures locally.
DEVICE_CONTRACT = """\
Verification for RoboDog hardware is the bench, never the worker's host.

- `bench submit build --head <sha> --ref refs/bench/<sha> --class dev` builds the
  firmware; `bench submit flash_capture --head <sha> --build <build id> --class dev`
  flashes and captures the board. `bench wait <job id> --on-artifacts <dir>` pulls
  the frames and transcriptions back; `bench evidence <job id>` renders the verdict.
- `BENCH_URL` is `http://op4:8099` (already in the service environment).
- Pass = the bench verdict. Never run `esptool`, `flash.sh` or `capture.py` locally.
- Exit 75 means the bench is busy: wait and retry. Exit 78 means the board is
  quarantined: report `blocked` and stop — the board needs a physical replug, and
  retrying into it cannot succeed."""

RULES = [
    {
        "id": "copy",
        "name": "Copy fixes",
        "enabled": True,
        "instruction": "Explicit requests to correct a typo, wording, punctuation or existing translation. Preserve product promises and business meaning.",
    },
    {
        "id": "robodog-device",
        "name": "RoboDog device work, verified on the bench",
        "enabled": True,
        "instruction": (
            "Explicit requests to change the RoboDog calendar or voice firmware, its rendering, "
            "or device-side behaviour, where the requested target is specified or an existing "
            "standard/correct state is to be restored. Verification is `bench submit build` then "
            "`bench submit flash_capture`; pass is the bench verdict, and the captured panel "
            "transcription must support it. Never run esptool, flash.sh or capture.py on the "
            "worker host. A visual change without a captured before/after is not ready."
        ),
    },
    {
        "id": "lmao-server",
        "name": "LMAO server and protocol fixes",
        "enabled": True,
        "instruction": (
            "Explicit requests to fix lmao_server/lma_core behaviour (LoRa/LXMF wire path, "
            "calendar bundle, contacts, NATS ingest) with a stated failing behaviour or an "
            "established correct state. Keep the change local to the named module or path; a "
            "transport or schema change needs a design decision first."
        ),
    },
]

# Team skills: the repo's own `.omp/skills/<name>/SKILL.md` bodies (frontmatter
# stripped).  dsh carries skill TEXT, not files, so the body is inlined into the
# prompts config — the same source omp loads, one definition.
SKILL_FILES = {"robodog-ui": "robodog-ui/SKILL.md"}


def team_skills() -> dict:
    root = Path(__file__).resolve().parent.parent / ".omp" / "skills"
    out = {}
    for name, rel in SKILL_FILES.items():
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            # strip the YAML frontmatter: name/description are dsh metadata
            text = text.split("---", 2)[2].lstrip("\n")
        out[name] = text
    return out


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}/{path}",
        data=data,
        method=method,
        headers={"x-orchestrator-request": "1", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} -> HTTP {error.code}: {detail}") from None


def save_block(block: str, value: dict, revision: int) -> dict:
    return call("PUT", f"settings/{block}", {"value": value, "revision": revision})


def prepare_command() -> str:
    return (
        f'git -C {CLONE} fetch --no-tags '
        '"https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" '
        '"${DSH_SUPERAGENT_GITHUB_PR_HEAD:-HEAD}:refs/dsh-superagent/$DSH_SUPERAGENT_TICKET" && '
        f'git -C {CLONE} worktree add -b "agent/$DSH_SUPERAGENT_NAME" '
        '"$DSH_SUPERAGENT_DIR" "refs/dsh-superagent/$DSH_SUPERAGENT_TICKET"'
    )


def desired() -> dict:
    settings = call("GET", "settings")
    prompts = call("GET", "prompts")
    defaults = prompts["defaults"]

    policy = dict(settings["blocks"]["policy"]["value"])
    policy["rules"] = RULES
    sources = dict(policy.get("sources") or {})
    github = dict(sources.get("github") or {})
    github["allowed"] = [{"id": "pondycrane"}]
    sources["github"] = github
    policy["sources"] = sources

    executor = dict(settings["blocks"]["executor"]["value"])
    executor["prepare"] = prepare_command()
    executor["blockedCommands"] = [
        "esptool", "esptool.py", "espsecure", "espsecure.py",
        "espefuse", "espefuse.py", "mpremote", "capture.py", "flash.sh",
    ]
    executor["maxWorkers"] = 2

    eval_block = dict(settings["blocks"]["eval"]["value"])
    eval_block["mode"] = "observe"

    source_github = next(s for s in settings["sources"] if s["id"] == "github")
    github_config = dict(source_github["value"]["config"])
    github_config.update({"repository": REPOSITORY, "cli": GH_AGENT})
    # Intake. The default is mentions-only (the agent account's notifications),
    # which is what the issue asks for. `DSH_SUPERAGENT_OBSERVE_REPO=1` also
    # receives every new Issue/PR/comment in the repository; the admission
    # table still decides what becomes a candidate, so it widens observation,
    # not authority.
    if os.environ.get("DSH_SUPERAGENT_OBSERVE_REPO", "0") == "1":
        github_config.update({"observeIssues": True, "observePulls": True, "observeComments": True})
    source_value = {"enabled": True, "config": github_config}

    config = {
        "communication": defaults["communication"],
        "scope": defaults["scope"],
        "delivery": defaults["delivery"] + "\n\n" + DEVICE_CONTRACT + "\n",
        "skills": team_skills(),
    }

    return {
        "revisions": {
            "policy": settings["blocks"]["policy"]["revision"],
            "eval": settings["blocks"]["eval"]["revision"],
            "executor": settings["blocks"]["executor"]["revision"],
            "source:github": source_github["revision"],
            "prompts": prompts["current"]["revision"],
        },
        "policy": policy,
        "eval": eval_block,
        "executor": executor,
        "source:github": source_value,
        "prompts": config,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="print the desired blocks and exit")
    args = parser.parse_args()

    plan = desired()
    if args.check:
        print(json.dumps(plan, indent=1))
        return 0

    revisions = plan["revisions"]
    for block in ("policy", "eval", "executor"):
        result = save_block(block, plan[block], revisions[block])
        print(f"{block}: saved rev {result.get('saved')} restarting={result.get('restarting')}")

    result = call("PUT", "prompts", {"config": plan["prompts"], "baseRevision": revisions["prompts"]})
    print(f"prompts: saved rev {result.get('revision')}")

    # The GitHub Source runs the setup check (agent account, push access) on save.
    try:
        result = save_block("source:github", plan["source:github"], revisions["source:github"])
        print(f"source:github: saved rev {result.get('saved')} restarting={result.get('restarting')}")
    except SystemExit as error:
        print(f"source:github: REFUSED — {error}", file=sys.stderr)
        print(
            "The GitHub Source needs a signed-in agent account with push access "
            f"({GH_AGENT} auth login). Everything else was saved.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
