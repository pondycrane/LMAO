#!/usr/bin/env python3
"""Flash the voice Cardputer native client with a per-device identity.

The repeatable foundation target for the voice Cardputer (the kitchen):
builds the native firmware with the per-device identity + the server
DEST_HASH baked in, flashes it via idf.py, and registers the device's public
delivery hash in the server contact book — the same pipeline as

    install_all --native-cardputer --device-name <name> --skip-rnode

exposed as a single runnable bazel target so `bazel run :flash_voice_cardputer`
always reproduces the exact working flash.  Runtime is native C (PR1); the
identity is the same no-drift private key the MicroPython client used, so one
ALLOWED_CLIENTS entry covers both runtimes and a re-flash never changes the
lxmf/delivery hash.

Usage:
    bazel run //tools:flash_voice_cardputer
    bazel run //tools:flash_voice_cardputer -- --port /dev/ttyACM0
    bazel run //tools:flash_voice_cardputer -- --port /dev/ttyACM0 --device-name kitchen
"""

import argparse
import os
import sys

# Repo root: BUILD_WORKSPACE_DIRECTORY is set by `bazel run` (and points at the
# real source tree, not the runfiles sandbox).  Fall back to the script dir.
ROOT = os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="flash_voice_cardputer",
        description="Build + flash the native LMAO voice Cardputer client with an "
        "identity baked in, and register it in the server contact book.",
    )
    ap.add_argument(
        "--port",
        default=None,
        help="Cardputer serial port (e.g. /dev/ttyACM0). Auto-detected when omitted.",
    )
    ap.add_argument(
        "--device-name",
        default=None,
        help="Per-device identity name (resolves/mints the private key by name, e.g. "
        "'kitchen'). Baked into the firmware and registered in the contact book.",
    )
    ap.add_argument(
        "--skip-dest-hash",
        action="store_true",
        help="Do not bake the server DEST_HASH (runs in announce-only mode).",
    )
    ap.add_argument(
        "--skip-register",
        action="store_true",
        help="Do not POST the device's delivery hash to the server contact book.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    from tools.install_all import DeviceResult, _flash_cardputer_native

    args = _build_parser().parse_args(argv)

    port = args.port
    if port is None:
        from tools.install_all import find_cardputer_port

        port = find_cardputer_port(None)
        if not port:
            print("FAIL: no Cardputer detected on USB (pass --port explicitly)")
            return 2
        print(f"Auto-detected Cardputer on {port}")

    result = DeviceResult("Cardputer (voice)")
    try:
        _flash_cardputer_native(
            port, result,
            inject_dest_hash=not args.skip_dest_hash,
            device_name=args.device_name,
        )
    except Exception as exc:  # keep failures visible + non-zero
        result.fail(str(exc))
        print(f"  FAIL: {exc}")

    if result.status == "OK" and args.device_name and not args.skip_register:
        try:
            from lma_core.client_identity import device_delivery_destination_hash
            from tools.install_all import _register_contact_book

            dev_hash = device_delivery_destination_hash(args.device_name)
            _register_contact_book(args.device_name, dev_hash)
        except Exception as exc:
            print(f"  WARNING: contact-book registration skipped: {exc}")

    print()
    print(f"  [{'OK' if result.status == 'OK' else result.status}] Cardputer (voice) — {result.detail}")
    return 0 if result.status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
