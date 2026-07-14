import os
import sys
print("CWD:", os.getcwd())
print("PYTHONPATH env:", os.environ.get("PYTHONPATH", "NOT SET"))
print("Sys.path:")
for i, p in enumerate(sys.path):
    print(f"  [{i}] {p}")
    if os.path.isdir(p):
        try:
            contents = sorted([d for d in os.listdir(p) if not d.startswith(".")])[:8]
            if contents:
                print(f"       (has: {contents})")
        except PermissionError:
            pass

# Check for proto
print()
for p in sys.path:
    candidate = os.path.join(p, "proto", "lma_messages_pb2.py")
    if os.path.exists(candidate):
        print(f"Found proto.lma_messages_pb2 at: {candidate}")
    candidate2 = os.path.join(p, "_main", "proto", "lma_messages_pb2.py")
    if os.path.exists(candidate2):
        print(f"Found at _main variant: {candidate2}")
