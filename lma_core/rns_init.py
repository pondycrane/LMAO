"""
Shared Reticulum + LXMF bootstrap logic used by both lmao_server and human_client.

Provides a single source of truth for:
  - RNode port warning
  - Reticulum initialisation with config directory management
  - Identity creation
  - LXMF router startup with optional delivery-callback registration
"""

import atexit
import logging
import os
import shutil
import sys
import tempfile
import threading

from lma_core.rns_di import LXMF, RNS

logger = logging.getLogger(__name__)


# RNS ships path requests for a TRANSPORT-enabled node's control destinations,
# but a leaf node (transport disabled — which this server is) drops inbound
# DATA to rnstransport.path.request at the `transport_enabled() or
# for_local_client ...` gate before its callback runs, so it can never answer.
# Wrap Transport.inbound to deliver control-destination DATA (leaf-gap fix,
# issue #135) so the server can answer Sprout path requests with a
# PATH_RESPONSE announce even with periodic announces off.  Strictly
# additive: anything that is not a control-destination DATA packet falls
# through to the original inbound untouched.
_PATCHED_INBOUND_SENTINEL = "_lmao_patched_inbound"

# A half-duplex requester cannot receive a reply sent during its own TX->RX
# turnaround, so the PATH_RESPONSE answer is deferred by this many seconds
# (issue #142).  Test hook: LMAO_PATH_REQ_ANSWER_DELAY overrides it.
_PATH_REQUEST_ANSWER_DELAY = 1.0


def _patch_leaf_node_path_request_delivery():
    """Install the leaf-node path-request delivery wrap on RNS.Transport.inbound.

    Idempotent.  No-op (silently) if the RNS API moved.
    """
    try:
        # RNS.Transport is lazily usable once Reticulum() has started.
        import RNS  # noqa: F811  (the local import keeps this decoupled)

        Transport = RNS.Transport
        if getattr(Transport, _PATCHED_INBOUND_SENTINEL, False):
            return
        orig_inbound = Transport.inbound
        control_hashes = None
        control_destinations = None

        # Cache the attribute lookups on first inbound use (Transport.start()
        # populates them; Reticulum() has started by the time we install this).
        def _resolve_resources():
            nonlocal control_hashes, control_destinations
            if control_hashes is None:
                control_hashes = Transport.control_hashes
                control_destinations = Transport.control_destinations
            return control_hashes, control_destinations

        def wrapped(raw, interface=None):
            try:
                if not (isinstance(raw, bytes) and len(raw) > 18):
                    orig_inbound(raw, interface)
                    return
                hashes, dests = _resolve_resources()
                dst = raw[2:18] if not (raw[0] & 0x40) else (raw[18:34] if len(raw) > 34 else None)
                if dst is None or dst not in hashes:
                    orig_inbound(raw, interface)
                    return
                # Control-destination DATA — deliver to its callback the same
                # way RNS would (Packet + unpack + filter + receive).
                hexd = dst.hex()
                pkt = RNS.Packet(None, raw)
                if not pkt.unpack():
                    logger.warning("path-req patch: unpack failed for %s", hexd)
                    orig_inbound(raw, interface)
                    return
                if pkt.packet_type != RNS.Packet.DATA:
                    orig_inbound(raw, interface)
                    return
                pkt.receiving_interface = interface
                pkt.hops += 1
                if not Transport.packet_filter(pkt):
                    orig_inbound(raw, interface)
                    return
                RNS.Transport.add_packet_hash(pkt.packet_hash)
                for d in dests:
                    if d.hash == pkt.destination_hash:
                        cb = getattr(getattr(d, "callbacks", None), "packet", None)
                        if cb is None:
                            logger.warning("path-req patch: control dest %s has no packet callback", hexd)
                        else:
                            logger.info("path-req patch: control-dest DATA %s delivered -> answered", hexd)
                            # Defer the answer slightly.  The requester is a
                            # half-duplex node that has *just* transmitted this
                            # request; an answer arriving ~2ms later lands in its
                            # TX->RX turnaround window and is missed entirely,
                            # leaving it unlearned (issue #142).  Answering a
                            # second later puts the PATH_RESPONSE announce
                            # outside that window.  Override (tests) with
                            # LMAO_PATH_REQ_ANSWER_DELAY; 0 = answer inline.
                            _delay = float(
                                os.environ.get("LMAO_PATH_REQ_ANSWER_DELAY", "")
                                or _PATH_REQUEST_ANSWER_DELAY
                            )
                            if _delay > 0:
                                timer = threading.Timer(_delay, cb, args=(pkt.data, pkt))
                                timer.daemon = True
                                logger.debug("path-req patch: deferring answer by %.2fs", _delay)
                                timer.start()
                            else:
                                cb(pkt.data, pkt)   # == Destination.receive for a PLAIN dest (no decrypt)
                        break
                return  # consumed: original inbound would drop it anyway
            except Exception as exc:
                logger.warning("path-req patch error: %r", exc, exc_info=True)
            orig_inbound(raw, interface)

        Transport.inbound = staticmethod(wrapped)
        setattr(Transport, _PATCHED_INBOUND_SENTINEL, True)
        logger.info("Installed leaf-node path-request delivery patch (issue #135)")
    except Exception as exc:
        logger.warning("Could not install path-request delivery patch: %s", exc)


