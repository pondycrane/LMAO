# Skill: family-calendar line drawing (pic → Postgres)

Generates a **1-bit black & white line drawing** for the family calendar and
stores it in the in-cluster Postgres `cal_pics` table, keyed by the
`CalEvent.pic` field. Designed for the 4.2" RLCD calendar display
(**400×300 landscape** logical canvas on the panel's 300×400 memory): the
picture is the *headline* tile (default **176×176**) drawn at native size on
the left, leaving the right column for the title, time range and forecast
and the band below for the compact event rows.

## Prerequisites (once)

System python with the image + DB libs (Pillow/cairosvg + psycopg) — on the
host the agent runs on:

```bash
python3 -m pip install --break-system-packages Pillow cairosvg 'psycopg[binary]'
```

## Invoke

```bash
# Rasterize an existing pack key (assets/calendar-icons/<key>.svg) and store
bazel run //tools:calendar_pics -- --key swim

# Find a line drawing online and store it under a key (the tool downloads
# the URL — SVG preferred, PNG/JPEG works too — and renders it 1-bit B&W)
bazel run //tools:calendar_pics -- --key robodog \
    --source https://commons.wikimedia.org/wiki/Special:FilePath/Dog.svg

# A custom local SVG/PNG source at a different tile size, also saved locally
bazel run //tools:calendar_pics -- --key shopping --source my-basket.svg \
    --size 128x128 --out /tmp/check.png

# List keys already in the DB
bazel run //tools:calendar_pics -- --list
```

The tool is `tools/calendar_pics.py` (also runnable as
`python3 tools/calendar_pics.py`), backed by
`lma_core.calendar_pics.PicStore`.

## Postgres connection

The DSN uses the same env the server does — `LMAO_CONTACTS_URL`
(`postgres://…`) wins; otherwise `LMAO_CONTACTS_PG_HOST/USER/PASSWORD/DB`
parts are composed (one DB + Secret as the contact book/calendar).
Out-of-cluster, reach Postgres via `kubectl port-forward` and set
`LMAO_CONTACTS_URL`:

```bash
kubectl port-forward svc/postgres 5433:5432 --address 127.0.0.1 &
export LMAO_CONTACTS_URL='postgres://lmao:$PW@127.0.0.1:5433/lmao'
```

The `cal_pics` table is created on first use; rerunning a key overwrites
(idempotent) — regenerate artwork freely.

## Output contract

- **format**: PNG, 1-bit, two colors only (line = `0` ink, background `255`).
- **size**: default `176x176` (the panel's headline slot on the 400×300
  landscape canvas); clamped to never exceed the RLCD tile area
  (`pick_tile_size()`).
- **reference**: 176×176 line art ≈ 500–900 bytes (the headline slot's native
  size; the extra resolution over the old 144×144 costs a few hundred bytes).
- store schema: `cal_pics(pic_key PK, width, height, mime, bytes, created_ms, updated_ms)`.

## Procedure for the agent

1. Decide the key (must match the `pic` value your calendar events use).
2. Prefer a real line drawing over an agent-drawn one: for a key not in
   `assets/calendar-icons/`, find a **small, simple, black-on-white line
   drawing online** (Wikimedia Commons is a good source; prefer public-domain
   works) and pass its URL via `--source`. Note the URL, and only fall back
   to hand-drawing an SVG when no good line drawing exists — invented SVG
   artwork reads badly on the reflective panel.
3. Run the tool (above). Verify the `Stored '<key>': WxH…` line.
4. Optionally also write `--out <png>` if the display client vendors local
   assets instead of reading Postgres directly.

## Automated stage (hourly CronJob)

`tools/calendar_pics_sync.py` fills missing keys for upcoming kid events
without an agent. It draws **what the event says**, from the web:

1. **Query from the event** — `pick_character` chooses a character from the
   kids' shows (`juliet` → Pokemon pool, `wesley` → PAW Patrol pups, a shared
   event → either; seeded by title+participants, so one kid's events rotate
   while a re-run never reshuffles), `extract_action` maps the event to an
   activity phrase (`J tennis 4pm` → `playing tennis`, `W dental` → `at the
   dentist`, `Wesley Princeton` → `at school`).  Primary query:
   `<character> <activity> coloring page` (`pikachu playing tennis coloring
   page`); then `<character> coloring page`; then `person_word`'s generic kid
   (`juliet` → `girl`, `wesley` → `boy`, shared → `kids`) doing the activity.
2. **Sources** — Wikimedia Commons plus the keyless DuckDuckGo image index.
   The ranker prefers line art / coloring pages, a single drawing over a
   multi-panel print sheet, and **the requested character above any generic
   kid** (its distinctive word — `character_token` — carries a +120 score, and
   `pick_source` returns a candidate naming it whenever one exists) — and it
   **rejects AI-generated art** (`ai-generated`, `generated vector`, diffusion
   tool names in the title/URL) and SVGs (the sync image has no cairosvg).
3. **Framing** — `lma_core.calendar_pics.rasterize_line_art`, the one
   rasterizer the by-hand tool shares: trim to the *subject* bbox
   (`subject_mask` drops page furniture such as a footer logo or a page
   number, which would otherwise land in the tile as a black blob), threshold
   at full resolution, area-average the ink mask down (thin 1-px strokes
   survive a 10× downscale), then fit **aspect-preserving** and center on the
   white tile with a 6-px margin.  A portrait coloring page is no longer
   squashed into a square.
4. **Gate** — the tile must be outline work: ink fraction 0.02–0.45 **and**
   at least 0.15 of the tile as enclosed white interior.  The interior check
   separates a drawing from a filled silhouette (0.35 ink, ~0.02 interior) or
   a badly cropped blank page.  A source whose subject bbox is a >2.6:1 strip
   is skipped before rasterizing (it would be a sliver at tile size).

There is **no image generator and no model call** anywhere in this path.  An
event with no acceptable hit stays artless and retries on the next hourly
run; place it by hand with `--source` above when the web has nothing usable.
