// Pure C++ calendar event selection/formatting — port of the logic in
// robodog_client/ui.py (_select_events, _fmt_hhmm, _default_header).
// Host-testable: no LVGL / ESP-IDF includes.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <string>
#include <vector>

// The UI layer owns its event type; wire codecs (PR 2: framed-TCP GetCalendar
// / MXF envelope) map onto it. No protocol dependency in the UI framework.
namespace calendar_model {

// The UI layer owns its event type; wire codecs (PR 2: framed-TCP GetCalendar
// / LMAO envelope) map onto it. No protocol dependency in the UI framework.
struct UiEvent {
    std::string uid, title, notes, pic;
    uint64_t start_ms = 0, end_ms = 0;
    bool deleted = false;
};

constexpr size_t MAX_ROWS = 6;

// Drop deleted / blank-title / already-over events, sort by start_ms
// ascending (stable), cap at max_rows. now_ms==0 disables the "past" filter.
std::vector<UiEvent> select_events(
    const std::vector<UiEvent>& events, uint64_t now_ms,
    size_t max_rows = MAX_ROWS);

// "HH:MM" (UTC) for an epoch-ms timestamp, or "" when ms == 0 / invalid.
std::string fmt_hhmm(uint64_t ms);

// "YYYY-MM-DD  HH:MM" (UTC) for an epoch-ms timestamp, or "FAMILY CALENDAR"
// when now_ms == 0 / invalid.
std::string header_text(uint64_t now_ms);

}  // namespace calendar_model
