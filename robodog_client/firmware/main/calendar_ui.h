// LVGL 9 declarative UI layer for the RoboDog family-calendar client.
// Owns the LVGL subjects bound by ui_xml/calendar_screen.xml and exposes the
// small imperative API used by the application/net layer.
//
// robodog_core.c (C) only sees the extern "C" shim at the bottom; everything
// else is C++.
#pragma once

#include <stddef.h>

#include "lma_proto.h"

#ifdef __cplusplus

#include <cstdint>

#include "calendar_model.h"

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
void set_events(const calendar_model::UiEvent* events, size_t count);

}  // namespace calendar_ui

// C shim: robodog_core.c's sync loop calls this after each (successful or
// cached) fetch. state: 0 = fresh, 1 = stale, 2 = offline — the header-band
// OFFLINE status shows when != 0 and events were not re-confirmed.
extern "C"
void set_events_data(const lma_cal_event_t *events, size_t count,
                     uint64_t now_ms, bool stale_badge);

#endif /* __cplusplus */
