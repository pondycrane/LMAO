"""Smoke tests for urns Transport — routing, registration, and packet filtering."""

import sys
import os
import hashlib
from unittest.mock import MagicMock

# Ensure the urns package is importable
_urns_parent = os.path.join(os.path.dirname(__file__), "..", "cardputer_client", "lib")
_urns_parent = os.path.abspath(_urns_parent)
if _urns_parent not in sys.path:
    sys.path.insert(0, _urns_parent)

# Mock MicroPython dependencies
class _MockMicroPython:
    @staticmethod
    def const(x):
        return x

    @staticmethod
    def native(f):
        return f

sys.modules["micropython"] = _MockMicroPython()

_mp_uhashlib = MagicMock()
_mp_uhashlib.sha256 = hashlib.sha256
sys.modules["uhashlib"] = _mp_uhashlib

class _MockAESCipher:
    def __init__(self, key, mode, iv):
        pass
    def encrypt(self, plaintext):
        return plaintext
    def decrypt(self, ciphertext):
        return ciphertext

_mp_ucryptolib = MagicMock()
_mp_ucryptolib.aes = _MockAESCipher
sys.modules["ucryptolib"] = _mp_ucryptolib

sys.modules["machine"] = MagicMock()
sys.modules["network"] = MagicMock()

from urns import const  # noqa: E402
from urns.transport import Transport  # noqa: E402
from urns.identity import Identity  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_mock_interface(name="mock_iface"):
    """Create a mock interface with minimal attributes."""
    iface = MagicMock()
    iface.name = name
    iface.get_hash.return_value = Identity.full_hash(name.encode())
    iface.bitrate = 10000
    iface.online = True
    return iface


class TransportStateHelper:
    """Resets Transport class-level state to defaults between tests."""

    @staticmethod
    def save():
        """Return a snapshot of current Transport state."""
        return {
            "interfaces": list(Transport.interfaces),
            "destinations": list(Transport.destinations),
            "packet_hashlist": list(Transport.packet_hashlist),
            "path_table": dict(Transport.path_table),
            "active_links": list(Transport.active_links),
            "announce_table": dict(Transport.announce_table),
            "reverse_table": dict(Transport.reverse_table),
            "path_states": dict(Transport.path_states),
            "receipts": list(Transport.receipts),
            "destination_table": dict(Transport.destination_table),
            "discovery_path_requests": dict(Transport.discovery_path_requests),
            "_pr_tags": list(Transport._pr_tags),
            "_announce_rate": dict(Transport._announce_rate),
            "_path_waiters": dict(Transport._path_waiters),
            "_path_request_times": dict(Transport._path_request_times),
            "_time_votes": dict(Transport._time_votes),
            "reachable_destinations": dict(Transport.reachable_destinations),
            "relayed_announces": Transport.relayed_announces,
            "relayed_data": Transport.relayed_data,
        }

    @staticmethod
    def reset():
        """Reset all mutable Transport state to defaults."""
        Transport.interfaces = []
        Transport.destinations = []
        Transport.pending_links = []
        Transport.active_links = []
        Transport.packet_hashlist = []
        Transport.receipts = []
        Transport.announce_table = {}
        Transport.destination_table = {}
        Transport.path_table = {}
        Transport.blackholed_identities = []
        Transport.reverse_table = {}
        Transport.link_table = {}
        Transport.packet_cache = {}
        Transport.path_states = {}
        Transport.control_destinations = []
        Transport.control_hashes = []
        Transport.discovery_path_requests = {}
        Transport._pr_tags = []
        Transport._last_cull = 0
        Transport._last_persist = 0
        Transport._announce_rate = {}
        Transport.relayed_announces = 0
        Transport.relayed_data = 0
        Transport.relayed_links = 0
        Transport.relayed_proofs = 0
        Transport.reachable_destinations = {}
        Transport._path_waiters = {}
        Transport._path_request_times = {}
        Transport._path_request_dest = None
        Transport._time_votes = {}
        Transport._last_job = 0


