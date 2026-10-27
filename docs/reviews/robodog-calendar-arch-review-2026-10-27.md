# RoboDog Family-Calendar Client — Architectural Review (2026-10-27)

Scope: `robodog_client/` calendar firmware (`main/`), its server counterpart
(gRPC `GetCalendar` + framed-TCP :4247), and the shared tooling of the three
firmware personalities on the same board (calendar / hermes voice / esp-claw).
Focus: architecture + DRY. Every finding is cited `file:line`; the headline
claims were verified first-hand against the sources.

## Architecture as built

```
robodog_client/main (one ESP-IDF component)
  main.c ──> robodog.c (1214 lines, 7 concerns)
               ├── lma_proto.c/.h   hand-rolled protobuf codec (host-testable)
               ├── rd_clip.c/.h     measured-width clip + word wrap (pure)
               └── rd_datetime.c/.h civil-day math + meta formatting (pure)

Board ── hand-rolled HTTP/2 + gRPC POST /lma.LMAO/GetCalendar :50051 ──> lmao_server
lmao_server ── CalendarStore.sync (Postgres cal_events + cal_pics)
lma_core/calendar_tcp_api.py :4247 ── same store, ⚠ no client in repo
```

`robodog.c` concern split: NVS config/cache (:99-260), cache merge (:262-330),
WiFi (:332-410), SNTP (:412-433, compiled out), TCP/HTTP-2/gRPC (:435-660),
rendering (:662-1129; `render_screen` alone ~260), hold loop + entry
(:1131-1214).

### What's done well (keep)

- `rd_datetime` / `rd_clip` are pure, dependency-free, host-testable units.
- `rd_utc_ms_to_local_day` is a documented single source of truth for day
  math — header and rows cannot drift apart.
- `lma_proto.h:34-51` field numbers match `proto/lma_messages.proto:155-179`
  exactly (verified).
- u8g2 fetched at build time, not vendored (repo rule honored).
- `capture.py` is genuinely shared across the three firmware personalities.

## P1 — Two server calendar transports; one orphaned and already drifted

Verified first-hand:

- The firmware uses **gRPC :50051** (`main/config.h:50-52`; hand-rolled
  HTTP/2 at `main/robodog.c:433-545`).
- The framed-TCP service on **:4247** (`lma_core/calendar_tcp_api.py`,
  started at `lmao_server/server.py:1151-1164`) has **no client in the
  repo** — kept alive only by `tests/test_calendar_tcp_api.py`. It duplicates
  the bundle-assembly loop from `server.py:955-971` and has drifted behind:
  - no full-fetch tombstone filter (gRPC has it at `server.py:951-955`);
  - never fills `pic_data` → the panel headline icon cannot render over TCP.
