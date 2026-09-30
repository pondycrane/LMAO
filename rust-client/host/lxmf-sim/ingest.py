#!/usr/bin/env python3
"""Offline "would the server accept this" simulation for the Rust Cardputer
client's wire packets (the L1/L2 verification harness).

Feeds Rust-built packets through a stock Python RNS 1.3.5 + LXMF 1.0.1 node
carrying the REAL server identity (~/.local/share/lmao_server/lxmf/identity),
exactly as the production RNode interface would deliver them, and reports
whether the LXMF delivery callback fires — i.e. whether the server would log
"Message received — From: 99ce32…".

Usage:
    python3 -m venv /tmp/lxmf-venv && /tmp/lxmf-venv/bin/pip install "rns==1.3.5" "lxmf==1.0.1"
    MSG_PACKET_HEX=<hex of the full RNS DATA packet> \
    ANNOUNCE_HEX=<hex of the lxmf.delivery announce packet> \
        /tmp/lxmf-venv/bin/python rust-client/host/lxmf-sim/ingest.py

Exit 0 = the server would accept the message (announce valid + delivery
callback fired); exit 1 otherwise. The Rust-side packet bytes come from the
crates/leaf-lxmf tests (L1) or any byte-exact copy of the firmware builder.

Caveats that matter (learned 2026-09-29):
  * The router must `register_delivery_identity(...)` like the production
    server does (lma_core/rns_init.py) or the delivery destination is not in
    Transport.destinations_map and DATA packets drop silently.
  * `Transport.inbound` needs an interface object with the ingress-control
    attributes present; the stub below is enough.
"""
import os
import sys
import tempfile
import time

import LXMF
import RNS

MSG_PACKET_HEX = os.environ["MSG_PACKET_HEX"]
ANNOUNCE_HEX = os.environ["ANNOUNCE_HEX"]

RNS.loglevel = RNS.LOG_EXTREME

cfgdir = tempfile.mkdtemp()
os.makedirs(os.path.join(cfgdir, "storage"), exist_ok=True)
with open(os.path.join(cfgdir, "config"), "w") as f:
    f.write("[reticulum]\nenable_transport = No\nshare_instance = No\n\n[interfaces]\n")

r = RNS.Reticulum(configdir=cfgdir, logdest=RNS.LOG_STDOUT)

# Real server identity (64 B private key file — never commit this key).
idpath = os.path.expanduser("~/.local/share/lmao_server/lxmf/identity")
priv = open(idpath, "rb").read()
server_id = RNS.Identity(create_keys=False)
assert server_id.load_private_key(priv), "server identity load failed"
print("server identity hash:", RNS.hexrep(server_id.hash, delimit=False))

router = LXMF.LXMRouter(identity=server_id, storagepath=os.path.join(cfgdir, "storage"))
# The production server registers the delivery identity (lma_core/rns_init.py).
router.register_delivery_identity(server_id, display_name="LMAO Server")

got = []


def cb(msg):
    src = msg.get_source()
    got.append(msg)
    print(
        "!!! DELIVERY CALLBACK FIRED — From:",
        RNS.hexrep(src.hash, delimit=False) if src else "?",
        " Title:",
        msg.title_as_string(),
        " Content length:",
        len(msg.content),
    )


router.register_delivery_callback(cb)

# Stand-in for the RNode interface: base Interface with ingress hooks off.
iface = RNS.Interfaces.Interface.Interface()
iface.online = True
iface.IN = True
iface.OUT = True
iface.name = "SimRNode"
iface.bitrate = 0
iface.mode = RNS.Interfaces.Interface.Interface.MODE_FULL
iface.ifac_size = 0
iface.announce_rate_target = None
iface.announce_rate_grace = 0
iface.announce_rate_penalty = 0
iface.announce_cap = 2.0
iface.held_announces = {}
iface.ingress_control = False
RNS.Transport.interfaces.append(iface)

print("=== feeding Rust announce (lxmf.delivery) ===")
RNS.Transport.inbound(bytes.fromhex(ANNOUNCE_HEX), iface)
time.sleep(0.5)

client_delivery = bytes.fromhex("99ce32311dc37193eff4951a912f8f1b")
recalled = RNS.Identity.recall(client_delivery)
print(
    "Identity.recall(99ce32…) ->",
    RNS.hexrep(recalled.hash, delimit=False) if recalled else None,
)
print("path known:", RNS.Transport.has_path(client_delivery))

print("=== feeding Rust LXMF DATA packet ===")
RNS.Transport.inbound(bytes.fromhex(MSG_PACKET_HEX), iface)
for _ in range(40):
    if got:
        break
    time.sleep(0.25)

print(
    "RESULT:",
    "PASS — server would accept the Rust message" if got else "FAIL — packet silently dropped before delivery",
)
sys.exit(0 if got else 1)
