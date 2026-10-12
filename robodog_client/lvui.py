"""LVGL calendar UI for the RoboDog ST7305 panel (MicroPython).

Rich 1-bit B&W family-calendar screen:
  * header band: left label = date/time ("2026-10-12  14:30") or the "FAMILY
    CALENDAR" fallback whenever no fresh time is known; right label = status
    ("OFFLINE" while the sync loop has nothing fresh).
  * six event rows, each a container with the event title (big font) and the
    start time underneath.
The API mirrors the old ``robodog_client/ui.py`` so ``main.py`` swaps
renderers with the same call signature.
"""
import time

import lvgl as lv  # NOQA

MAX_ROWS = 6


class CalendarScreen:
    """The whole RoboDog screen in one object."""

    def __init__(self, disp):
        self.disp = disp
        self.rows = []

        scr = lv.screen_active()
        scr.set_style_bg_color(lv.color_hex(0xFFFFFF), 0)
        scr.set_style_text_color(lv.color_hex(0x000000), 0)
        scr.set_style_pad_all(4)
        scr.set_style_layout(lv.LAYOUT_FLEX)
        scr.set_style_flex_flow(lv.FLEX_FLOW.COLUMN, 0)
        scr.set_style_pad_row(0)

        # ---- header band ------------------------------------------------
        header = lv.obj(scr)
        header.remove_style_all()
        header.set_size(lv.pct(100), 28)
        header.set_style_layout(lv.LAYOUT_FLEX)
        header.set_style_flex_flow(lv.FLEX_FLOW.ROW, 0)
        header.set_style_flex_main_place(lv.FLEX_ALIGN.SPACE_BETWEEN, 0)
        header.set_style_flex_cross_place(lv.FLEX_ALIGN.CENTER, 0)
        self._header_label = lv.label(header)
        self._header_label.set_style_text_font(lv.font_montserrat_14, 0)
        self._header_label.set_text("FAMILY CALENDAR")
        self._status_label = lv.label(header)
        self._status_label.set_style_text_font(lv.font_montserrat_14, 0)
        self._status_label.set_text("")

        # separator line
        sep = lv.obj(scr)
        sep.remove_style_all()
        sep.set_size(lv.pct(100), 1)
        sep.set_style_bg_color(lv.color_hex(0x000000), 0)
        sep.set_style_bg_opa(lv.OPA.COVER, 0)

        # ---- six event rows ---------------------------------------------
        for _ in range(MAX_ROWS):
            row = lv.obj(scr)
            row.remove_style_all()
            row.set_size(lv.pct(100), 58)
            row.set_style_layout(lv.LAYOUT_FLEX)
            row.set_style_flex_flow(lv.FLEX_FLOW.ROW, 0)
            row.set_style_flex_main_place(lv.FLEX_ALIGN.SPACE_BETWEEN, 0)
            row.set_style_flex_cross_place(lv.FLEX_ALIGN.CENTER, 0)
            row.set_style_pad_column(8)
            col = lv.obj(row)
            col.remove_style_all()
            col.set_style_layout(lv.LAYOUT_FLEX)
            col.set_style_flex_flow(lv.FLEX_FLOW.COLUMN, 0)
            col.set_style_flex_grow(1)
            col.set_style_pad_row(6)
            title = lv.label(col)
            title.set_style_text_font(lv.font_montserrat_20, 0)
            title.set_long_mode(lv.LABEL_LONG.DOT)
            title.set_width(lv.pct(100))
            time_lbl = lv.label(col)
            time_lbl.set_style_text_font(lv.font_montserrat_14, 0)
            time_lbl.set_long_mode(lv.LABEL_LONG.CLIP)
            time_lbl.set_width(lv.pct(100))
            self.rows.append((row, title, time_lbl))

        lv.screen_load(scr)

    # ---- public API (mirrors ui.py) ----------------------------------------
    def set_header(self, text):
        self._header_label.set_text(text)

    def set_offline(self, offline):
        self._status_label.set_text("OFFLINE" if offline else "")

    def set_events(self, events_list):
        for row, title, time_lbl in self.rows:
            row.add_flag(lv.FLAG.HIDDEN)
        for i, ev in enumerate(events_list[:MAX_ROWS]):
            row, title, time_lbl = self.rows[i]
            title.set_text(ev.get("title", ""))
            ms = ev.get("start_ms", 0)
            try:
                lt = time.localtime(ms // 1000)
                hhmm = "%02d:%02d" % (lt[3], lt[4])
            except Exception:
                hhmm = ""
            time_lbl.set_text(hhmm)
            row.clear_flag(lv.FLAG.HIDDEN)
