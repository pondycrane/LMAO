# Skill: family-calendar line drawing (pic → Postgres)

Generates a **1-bit black & white line drawing** for the family calendar and
stores it in the in-cluster Postgres `cal_pics` table, keyed by the
`CalEvent.pic` field. Designed for the 4.2" RLCD calendar display
(300×400 portrait): the picture is a content *tile* (default **144×144** —
about half the 300 px width) so the screen still has room for the
title/time/date rows.

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

# A custom SVG/PNG source at a different tile size, also saved locally
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
- **size**: default `144x144`; clamped to never exceed the RLCD tile area
  (`pick_tile_size()`).
- **reference**: 144×144 line art ≈ 400–600 bytes (e.g. `swim` = 572 B).
- store schema: `cal_pics(pic_key PK, width, height, mime, bytes, created_ms, updated_ms)`.

## Procedure for the agent

1. Decide the key (must match the `pic` value your calendar events use).
2. Prefer a source from `assets/calendar-icons/`; otherwise draw/produce an
   SVG at the tile aspect and pass `--source`.
3. Run the tool (above). Verify the `Stored '<key>': WxH…` line.
4. Optionally also write `--out <png>` if the display client vendors local
   assets instead of reading Postgres directly.
