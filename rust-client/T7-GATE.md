# T7 — Integration + packaging + E2E: evidence

**Ticket:** T7 (#165). Crate layout finalized per design §5; `bazel test
//rust-client:...` host + hardware gate; flash + full E2E on the Cardputer
against RNode + server.
**Accept:** **stable RSS Resource transfer on the Rust stack** demonstrated
end-to-end on the Cardputer and documented in README.

## Result: HOST PASS (full composed leaf pipeline, §5 data-flow) · on-device RF E2E PENDING-HARDWARE

The §5 crate layout is finalized (`rust-client/README.md`, mirrored dir tree).
The whole leaf now **composes and runs on the host** — identity → LXMF control
path → RNS Resource over a held, duty-cycled link — which is the host half of
this milestone. The on-device RF leg (radio-interface → link → Resource over the
real RNode to the production server) needs firmware transport bring-up + an
attached RNode and is production-gated, per the standing mandate.

## `host/leaf-e2e` — the composed leaf pipeline (bazel `//rust-client:run_leaf_e2e`)

Runs the §5 data-flow on the host in one binary that returns PASS/FAIL (exit
code), tying the T2/T4/T5/T6 crates together:

1. **Identity** (lma-identity): mint leaf + server, compute the server's
   `lxmf/delivery` DEST.
2. **Control path** (lma-wire → lma-lxmf): build a 3-reading `SensorReport`
   `LmaoEnvelope`, pack + sign it into an LXMF message addressed to the delivery
   DEST, unpack + verify → decodes to the same report (delivery addressed,
   signature valid).
3. **Resource leg** (leaf-rns paces leaf-resource): the gateway pushes a 5000 B
   RNS Resource to the leaf; each part is applied on the leaf **only inside an
   open link window** (`LinkWindowScheduler`, window 160 ms / gap 840 ms); across
   a dormant gap the full payload is **hash-verified and reassembled** (proof
   produced). This is stable "Resource transfer on the Rust stack" at the host
   level: the link is held (sustain) and the transfer completes across windows.

Observed on this host:

```
control: SensorReport(LXMF) -> delivery bb0125...347e5 verified; 3 readings; wire 76 B
resource: RNS Resource reassembled+hash-verified by the leaf across 2 link windows; 5000 B; proof 64 B
[t7-e2e] PASS
```

## Packaging / README (§5 finalization)
`rust-client/README.md` documents the §5 data-flow tree, per-ticket status, and
the Bazel-first gate surface (all `manual` sh_binary wrappers, no rules_rust —
matching native-client). Crate members finalized:
`host/interop`, `host/leaf-e2e`, `lma-identity`, `leaf-rns`, `radio-interface`,
`lma-wire`, `lma-lxmf`, `leaf-resource`.

## Tie-in to the other tickets (all green on the host)
`run_leaf_e2e` depends on and exercises T2 (identity/DEST), T5 (control wire +
LXMF), T4 (link-window scheduler), T6 (Resource machinery + RNS hashes). It
links them into one leaf.

## Bazel-first
`host/leaf-e2e` → `//rust-client:run_leaf_e2e` (manual sh_binary over
`:rust_sources`). Host gates: `test_lma_identity`, `test_radio`, `test_leaf_rns`,
`test_lma_wire`, `test_lma_lxmf`, `test_leaf_resource` (all PASS).

## On-device hardware leg (held)
Flashing the full Rust stack to the Cardputer and completing a real RNS Resource
over the RNode/LoRa to the production server is the on-device half of T7. It
additionally needs the no_std firmware to drive the RNode as `rns_core::Interface`
and run the §5 pipeline in `firmware/` (currently T1 boot), plus an attached
RNode. The MicroPython Cardputer stays live until then.
