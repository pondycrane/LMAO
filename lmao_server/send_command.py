"""
send_command — CLI tool for sending LMAO CommandRequest messages over LXMF.

Usage:
    bazel run //lmao_server:send_command -- --target <hex> --action reboot
    bazel run //lmao_server:send_command -- --target <hex> --action reboot --params '{"key":"val"}'
    bazel run //lmao_server:send_command -- --target <hex> --action reboot --rnode-port /dev/ttyUSB0

This tool initialises Reticulum + LXMF, sends a CommandRequest to the
target node, and exits.  It is the server-side counterpart to the
Cardputer client's CommandRequest handler (issue #78).
"""

import argparse
import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Send a CommandRequest to a Cardputer node over LXMF.",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Target node identity hex (required). Use the node's identity hash "
        "(the value printed at boot as 'ID: a1b2c3...').",
    )
    parser.add_argument(
        "--action",
        default="reboot",
        help="Command action (default: reboot).",
    )
    parser.add_argument(
        "--params",
        default=None,
        help="Optional JSON dict of string→string parameters (e.g. '{\"key\":\"val\"}').",
    )
    parser.add_argument(
        "--rnode-port",
        default=os.environ.get("LMAO_RNODE_PORT"),
        help="RNode serial port (default: $LMAO_RNODE_PORT or auto-detect).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60000,
        help="Command expiry timeout in milliseconds (default: 60000).",
    )
    args = parser.parse_args()

    # Parse optional params
    params = {}
    if args.params:
        try:
            params = json.loads(args.params)
            if not isinstance(params, dict):
                logger.error("--params must be a JSON object (dict), got: %s", type(params).__name__)
                sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in --params: %s", e)
            sys.exit(1)

    # Initialise Reticulum + LXMF
    from lmao_server.server import Server, _init_rns_and_lxmf, _warn_if_rnode_missing

    rnode_port = args.rnode_port
    if rnode_port:
        _warn_if_rnode_missing(rnode_port)

    logger.info("Initialising Reticulum + LXMF router (rnode_port=%s)...", rnode_port or "auto")
    try:
        server_identity, router = _init_rns_and_lxmf(rnode_port)
    except Exception as e:
        logger.error("Failed to initialise RNS/LXMF: %s", e, exc_info=True)
        sys.exit(1)

    identity_hex = server_identity.hexhash if hasattr(server_identity, "hexhash") else ""
    logger.info("Server identity: %s", identity_hex[:32] if identity_hex else "<unknown>")

    # Create Server instance and send command
    server = Server()
    server.server_identity = server_identity
    server.router = router

    # Register delivery callback (required for LXMF to process path responses)
    router.register_delivery_callback(server.handle_lxmf_delivery)

    logger.info(
        "Sending CommandRequest: target=%s action=%s params=%s timeout=%dms",
        args.target,
        args.action,
        params,
        args.timeout,
    )

    success = server.send_command(
        target_identity_hex=args.target,
        action=args.action,
        params=params,
        timeout_ms=args.timeout,
    )

    if success:
        logger.info("CommandRequest dispatched successfully.")
        # Give the router a moment to flush TX
        time.sleep(2)
        sys.exit(0)
    else:
        logger.error("CommandRequest dispatch failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
