#!/usr/bin/env python3
"""Detect Turing-Pi LAN addresses hardcoded in runtime paths (drift guard).

Why this exists
---------------
The cluster nodes' `wlan0` interfaces are DHCP.  On 2026-11-02 the leases moved
(tp3 `.90`→`.91`, tp4 `.67`→`.68`) and everything pinned to those addresses
died quietly at once: `kubectl` could not reach the API, `ssh tp4` failed, the
RoboDog calendar panel stopped syncing, and the calendar-sync CronJob kept
failing against a Postgres hostname it could still resolve but whose server was
no longer where the clients looked.

The durable fix is to address nodes by **hostname** (`tp1`…`tp4`), which the
router's DNS resolves for whatever lease DHCP hands out.  This tool fails the
build/CI if an address creeps back into a runtime path.

Usage::
    bazel run //tools:check_node_addresses
    python3 tools/check_node_addresses.py          # from anywhere in the repo

Exit codes: 0 = clean, 1 = hardcoded node address found.

`docs/cluster-setup.md` is the single allowlisted place that may list the
current addresses — as a snapshot, never as a client target.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Turing Pi nodes' DHCP LAN addresses, past and present.  Keep both the old and
# new value so a regression that reintroduces either is caught.
NODE_IPS: dict[str, tuple[str, ...]] = {
    "tp1": ("192.168.50.183",),
    "tp2": ("192.168.50.156", "192.168.50.157"),
    "tp3": ("192.168.50.90", "192.168.50.91"),
    "tp4": ("192.168.50.67", "192.168.50.68"),
}

# Where an address would actually be used at runtime (and thus drift-break).
RUNTIME_DIRS = (
    "lma_core",
    "lmao_server",
    "human_client",
    "tools",
    "k8s",
    "k8s-app",
    "cardputer_client",
    "robodog_client/main",
    "firmware_common",
    "smart_irrigation/native-client/main",
)

RUNTIME_FILES = ("README.md",)

# Build artifacts (gitignored, regenerated) and fetched upstream trees may carry
# a resolved address without it being a source-of-truth regression.
SKIP_DIR_NAMES = {
    ".rtreticulum", "build", "managed_components", ".xiaozhi", ".esp-claw",
    "node_modules", "__pycache__", "shots", ".git",
}

SCAN_EXTS = {
    ".py", ".c", ".h", ".cpp", ".yaml", ".yml", ".sh", ".service",
    ".md", ".txt", ".json",
}

# The detector and its test necessarily contain the addresses they look for.
ALLOWLIST_FILES = {
    "tools/check_node_addresses.py",
    "tests/test_node_address_drift.py",
}


def repo_root() -> str:
    """Workspace root: `bazel run` exports BUILD_WORKSPACE_DIRECTORY."""
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env and os.path.isdir(env):
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def iter_runtime_files(root: str):
    for rel in RUNTIME_DIRS:
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for name in filenames:
                if os.path.splitext(name)[1] in SCAN_EXTS:
                    yield os.path.join(dirpath, name)
    for rel in RUNTIME_FILES:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            yield path


def node_ip_pattern() -> re.Pattern[str]:
    ips = sorted({ip for group in NODE_IPS.values() for ip in group})
    # Whole-token match only (never inside a longer IP).
    return re.compile(r"(?<![\d.])(" + "|".join(re.escape(i) for i in ips) + r")(?![\d.])")


def find_hardcoded_node_ips(root: str | None = None) -> list[tuple[str, int, str, str]]:
    """Return [(relpath, lineno, ip, line)] for every offending occurrence."""
    root = root or repo_root()
    pat = node_ip_pattern()
    hits: list[tuple[str, int, str, str]] = []
    for path in iter_runtime_files(root):
        rel = os.path.relpath(path, root)
        if rel in ALLOWLIST_FILES:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    for m in pat.finditer(line):
                        hits.append((rel, lineno, m.group(1), line.strip()))
        except OSError:
            continue
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="repo root (default: auto)")
    parser.add_argument("--quiet", action="store_true", help="only print on failure")
    args = parser.parse_args(argv)

    root = args.root or repo_root()
    files = list(iter_runtime_files(root))
    hits = find_hardcoded_node_ips(root)

    if not args.quiet:
        print(f"scanned {len(files)} runtime file(s) under {root}")
        for host, ips in NODE_IPS.items():
            print(f"  {host}: {', '.join(ips)} (DHCP — never hardcode)")

    if hits:
        print("\nDRIFT: hardcoded Turing-Pi LAN address(es) in runtime paths:", file=sys.stderr)
        for rel, lineno, ip, line in hits:
            print(f"  {rel}:{lineno}  {ip}  {line}", file=sys.stderr)
        print("\nAddress nodes by hostname (tp1..tp4). See docs/cluster-setup.md"
              " 'Preventing address drift'.", file=sys.stderr)
        return 1

    if not args.quiet:
        print("OK: no hardcoded node addresses in runtime paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
