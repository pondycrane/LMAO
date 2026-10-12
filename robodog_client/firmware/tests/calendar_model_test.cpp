// Host unit test for the RoboDog calendar event-selection/formatting model
// (firmware/main/calendar_model.cpp) — a port of the semantics of the old
// MicroPython renderer (robodog_client/ui.py _select_events / _fmt_hhmm /
// _default_header), exercised here exactly as the device firmware compiles it.
#include <cstdio>
#include <string>
#include <vector>

#include "calendar_model.h"

using lma_calendar::CalEvent;

static int failures = 0;

#define CHECK_TRUE(cond, label)                                              \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::printf("FAIL %s\n", label);                                 \
            failures++;                                                      \
        } else {                                                             \
            std::printf("ok   %s\n", label);                                 \
        }                                                                    \
    } while (0)

static CalEvent ev(const char* title, uint64_t start_ms, uint64_t end_ms,
                   bool deleted = false, const char* pic = "") {
    CalEvent e;
    e.title = title;
    e.start_ms = start_ms;
    e.end_ms = end_ms;
    e.deleted = deleted;
    e.pic = pic;
    return e;
}

int main() {
    // ---- fmt_hhmm / header_text (UTC; 1700000000000 ms = 2023-11-14 22:13:20Z)
    CHECK_TRUE(calendar_model::fmt_hhmm(1700000000000ULL) == "22:13",
               "fmt_hhmm formats UTC HH:MM");
    CHECK_TRUE(calendar_model::fmt_hhmm(0) == "", "fmt_hhmm(0) is empty");
    CHECK_TRUE(calendar_model::header_text(1700000000000ULL) == "2023-11-14  22:13",
               "header_text formats date + time");
    CHECK_TRUE(calendar_model::header_text(0) == "FAMILY CALENDAR",
               "header_text(0) falls back to the banner");

    const uint64_t now = 1700000000000ULL;

    // ---- selection: deleted / blank-title / past events dropped
    std::vector<CalEvent> in = {
        ev("kept-future", now + 1000, now + 2000),
        ev("deleted", now + 1000, now + 2000, /*deleted=*/true),
        ev("", now + 1000, now + 2000),
        ev("   ", now + 1000, now + 2000),
        ev("past", now - 2000, now - 1000),
    };
    auto out = calendar_model::select_events(in, now);
    CHECK_TRUE(out.size() == 1 && out[0].title == "kept-future",
               "deleted/blank/past events are dropped");

    // ---- end_ms==0 events are kept regardless of start (parity with ui.py:
    //      the past-check requires both now_ms and end_ms)
    in = {ev("no-end", now - 5000, 0)};
    out = calendar_model::select_events(in, now);
    CHECK_TRUE(out.size() == 1, "end_ms==0 event is kept");

    // ---- now_ms==0 disables the past filter (boot before time sync)
    in = {ev("old", 1000, 2000)};
    out = calendar_model::select_events(in, 0);
    CHECK_TRUE(out.size() == 1, "now_ms==0 keeps past events");

    // ---- sorted by start_ms ascending
    in = {
        ev("third", now + 3000, now + 4000),
        ev("first", now + 1000, now + 2000),
        ev("second", now + 2000, now + 3000),
    };
    out = calendar_model::select_events(in, now);
    CHECK_TRUE(out.size() == 3 && out[0].title == "first" &&
                   out[1].title == "second" && out[2].title == "third",
               "events sorted by start_ms");

    // ---- capped at max_rows (default 6)
    in.clear();
    for (int i = 0; i < 9; i++) {
        std::string t = "e" + std::to_string(i);
        in.push_back(ev(t.c_str(), now + 1000 * (i + 1), now + 1000 * (i + 1) + 1));
    }
    out = calendar_model::select_events(in, now);
    CHECK_TRUE(out.size() == calendar_model::MAX_ROWS, "rows capped at MAX_ROWS");
    CHECK_TRUE(out.back().title == "e5", "cap keeps the earliest events");
    out = calendar_model::select_events(in, now, 3);
    CHECK_TRUE(out.size() == 3 && out.back().title == "e2", "explicit max_rows cap");

    if (failures) {
        std::printf("%d FAILURES\n", failures);
        return 1;
    }
    std::printf("all calendar_model tests passed\n");
    return 0;
}
