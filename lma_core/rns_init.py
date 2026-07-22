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

from lma_core.rns_di import LXMF, RNS

logger = logging.getLogger(__name__)


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