def _is_temp_configdir(configdir):
    """True when *configdir* is a throwaway dir under the system temp root.

    Persistent Reticulum state dirs (e.g. ``/data/transport`` on the K8s
    PVC, via ``LMAO_RNS_TRANSPORT_PATH``) live elsewhere and must NOT be
    deleted at exit (issue #93).
    """
    temp_root = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(configdir).startswith(temp_root + os.sep)


def _fatal(msg, *, extra=None):
    """Log a critical error, print to stderr, and exit with code 1."""
    logger.critical(msg, exc_info=True)
    print(f"FATAL: {msg}", file=sys.stderr)
    if extra:
        print(extra, file=sys.stderr)
    sys.exit(1)


def warn_if_rnode_missing(rnode_port, role="node"):
    """Warn if the RNode port does not exist."""
    if os.path.exists(rnode_port):
        return
    logger.warning("RNode port %s not found. LoRa messaging will be unavailable.", rnode_port)
    print(
        f"\u26a0\ufe0f  RNode port {rnode_port} not found.\n"
        f"   The {role} will start with WiFi AutoInterface only.\n"
        f"   Set the LMAO_RNODE_PORT environment variable if your RNode is on a different port.\n"
        f"   Example: LMAO_RNODE_PORT=/dev/ttyACM0 python3 server.py\n"
        f"   LoRa messaging will be unavailable until an RNode is connected.\n"
    )


def init_rns_and_lxmf(
    *,
    rnode_port,
    configdir_factory,
    identity_storage_path,
    display_name=None,
    atexit_register=None,
    register_delivery_callback=None,
    rnode_exists=True,
):
    """Bootstrap Reticulum + LXMF. Returns (identity, router).

    Args:
        rnode_port: Path to the RNode device (e.g. /dev/ttyUSB0).
        configdir_factory: Callable → config directory path.
        identity_storage_path: Storage path for LXMF router.
        atexit_register: If provided, called with (configdir) to register
            cleanup handler.  ``None`` (default) uses ``atexit.register``.
        register_delivery_callback: If provided, called with (router) to
            register the LXMF delivery callback.
        rnode_exists: Whether the RNode port exists.  Controls whether
            RNode-specific troubleshooting advice is printed on errors.

    Calls ``sys.exit(1)`` on any unrecoverable error — does not return.
    """
    print("Initializing Reticulum...")
    try:
        configdir = configdir_factory()
        if _is_temp_configdir(configdir):
            # Only auto-delete throwaway temp configdirs.  A persistent
            # configdir (LMAO_RNS_TRANSPORT_PATH, e.g. /data/transport on
            # the K8s PVC) holds RNS state that must survive restarts
            # (issue #93).
            if atexit_register is not None:
                atexit_register(lambda: shutil.rmtree(configdir, ignore_errors=True))
            else:
                atexit.register(lambda: shutil.rmtree(configdir, ignore_errors=True))
        RNS.Reticulum(configdir=configdir)
    except (OSError, PermissionError) as e:
        _fatal(
            f"Failed to create config directory for Reticulum: {e}",
            extra="Check that /tmp is writable and disk is not full.",
        )
    except (ValueError, KeyError, IOError, OSError) as e:
        msg = f"Reticulum initialization failed: {e}"
        extra = None
        if rnode_exists:
            extra = (
                f"This is often caused by a missing or misconfigured RNode on {rnode_port}.\n"
                "Check that:\n"
                f"  1. The RNode is plugged in and on the correct port ({rnode_port})\n"
                "  2. You have permission: sudo usermod -a -G dialout $USER\n"
                "  3. The RNode firmware is flashed correctly\n"
                "  See rnode_firmware/README.md and README Troubleshooting."
            )
        _fatal(msg, extra=extra)
    except Exception as e:
        _fatal(
            f"Failed to initialize Reticulum: {e}",
            extra="Check your config and RNode connection. See README Troubleshooting.",
        )
    print("Reticulum initialized.")

    # Leaf-node path-request delivery (issue #135): allow the server to answer
    # on-demand path requests even with transport disabled.
    _patch_leaf_node_path_request_delivery()

    # Load or create identity.  Persisting the identity is critical:
    # clients (Cardputer) bake the server's lxmf.delivery destination hash
    # into their config, and that hash is derived from the identity.  A
    # fresh identity on every boot silently breaks all client configs.
    identity_file = os.path.join(identity_storage_path, "identity")
    identity = None
    if os.path.isfile(identity_file):
        try:
            identity = RNS.Identity.from_file(identity_file)
            if identity is not None:
                print(f"Loaded identity from {identity_file}")
        except (ValueError, KeyError, IOError, OSError) as e:
            print(f"WARNING: could not load identity from {identity_file}: {e}")
            identity = None
    if identity is None:
        try:
            identity = RNS.Identity()
            os.makedirs(identity_storage_path, exist_ok=True)
            identity.to_file(identity_file)
            print(f"Created new identity, saved to {identity_file}")
        except (ValueError, KeyError, IOError, OSError) as e:
            _fatal(f"Failed to create or persist identity: {e}")

    # Create LXMF router
    print("Starting LXMF router...")
    try:
        router = LXMF.LXMRouter(identity=identity, storagepath=identity_storage_path)
    except (ValueError, KeyError, IOError, OSError):
        _fatal("Failed to start LXMF router. See log for details.")

    # Register delivery identity (required for LXMF to receive incoming messages)
    if display_name:
        router.register_delivery_identity(identity, display_name=display_name)

    # Register delivery callback if provided
    if register_delivery_callback is not None:
        register_delivery_callback(router)

    return identity, router