- Doc drift in three places: `main/robodog.c:8,15-19` and `main/lma_proto.h:3`
  still describe the firmware as a framed-TCP client of
  `calendar_tcp_api.py`; the README env table (`LMAO_CAL_TCP_PORT`, "RoboDog
  native client") contradicts README §7d (gRPC).

**Decision needed** — either, not the status quo:

| Option | Trade |
|---|---|
| A. Delete the TCP path (`calendar_tcp_api.py` + its test + `server.py:1151-1191` + env var) | Kills dead code; keeps ~130 lines of fragile hand-rolled HTTP/2 on the board (no HPACK decode, no grpc-status, no flow control — `robodog.c:442-446`) |
| B. Revert the firmware to framed TCP, extracting one shared `build_bundle(store, req)` for both frontends | ~4-byte framing vs HTTP/2+HPACK on an MCU; requires re-adding the tombstone filter + `pic_data` fill to `_sync_to_bundle` |

Recommendation: **B with the shared helper** — framed TCP was the right MCU
design; the gRPC detour bought nothing while the board is the only consumer.
If gRPC stays (A), the HTTP/2 client at minimum needs grpc-status handling.

## P2 — DRY findings

| # | Finding | Evidence |
|---|---|---|
| 1 | Flash-script boilerplate triplicated (~40 lines ×3): preamble, `--port` parsing, host-esptool check, identical `write_flash @flash_args`; NVS-image generation duplicated flash.sh↔flash_esp_claw.sh. Already drifted: the "option-shaped value is an ERROR" fix was not back-ported to `xiaozhi_flash.sh:26`. Fix: shared `flash_common.sh`. | `flash.sh:24-127` ≈ `xiaozhi_flash.sh:14-47` ≈ `esp-claw/flash_esp_claw.sh:25-122` |
| 2 | RD_SHOT capture protocol implemented 3× + parsed 2×: calendar firmware, esp-claw overlay, hermes patch; `robodog_claw_e2e.py:385-428` forks `capture.py`'s frame parser instead of parameterizing it. | `main/robodog.c:853-870`, `esp-claw/overlay/components/robodog_phase0/src/robodog_phase0.c:70-73`, `voice/robodog_hermes_capture.patch:22-25`, `capture.py:160-201` |
| 3 | E2E scaffolding ~150-200 lines duplicated between `voice/hermes_voice_e2e.py` (768 ln) and `esp-claw/robodog_claw_e2e.py` (821 ln): env block, `log`/`shell_run`, ffmpeg wrappers, serial arm dance (3rd copy in `capture.py:264-275`). Worse: two divergent `detect_board_port` implementations; claw re-implements loud-window trim (`trim_loudest_window` claw:179-227) and raw Whisper POST (claw:117-139) that `e2e_readout.energy_burst_window` / `voice_bridge.call_asr` already provide. Fix: shared `e2e_rig.py`. | hermes:82-196 ≈ claw:42-241 |
| 4 | Varint/wire framing in 3 places: `lma_proto.c:13-124`, `firmware_common/lma_common/lma_encoder.cpp:6-26` (different tree/language — acceptable), and `lma_proto_selftest.c:22-57` re-encoding the wire format for fixtures — the test serializer can drift from production encode and stay green. | riskiest copy is the selftest |
| 5 | Decode-case guard boilerplate repeated ~15× in `lma_proto.c` (e.g. :198-206, :221-229, :311-318); `event_decode` bounds against `end` while bundle-level uses `len` — the inconsistency class the copy-paste invites. Fix: `read_var()`/`read_ld()` helpers (~30% smaller). | `main/lma_proto.c` |
| 6 | NVS key names in 4 copies: `#define` (`robodog.c:87`), positional array (`robodog.c:117`), `config.h:5-8` comment, `flash.sh:92-95` CSV. `flash.sh` carries a comment about a past namespace-mismatch bug from exactly this. | single shared key list |
| 7 | `pic_data` tile size documented as 3 different values: "32x32/128B" (`proto/lma_messages.proto:170-171`), "64x64/512B" (`lma_proto.h:73-77`), actual 144×144/2592 B (`lma_proto.h:57`, `lma_core/calendar_pics.py:188-190`). Wire unaffected; both comments stale — fix the canonical proto comment. | verified |
| 8 | UTF-8-boundary back-off loop written 3× in `rd_clip.c:42-46, 155-160, 163-171` (comments admit they "mirror" each other). Fix: `prev_utf8_boundary()`. | `main/rd_clip.c` |
| 9 | `robodog_client/BUILD`: six near-identical `sh_binary` blocks (~80 lines → ~20 with one macro); no `robodog_claw_e2e` run target (hermes has one, BUILD:181-190); claw borrows `voice/corpus/capabilities.wav` inline (BUILD:110) instead of depending on `:voice_corpus` (BUILD:193-199); claw e2e imports `capture.py` with no declared `data` edge. | `robodog_client/BUILD:11-120, 181-199` |
| 10 | Host selftests exist but no BUILD target runs them — the "host cc gate" is a copy-paste line in a comment; they can silently stop being run. Fix: two `cc_test` targets. | `main/lma_proto_selftest.c:6-8`, `main/rd_datetime_selftest.c:9-12` |

## P3 — Risks (non-DRY)

- **No watchdog, blocking design**: `hold_until_next_sync` busy-loops 30 s
  chunks (`robodog.c:1144-1158`); one `SO_RCVTIMEO` value reused for connect
  and all reads (:522-525) — a slow-drip server stalls a full cycle; a wedged
  recv = hung board until power-cycle. The Cardputer client already learned
  this (WDT + self-reset); RoboDog hasn't.
- **Full fetch every boot** (`robodog.c:1203-1205` forces `s_watermark = 0`):
  the delta-merge machinery is boot-only infrastructure; the design comment
  (:90-93) and the code live 1100 lines apart and partially contradict.
- **NVS blob = raw struct prefix** via `offsetof(lma_cal_event_t, pic_data)`
  (`robodog.c:96`): any field reorder silently corrupts every persisted
  event; no schema version stored.
- **`cache_key()` returns a shared static buffer** (`robodog.c:102-107`)
  among 8 other file-scope statics — non-reentrant landmine.
- **`--kid-name` flash wipes the whole 24 KB NVS** including NVS-set WiFi
  credentials (`flash.sh:14-19, 88-97`) — documented but a footgun.
- **Hand-rolled HTTP/2 correctness assumptions** (`robodog.c:560-617`):
  assumes response DATA ≤ 16 KiB, ignores response HEADERS/HPACK, no
  WINDOW_UPDATE/PING/GOAWAY semantics — works against the one known server,
  fragile against any proxy or server change.

## Highest-leverage fixes, in order

1. Resolve the transport fork (P1) — delete or revive :4247, extract shared
   bundle assembly, fix stale comments in `robodog.c` / `lma_proto.h` /
   README / the proto's 32×32 comment.
2. Split `robodog.c`: HTTP/2+gRPC client and NVS cache as separate
   components; render layout constants (:796-801, :985-1109) into one block.
3. `flash_common.sh` + shared RD_SHOT parser + `e2e_rig.py` (P2 #1-3) —
   actively drifting, not just duplicated.
4. Wire both selftests into Bazel as `cc_test`s (P2 #10).
5. Add esp_task_wdt + separate connect/read timeouts (P3).

---
*Method: two parallel read-only code scouts + first-hand verification of the
transport, schema, and drift claims against `lmao_server/server.py`,
`lma_core/calendar_tcp_api.py`, and `proto/lma_messages.proto`.*
