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

bool break_down(uint64_t ms, std::tm* out) {
    // MicroPython localtime() of the RTC is UTC on the device; gmtime_r is
    // the exact POSIX equivalent.
    const time_t t = static_cast<time_t>(ms / 1000);
    return gmtime_r(&t, out) != nullptr;
}

}  // namespace

std::vector<lma_calendar::CalEvent> select_events(
    const std::vector<lma_calendar::CalEvent>& events, uint64_t now_ms,
    size_t max_rows) {
    std::vector<lma_calendar::CalEvent> kept;
    kept.reserve(events.size());
    for (const lma_calendar::CalEvent& ev : events) {
        if (ev.deleted) continue;
        if (blank_title(ev.title)) continue;
        if (now_ms && ev.end_ms && ev.end_ms < now_ms) {
            continue;  // event is already over
        }
        kept.push_back(ev);
    }
    // Python's list.sort() is stable; keep that observable behaviour.
    std::stable_sort(kept.begin(), kept.end(),
                     [](const lma_calendar::CalEvent& a,
                        const lma_calendar::CalEvent& b) {
                         return a.start_ms < b.start_ms;
                     });
    if (kept.size() > max_rows) kept.resize(max_rows);
    return kept;
}

std::string fmt_hhmm(uint64_t ms) {
    if (!ms) return "";
    std::tm tm_utc;
    if (!break_down(ms, &tm_utc)) return "";
    char buf[8];
    std::snprintf(buf, sizeof(buf), "%02d:%02d", tm_utc.tm_hour,
                  tm_utc.tm_min);
    return buf;
}

std::string header_text(uint64_t now_ms) {
    if (!now_ms) return "FAMILY CALENDAR";
    std::tm tm_utc;
    if (!break_down(now_ms, &tm_utc)) return "FAMILY CALENDAR";
    char buf[64];  // worst case needs room for full int-width tm fields
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d  %02d:%02d",
                  tm_utc.tm_year + 1900, tm_utc.tm_mon + 1, tm_utc.tm_mday,
                  tm_utc.tm_hour, tm_utc.tm_min);
    return buf;
}

}  // namespace calendar_model