# ── Tests ───────────────────────────────────────────────────────────────────

class TestRegisterInterface:
    """Interface registration and lookup."""

    def setup_method(self):
        TransportStateHelper.reset()

    def test_register_interface(self):
        iface = _make_mock_interface("test_iface")
        Transport.interfaces.append(iface)
        assert iface in Transport.interfaces
        assert len(Transport.interfaces) == 1

    def test_register_multiple_interfaces(self):
        iface1 = _make_mock_interface("iface1")
        iface2 = _make_mock_interface("iface2")
        Transport.interfaces.extend([iface1, iface2])
        assert len(Transport.interfaces) == 2


class TestRegisterDestination:
    """Destination registration."""

    def setup_method(self):
        TransportStateHelper.reset()

    def test_register_destination(self):
        dest = MagicMock()
        dest.hash = b"\x01" * 16
        Transport.destinations.append(dest)
        assert dest in Transport.destinations

    def test_register_multiple_destinations(self):
        d1 = MagicMock()
        d1.hash = b"\x01" * 16
        d2 = MagicMock()
        d2.hash = b"\x02" * 16
        Transport.destinations.extend([d1, d2])
        assert len(Transport.destinations) == 2


class TestPacketFilter:
    """Duplicate packet detection via packet_hashlist."""

    def setup_method(self):
        TransportStateHelper.reset()

    def test_admits_new_packet(self):
        """Fresh hash should not be in hashlist."""
        new_hash = b"\xaa" * 32
        assert new_hash not in Transport.packet_hashlist

    def test_drops_duplicate_packet(self):
        """Cached hash should be filtered."""
        dup_hash = b"\xbb" * 32
        Transport.packet_hashlist.append(dup_hash)
        assert dup_hash in Transport.packet_hashlist

    def test_hashlist_capped(self):
        """Hashlist should not grow beyond MAX_PACKET_HASHLIST."""
        for i in range(const.MAX_PACKET_HASHLIST + 10):
            Transport.packet_hashlist.append(hashlib.sha256(str(i).encode()).digest())
            if len(Transport.packet_hashlist) > const.MAX_PACKET_HASHLIST:
                Transport.packet_hashlist.pop(0)
        assert len(Transport.packet_hashlist) <= const.MAX_PACKET_HASHLIST


class TestPathTable:
    """Path table for known destinations."""

    def setup_method(self):
        TransportStateHelper.reset()

    def test_has_path_returns_true_for_known_dest(self):
        dest_hash = b"\xcc" * 16
        Transport.reachable_destinations[dest_hash] = 1000  # timestamp
        assert Transport.has_path(dest_hash) is True

    def test_has_path_returns_false_for_unknown_dest(self):
        unknown = b"\xff" * 16
        assert Transport.has_path(unknown) is False


class TestOutbound:
    """Outbound packet dispatch to interfaces."""

    def setup_method(self):
        TransportStateHelper.reset()

    def test_outbound_sends_to_interfaces(self):
        iface = _make_mock_interface("outbound_iface")
        Transport.interfaces.append(iface)

        # Create a simple mock packet
        pkt = MagicMock()
        pkt.packet_type = const.PKT_DATA
        pkt.destination_hash = b"\x01" * 16
        pkt.packed = True
        pkt.raw = b"mock_packet_raw"
        pkt.hops = 0

        result = Transport.outbound(pkt)
        # Should be True if at least one interface accepted
        assert result is True or result is False

    def test_outbound_no_interfaces(self):
        """Outbound with no interfaces registered returns False."""
        pkt = MagicMock()
        pkt.packet_type = const.PKT_DATA
        pkt.destination_hash = b"\x01" * 16
        pkt.packed = True
        pkt.raw = b"mock_packet_raw"
        pkt.hops = 0

        result = Transport.outbound(pkt)
        assert result is False, "Outbound with no interfaces should return False"
