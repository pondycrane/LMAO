// LVGL 9 declarative UI layer for the RoboDog family-calendar client.
// Owns the LVGL subjects bound by ui_xml/calendar_screen.xml and exposes the
// small imperative API used by the application/net layer.
#pragma once

#include <stddef.h>

#include "lma_calendar.h"

namespace calendar_ui {

// Brings up the panel + LVGL display (st7305_disp_init), creates and
// registers all subjects, registers the embedded calendar_screen XML
// component and instantiates it on the active screen. Idempotent.
bool init();

// Header band text (e.g. calendar_model::header_text(now_ms)).
void set_header(const char* text);

// Shows "OFFLINE" in the header band when offline, clears it otherwise.
void set_offline(bool offline);

// Fills rows 0..count-1 from the pre-filtered event list (<= 6, see
// calendar_model::select_events) and clears the remaining rows.
void set_events(const lma_calendar::CalEvent* events, size_t count);

}  // namespace calendar_ui
