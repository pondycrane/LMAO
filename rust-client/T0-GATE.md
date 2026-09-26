# T0 DECISION GATE — evidence record

**Ticket:** T0 — Wire-compat + Link/Resource spike (HOST; DECISION GATE), design §10.
**Directive context:** the critical feature is RNS native `Resource` over `Link`
(design §2) — LMAF rebuild deferred (Ticket 8).

## Result: PASS

RNS **Resource-over-Link** transfer interops with Python RNS **bidirectionally**
— chunk sequencing / per-chunk hash-map reassembly, content integrity, and
sender-side completion proof verified. Validated against **Python RNS 1.3.5**,
the exact version the production `lmao-server` runs.

### Gate transcript (6 steps, all verified)

```
[gate] spawning Python RNS (interpreter: /tmp/t0-venv/bin/python)
[gate] Python RNS up -> port 43021 dest 5a5a619603cd5b7daff80d0010f2e292
[gate] (1) announce/path: Python announce -> Rust          ok (hops==1)
[gate] (2) Rust -> Python packet over path                 ok (plaintext echo)
[gate] (3) Rust announce + Python return packet            ok (decrypted)
[gate] (4) Link establish (Python initiated, Rust serves)  ok (link_id match, rtt)
[gate] (5) Resource RX: Python -> Rust (100000 B)          ok (len + byte pattern)
[gate] (6) Resource TX: Rust -> Python (100000 B)          ok (sha256 + completion proof)
[gate] T0 GATE: PASS — Link + Resource interop with Python RNS verified bidirectionally
```

Assertions that gave the gate its bite (any failure → non-zero exit):
- `(4)` link_id returned by the Python `link_established` callback equals the
  Rust `on_link_established` link_id; Rust is the non-initiating responder.
- `(5)` the 100000-byte Python→Rust resource reassembles to the exact
  `i % 251` byte pattern and full length — proving chunk/hash-map reassembly.
- `(6)` the Python side's SHA-256 of the reassembled Rust→Python resource equals
  `rns_core::hash::full_hash` of the original, **and** Rust receives the
  sender-side completion proof (`on_resource_completed`).

### How to reproduce

```bash
python3 -m venv /tmp/t0-venv && /tmp/t0-venv/bin/pip install "rns==1.3.5"
cd rust-client
LMAO_PYTHON=/tmp/t0-venv/bin/python cargo run -p lmao-t0-interop    # need NOT be root
# exit 0 = PASS (see UPSTREAM.md for the Bazel shim)
```

## Scope / hardware honesty (design §11, #1-related)

**No Cardputer flash at this stage.** T0 is the library/host-spike stage — there
is no Rust firmware to put on the device yet (that is T1+). The Cardputer and
its running MicroPython `urns` stack are untouched and stay untouched until a
Rust firmware actually exists (the design's "MicroPython stays until Rust is
E2E" mandate). No `esptool`, no raw-REPL flash was or should be run on the
Cardputer for T0.

**Stable MicroPython revert option (ready for hardware tasks):** when the Rust
firmware is eventually flashed to the Cardputer (T1+), restoring the old
MicroPython path is the existing canonical
`bazel run //cardputer_client:flash` (raw REPL; re-injects the server
`DEST_HASH`). Until Rust firmware replaces the MicroPython *runtime* image,
that raw-REPL target is the complete revert; preserving a full firmware image
for revert is tracked with the T1+ firmware flash target.

**Production-RF-live leg** of T0 ("against the live production mesh — RNode +
Python server") is also not run from this host: the only RNode is a `hostPath`
serial device consumed by the `lmao-server` pod on K8s node `tp4`
(`192.168.10.19`), and this box has no second radio. A second mesh participant
cannot share that one serial RNode. The protocol-interop substance of the gate —
the Link + Resource machinery vs the exact Python RNS the server runs — is fully
exercised over a shared **TCP mesh** (the canonical Reticulum method for
multi-instance interop, and the same model lelloman's own `python_interop.rs`
uses). The RF-path leg is a second-radio hardware prerequisite, tracked for T4
(duty/link-window decision) / T7 (Cardputer E2E) rather than a failure of the
protocol gate.

**Bazel-first (all new code is a Bazel target):** the whole `rust-client/` tree
is in the Bazel graph and driven from Bazel:
- `bazel build //rust-client:rust_sources` — source tree in the graph
- `bazel run //rust-client:build` — `cargo build --workspace`
- `bazel run //rust-client:run_t0_gate -- --python <py>` — the T0 gate

## Consumed toolchain

- Rust `stable` via rustup (host); firmware `nightly` + xtensa deferred to T1.
- rns-core 0.1.17 / rns-crypto 0.1.10 / rns-net 0.7.2 pinned in `Cargo.lock`;
  protocol-source mirrors in `vendor/`; pin record in `UPSTREAM.md`.
