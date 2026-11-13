---
name: robodog-ui
description: Design and review the RoboDog panel UIs — the 400x300 1-bit reflective ST7305 RLCD driven by u8g2 (calendar, home env chart) or LVGL (ESP-Claw). Covers the medium's hard invariants (one ink, one raster font size per role, luma-130 binarisation, no hover/motion/greys), hierarchy and spacing rules that survive 1-bit quantisation, the state-encoding channels that remain (text, shape, position), and the pixel-driven verification loop (RD_SHOT capture + vision readback + the op4 bench). Use when building, restyling, or critiquing any RoboDog screen, chart, header or status row.
---

# robodog-ui

Design rules for the RoboDog RLCD. The panel is a **reflective, 1-bit, 400×300**
ST7305. Every rule below follows from that and from what the firmware can
actually draw — not from web design habits.

Read this before touching `robodog_client/main/rd_render.c` (calendar),
`robodog_home_client/firmware/main/ui.cpp` (env chart), or the LVGL UI in
`robodog_client/esp-claw/overlay/components/robodog_phase0/src/robodog_ui.c`.

## The medium (non-negotiable facts)

- **One ink.** Pure black on white. There is no grey: the u8g2 driver writes
  1 bpp; the LVGL path renders RGB565 and the panel driver binarises with
  BT.601 luma at threshold 130 (`RLCD_LUMA_THRESHOLD` in
  `robodog_client/esp-claw/overlay/boards/waveshare_ESP32_S3_RLCD_4_2/setup_device.c`).
  Anything mid-tone (opacity, a 50 % fill, a thin grey rule) flips
  unpredictably per pixel. **Never** encode meaning in a mid-tone.
- **No pointer, no hover, no motion.** The board has one button. There is no
  cursor, no pressed state, no animation budget. State must be visible in the
  resting frame.
- **Reflective contrast is a lighting budget.** Contrast is best in daylight
  and degrades in a dim room. Text that must be read is black-on-white, never
  thin-stroke.
- **One font raster per role.** u8g2 fonts are pre-rendered bitmaps: a
  "16 px" face cannot render at 14 px. Available faces:
  `u8g2_font_6x10_tf` (meta), `u8g2_font_9x15B_tf` (rows),
  `u8g2_font_10x20_tf` (body/header), `u8g2_font_helvB18_tf` (headline).
  LVGL's path has exactly one generated face: Noto Sans CJK TC 16 px 1 bpp
  (`gen_font_tc_16.sh`). **There is no type scale** — hierarchy comes from
  weight, position, rules and inversion, not size.
- **CJK is not free.** The u8g2 faces carry ASCII only; the LVGL face carries
  ASCII + CJK. Never assume a glyph exists — probe with `u8g2_IsGlyph` and
  fall back (see `ellipsis_for` / `deg_suffix`).

## Hierarchy rules

1. **One primary element per screen.** The thing the screen is about — the
   headline event (calendar), the current reading (home), the reply text
   (voice) — gets the largest face and the most space. Everything else is
   secondary.
2. **A reading is the primary element, not a decoration.** If the panel's job
   is "what is it now", the number is the biggest thing on the panel; the
   chart/rows support it. Do not repeat the same number in two places at two
   sizes (it reads as two competing facts).
3. **Rules are real borders.** A separator is a 1 px `DrawHLine`/`DrawFrame`,
   never a light fill. Two solid pixels beat four grey ones.
4. **Group by proximity, not by boxes.** Do not frame every row. One frame
   around the plot is structure; a frame around each row is noise.
5. **Align to a baseline grid.** Pick the row baselines once (header, meta,
   plot, axis) and derive every element from them. Equal gutters, equal row
   heights.

## State without colour or motion

State is carried by the three channels that remain:

- **Text first.** `no wifi`, `stale`, `updated`, `waiting for server` — a word
  beats an icon, and it is what a vision readback can actually verify.
- **Shape second.** A filled square = live/now; a hollow square = stale/frozen;
  a crossed box = failed. Draw the shape at the same baseline as its word so
  they read as one unit.
- **Position third.** Degraded state moves to a fixed slot (e.g. right of the
  status line) rather than changing appearance in place.

**Never** draw a spinner, a progress animation, or an indeterminate bar. Use a
discrete count ("3 of 8") if progress must be shown.

## Typography

- Body/header text: `10x20` (20 px). Meta (axis labels, sample count, status
  hints): `6x10`. Rows: `9x15B` when density matters, `helvB18` for a headline.
- Left-align prose. Right-align numbers in a fixed-width slot (`draw_right`)
  so a value growing `9.9 → 10.1` does not reflow its neighbours.
- Clip by **measured** width (`u8g2_GetStrWidth`), not character count; u8g2
  is un-kerned so the measure is exact.
- Long strings: truncate with a marker the active font can draw
  (`u8g2_IsGlyph(0x2026)` → `…`, else `...`) and always mark the final shown
  line of a wrapped block.

## Layout defaults (400×300 landscape)

- Outer margin 4 px; header baseline 22; status/meta baseline 40; a 1 px rule
  under the header at y≈48; plot box from y=66 to y=250.
- Chart: left gutter for the temperature scale, right gutter for humidity;
  x-axis labels below the frame, first left-aligned, middle centred, last
  right-aligned. End-of-line values sit on a **paper knockout box** so a trace
  cannot strike through the digits.
- Keep volatile values out of reflow: fixed-width slots, stable gutters.
- No scrolling on this panel. Prefer a page/one-shot render over a feed; the
  LVGL transcript scroller is the one exception and it must bottom-snap so the
  newest chunk is what stays visible.

## Anti-patterns (rewrite on sight)

- A mid-grey or `LV_OPA_*` fill used for meaning.
- The same reading drawn twice at two sizes.
- A status word with no shape channel, or a shape with no word.
- Every row boxed; nested frames.
- Axis labels colliding with end-of-line values (draw the values last, on
  knockout boxes, and push them apart when they nearly coincide).
- Assuming a glyph exists (degree sign, ellipsis, CJK) without probing.
- A placeholder reading (`0 °C`) when the server sent none — draw nothing.

## Verification loop (pixel-driven — this is the design review)

The panel is captured **exactly**, so a design change is verified by reading
pixels back, not by reasoning about code:

```sh
# local board
python3 robodog_client/capture.py --port /dev/ttyACM0 --reset --n 2 --describe

# the bench (op4) owns the board; it builds, flashes and captures
bench submit build --family home --head <sha> --class dev
bench submit flash_capture --family home --head <sha> --build <build-id> \
    --payload '{"device_name":"study room","frames":6}'
bench wait <job-id>; bench evidence <job-id>
```

Acceptance for a UI change: the landscape PNG is **upright** (the RD_SHOT
header carries the logical `w`/`h`; decode by those, not by buffer size), the
primary element is the largest thing on the panel, every state word has a
shape, and the vision readback transcribes the intended text without inventing
status words.

## Where the design lives

- `robodog_client/esp-claw/DESIGN.md` — the LVGL/ESP-Claw presentation contract.
- `robodog_home_client/README.md` — the env chart's documented layout.
- `robodog_client/README.md` — the calendar panel's wire + render contract.
