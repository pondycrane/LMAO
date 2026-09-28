# T8 — rebuild LMAF / successor framing ON the stable Resource path: evidence

**Ticket:** T8 (#166). Apply the new transfer framing **on top of** the stable
T6/T7 Resource substrate, still **protobuf-schema'd + vector-tested per §6**.
Started only because T6/T7 gates are green (they are).

## Result: HOST PASS (successor framing wire in the shared proto + Rust reassembler on Resource)

The successor-framing wire contract is now in `proto/lma_messages.proto`
(design §6c — the anti-drift fix: the native C++ emitted it by hand with no
schema to pin it), **prost-generated** into `lma-wire`, **vector-tested byte-
for-byte against the native C++ `lma_attachment` encoding**, and the Rust
receiver-side **reassembler** is implemented in `crates/lma-framing`, exercised
carried by an RNS Resource (the §5 data-flow's stable substrate). The on-device
chart-downlink leg is pending-hardware as before.

## Wire contract in the proto (design §6c)
Added (additive — no existing field renumbered), matching the C++ `lma_attachment`
field layout exactly:
- `LmafManifest` (envelope oneof **40**): id (bytes, sha256(payload)[:16]),
  payload_sha256, kind, codec, chunk_size, chunk_count, total_bytes, sample_rate,
  channels, duration_ms, width, height, node_id, created_ms.
- `LmafChunk` (**41**): id, index, data(bytes), crc(**fixed32**).
- `LmafAck` (**42**): id, status, have_count, missing, reason — `missing` is
  `[packed = false]` to match the C++ (it emits non-packed repeated varints).
- `LmafCapability` (**43**): lmaf_version, kinds(`[packed=false]`), codecs,
  max_chunk_size, max_attachment_bytes, rx_window, airtime_budget_bps.
- `LmafKind` / `LmafAckStatus` enums pinned to the C++ values.

## Cross-language vectors (lma-wire, §6b/§6c)
`crates/lma-wire/tests/lmaf_wire.rs` (via `//rust-client:test_lma_wire`):
prost-encoded Manifest/Chunk/Ack/Capability envelopes are **byte-identical** to
goldens generated from a pure-Python reference that reproduces the C++
`lma_attachment` wire (varint/len/fixed32, non-packed repeats). 4 tests — the
catch that `missing`/`kinds` must be non-packed was found and pinned here.

## `crates/lma-framing` — the receiver-side reassembler (no_std + alloc)
`Reassembler`: open a session from a validated manifest, ingest chunks
(id + index-gated, **per-chunk CRC-32** = zlib poly 0xEDB88320), track gaps,
and on completion recompute **whole-payload SHA-256** against the manifest digest;
build a `LmafAck` (`NEED`+missing, or `COMPLETE`). 5 host tests via
`//rust-client:test_lma_framing`:
- `crc32_known_vector` (0xCBF43926), `crc_mismatch_rejects_chunk`,
  `feed_all_chunks_verifies_and_acks_complete`, `missing_chunks_report_need_with_gaps`,
  `foreign_id_rejected_and_duplicate_ignored`.

## On the Resource substrate (the "on Resource" half)
`crates/lma-framing/tests/resource_substrate.rs`: a gateway ships the attachment
as an **RNS Resource** over the link (leaf-resource); the leaf reassembles the
raw payload over the resource, then the framing layer splits it back into the
manifest's chunks, re-checks each CRC + the whole-payload SHA-256, and acks
`COMPLETE`. 1 test.

## Bazel-first
`crates/lma-framing` → `//rust-client:{lma_framing_sources, test_lma_framing}`
(manual). LMAF cross-language vectors live under `lma-wire` (needs PROTOC).

## On-device hardware leg (held)
Flashing the successor-framing chart downlink to the Cardputer over LoRa and
reassembling a server-pushed chart on-device remains hardware-pending (RNode +
firmware transport, T7 hardware leg).
