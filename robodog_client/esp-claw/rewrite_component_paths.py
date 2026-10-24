#!/usr/bin/env python3
"""Rewrite relative `path:` deps in esp-claw idf_component.yml manifests to
ABSOLUTE paths.

Why: the idf-component-manager (ESP-IDF 5.5.x, verified 2.3.0) records root
local-source requirements keyed by their raw `path:` string.  When one local
component is both self-listed in the build (e.g. claw_core at
components/claw_modules/claw_core) AND referenced by a sibling manifest with a
relative `path` (e.g. claw_core's own `claw_utils: ../claw_utils`), the raw
relative string hashes differently from the self-root absolute path and the
solver aborts with "Already defined root local requirement".

Rewriting every relative `path:` to its absolute location (resolved from the
declaring manifest's dir — the semantically correct target) makes the raw
`path:` string equal for every reference to the same component, so the solver
sees one canonical root.  Runs once per fetch inside the container (where the
tree is mounted at /workspace) before bmgr/build; application is idempotent
(already-absolute paths are left alone).

Usage: rewrite_component_paths.py <repo_root>
"""
import os
import re
import sys

REL_PATH_RE = re.compile(r"^(\s*path:\s*)(\S+)\s*$")
REL_PREFIX = "../"


def fetch_root() -> str:
    return os.path.abspath(sys.argv[1])


def rewrite(repo_root: str) -> int:
    changed = 0
    for dirpath, _dirs, files in os.walk(repo_root):
        if any(part in ("managed_components", ".git", "build") for part in dirpath.split(os.sep)):
            continue
        if "idf_component.yml" not in files:
            continue
        manifest = os.path.join(dirpath, "idf_component.yml")
        with open(manifest, encoding="utf-8") as f:
            lines = f.read().splitlines()
        out = []
        for line in lines:
            m = REL_PATH_RE.match(line)
            if m and m.group(2).startswith(REL_PREFIX):
                rel = m.group(2)
                target = os.path.normpath(os.path.join(dirpath, rel))
                out.append(f"{m.group(1)}{target}")
                changed += 1
            else:
                out.append(line)
        with open(manifest, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: rewrite_component_paths.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    n = rewrite(fetch_root())
    print(f"rewrote {n} relative path deps to absolute paths")
