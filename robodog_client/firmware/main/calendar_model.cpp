#include "calendar_model.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <ctime>

namespace calendar_model {

namespace {

bool blank_title(const std::string& title) {
    // Mirrors Python's str.strip(): whitespace-only counts as blank.
    for (unsigned char c : title) {
        if (!std::isspace(c)) return false;
    }
    return true;
}

bool break_down(uint64_t ms, int tz_min, std::tm* out) {
    // Display-local time: epoch ms shifted by the given zone offset (minutes
    // east of UTC), then broken down with gmtime_r — exact POSIX localtime()
    // for a fixed offset.
    const time_t t = static_cast<time_t>(ms / 1000) +
                     static_cast<time_t>(tz_min) * 60;
    return gmtime_r(&t, out) != nullptr;
}

}  // namespace

std::vector<calendar_model::UiEvent> select_events(
    const std::vector<calendar_model::UiEvent>& events, uint64_t now_ms,
    size_t max_rows) {
    std::vector<calendar_model::UiEvent> kept;
    kept.reserve(events.size());
    for (const calendar_model::UiEvent& ev : events) {
        if (ev.deleted) continue;
        if (blank_title(ev.title)) continue;
        if (now_ms && ev.end_ms && ev.end_ms < now_ms) {
            continue;  // event is already over
        }
        kept.push_back(ev);
    }
    // Python's list.sort() is stable; keep that observable behaviour.
    std::stable_sort(kept.begin(), kept.end(),
                     [](const calendar_model::UiEvent& a,
                        const calendar_model::UiEvent& b) {
                         return a.start_ms < b.start_ms;
                     });
    if (kept.size() > max_rows) kept.resize(max_rows);
    return kept;
}

std::string fmt_hhmm(uint64_t ms, int tz_min) {
    if (!ms) return "";
    std::tm tm_local;
    if (!break_down(ms, tz_min, &tm_local)) return "";
    char buf[8];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", tm_local.tm_hour,
                  tm_local.tm_min);
    return buf;
}

std::string header_text(uint64_t now_ms, int tz_min) {
    if (!now_ms) return "FAMILY CALENDAR";
    std::tm tm_local;
    if (!break_down(now_ms, tz_min, &tm_local)) return "FAMILY CALENDAR";
    char buf[64];  // worst case needs room for full int-width tm fields
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d  %02d:%02d",
                  tm_local.tm_year + 1900, tm_local.tm_mon + 1,
                  tm_local.tm_mday, tm_local.tm_hour, tm_local.tm_min);
    return buf;
}

}  // namespace calendar_model
