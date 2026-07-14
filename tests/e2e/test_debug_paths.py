"""Debug script to check PYTHONPATH and imports in Bazel sandbox."""

import os
import sys

print("CWD:", os.getcwd())
print("PYTHONPATH env:", os.environ.get("PYTHONPATH", "NOT SET"))
print("Sys.path:")
for i, p in enumerate(sys.path):
    print(f"  [{i}] {p}")
    if os.path.isdir(p):
        try:
            contents = [d for d in os.listdir(p) if d.startswith("p")]
            if contents:
                print(f"       (has: {contents[:5]})")
        except PermissionError:
            print("       (no access)")

print()
print("Checking _main/proto/lma_pb2.py...")
candidate = os.path.join(os.getcwd(), "_main", "proto", "lma_pb2.py")
print(f"  Path: {candidate}")
print(f"  Exists: {os.path.exists(candidate)}")

# Try relative
import importlib.util

spec = importlib.util.find_spec("proto.lma_messages_pb2")
print(f"\n  Find spec: {spec}")
