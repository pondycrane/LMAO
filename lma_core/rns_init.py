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
        logger.critical("Failed to create config directory for Reticulum: %s", e, exc_info=True)
        print(
            f"FATAL: Failed to create config directory for Reticulum: {e}",
            file=sys.stderr,
        )
        print("Check that /tmp is writable and disk is not full.", file=sys.stderr)
        sys.exit(1)
    except RNS.RNSException as e:
        logger.critical("Reticulum initialization failed: %s", e, exc_info=True)
        print(f"FATAL: Reticulum initialization failed: {e}", file=sys.stderr)
        if rnode_exists:
            print(f"This is often caused by a missing or misconfigured RNode on {rnode_port}.")
            print("Check that:")
            print(f"  1. The RNode is plugged in and on the correct port ({rnode_port})")
            print("  2. You have permission: sudo usermod -a -G dialout $USER")
            print("  3. The RNode firmware is flashed correctly")
            print("  See rnode_firmware/README.md and README Troubleshooting.")
        sys.exit(1)
    except Exception as e:
        logger.critical("Failed to initialize Reticulum: %s", e, exc_info=True)
        print(f"FATAL: Failed to initialize Reticulum: {e}", file=sys.stderr)
        print(
            "Check your config and RNode connection. See README Troubleshooting.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Reticulum initialized.")

    # Create identity
    try:
        identity = RNS.Identity()
    except (RNS.RNSException, OSError) as e:
        logger.critical("Failed to create identity: %s", e, exc_info=True)
        print(
            "FATAL: Failed to create identity. See log for details.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create LXMF router
    print("Starting LXMF router...")
    try:
        router = LXMF.LXMRouter(identity=identity, storagepath=identity_storage_path)
    except (RNS.RNSException, LXMF.LXMFException, OSError) as e:
        logger.critical("Failed to start LXMF router: %s", e, exc_info=True)
        print("FATAL: Failed to start LXMF router. See log for details.", file=sys.stderr)
        sys.exit(1)

    # Register delivery callback if provided
    if register_delivery_callback is not None:
        register_delivery_callback(router)

    return identity, router
