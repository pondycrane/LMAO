"""Guard: no hardcoded Turing-Pi LAN addresses in runtime paths.

The nodes' `wlan0` interfaces are DHCP.  On 2026-11-02 the leases moved
(tp3 .90->.91, tp4 .67->.68) and every consumer pinned to those addresses went
quietly dead: `kubectl` could not reach the API, `ssh tp4` failed, the RoboDog
panel stopped syncing, and the calendar-sync CronJob kept failing.

Addressing by hostname (`tp4`, `tp3`) is immune to that, because the router's
DNS follows the lease.  These tests fail if a node's LAN address creeps back
into code, manifests, or a client default.

The scan itself lives in ``tools/check_node_addresses.py`` so the same logic
backs both ``bazel run //tools:check_node_addresses`` and this test.
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import check_node_addresses as cna  # noqa: E402


def _require_source_tree():
    """The scan needs the workspace tree; Bazel runfiles carry only deps.

    Skip loudly rather than pass vacuously — a guard that scans zero files
    proves nothing.  CI runs ``bazel run //tools:check_node_addresses`` (which
    has BUILD_WORKSPACE_DIRECTORY) as the enforced path.
    """
    scanned = list(cna.iter_runtime_files(REPO))
    if len(scanned) < 20:
        pytest.skip(f"workspace tree not visible ({len(scanned)} files found) — "
                    "use `bazel run //tools:check_node_addresses`")
    return scanned


def test_no_node_lan_address_in_runtime_paths():
    _require_source_tree()
    hits = cna.find_hardcoded_node_ips(REPO)
    assert not hits, (
        "Hardcoded Turing-Pi LAN address(es) found in runtime paths; address "
        "nodes by hostname (tp1..tp4) instead — wlan0 is DHCP and the lease "
        "has moved before:\n"
        + "\n".join(f"  {p}:{n}  {ip}  {line}" for p, n, ip, line in hits)
    )


def test_node_ip_pattern_matches_only_whole_addresses():
    pat = cna.node_ip_pattern()
    assert pat.search("host=192.168.50.67")
    assert pat.search("192.168.50.68:50051")
    # Must not match a longer address that merely starts the same way.
    assert not pat.search("192.168.50.670")
    assert not pat.search("10.192.168.50.67")
    # A non-node address is fine (selfhost/registry is static).
    assert not pat.search("192.168.50.153:5000")


def test_docs_snapshot_still_documents_the_nodes():
    """The one allowlisted place keeps a human-readable address snapshot."""
    path = os.path.join(REPO, "docs", "cluster-setup.md")
    if not os.path.isfile(path):
        pytest.skip("docs not visible in this test environment")
    with open(path, encoding="utf-8") as f:
        doc = f.read()
    for host in cna.NODE_IPS:
        assert f"`{host}`" in doc, f"{host} missing from docs/cluster-setup.md"
    assert "DHCP" in doc
    assert "hostname" in doc.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
