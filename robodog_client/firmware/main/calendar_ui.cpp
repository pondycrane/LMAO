// calendar_ui.cpp — LVGL 9 Subjects + declarative XML screen glue.
//
// BUILD REQUIREMENT (scaffold): main/CMakeLists.txt must embed the screen
// definition with
//     idf_component_register(... EMBED_TXTFILES ui_xml/calendar_screen.xml)
// so the _binary_calendar_screen_xml_start symbol below resolves
// (EMBED_TXTFILES NUL-terminates the blob, so it can be passed straight to
// lv_xml_component_register_from_data()). ui_xml/calendar_screen.xml is the
// source of truth for the layout; LVGL must be configured with LV_USE_XML=1
// and LV_FONT_MONTSERRAT_14/20 (Kconfig, scaffold-owned).

#include "calendar_ui.h"

#include <stdint.h>
#include <stdio.h>

#include "calendar_model.h"
#include "lvgl.h"
#include "st7305_disp.h"

// ESP-IDF EMBED_TXTFILES symbols for ui_xml/calendar_screen.xml.
extern const uint8_t calendar_screen_xml_start[] asm(
    "_binary_calendar_screen_xml_start");

#if LV_FONT_MONTSERRAT_14
LV_FONT_DECLARE(lv_font_montserrat_14);
#endif
#if LV_FONT_MONTSERRAT_20
LV_FONT_DECLARE(lv_font_montserrat_20);
#endif

namespace {

constexpr size_t kRows = calendar_model::MAX_ROWS;  // 6

// Subject names must match the bindings in ui_xml/calendar_screen.xml.
lv_subject_t s_header_text;
lv_subject_t s_status_text;
lv_subject_t s_row_title[kRows];
lv_subject_t s_row_time[kRows];
lv_subject_t s_row_present[kRows];
lv_subject_t s_row_pic[kRows];

// lv_subject_init_string() copies into caller-owned buffers (persistent).
char s_header_buf[64];
char s_status_buf[16];
char s_row_title_buf[kRows][96];
char s_row_time_buf[kRows][16];

bool s_initialized = false;

void create_and_register_subjects() {
    lv_subject_init_string(&s_header_text, s_header_buf, nullptr,
                           sizeof(s_header_buf), "FAMILY CALENDAR");
    lv_subject_init_string(&s_status_text, s_status_buf, nullptr,
                           sizeof(s_status_buf), "");
    for (size_t i = 0; i < kRows; i++) {
        lv_subject_init_string(&s_row_title[i], s_row_title_buf[i], nullptr,
                               sizeof(s_row_title_buf[i]), "");
        lv_subject_init_string(&s_row_time[i], s_row_time_buf[i], nullptr,
                               sizeof(s_row_time_buf[i]), "");
        lv_subject_init_int(&s_row_present[i], 0);
        lv_subject_init_int(&s_row_pic[i], 0);
    }

#if LV_USE_XML
    lv_xml_register_subject(nullptr, "header_text", &s_header_text);
    lv_xml_register_subject(nullptr, "status_text", &s_status_text);
    char name[24];
    for (size_t i = 0; i < kRows; i++) {
        snprintf(name, sizeof(name), "row%u_title", (unsigned)i);
        lv_xml_register_subject(nullptr, name, &s_row_title[i]);
        snprintf(name, sizeof(name), "row%u_time", (unsigned)i);
        lv_xml_register_subject(nullptr, name, &s_row_time[i]);
        snprintf(name, sizeof(name), "row%u_present", (unsigned)i);
        lv_xml_register_subject(nullptr, name, &s_row_present[i]);
        snprintf(name, sizeof(name), "row%u_pic", (unsigned)i);
        lv_xml_register_subject(nullptr, name, &s_row_pic[i]);
    }
#endif  // LV_USE_XML
}

void register_fonts() {
#if LV_USE_XML
    // Names must match style_text_font in ui_xml/calendar_screen.xml.
#if LV_FONT_MONTSERRAT_14
    lv_xml_register_font(nullptr, "lv_font_montserrat_14",
                         &lv_font_montserrat_14);
#endif
#if LV_FONT_MONTSERRAT_20
    lv_xml_register_font(nullptr, "lv_font_montserrat_20",
                         &lv_font_montserrat_20);
#endif
#endif  // LV_USE_XML
}

}  // namespace

namespace calendar_ui {

bool init() {
    if (s_initialized) return true;
    if (!st7305_disp_init()) return false;

    lv_lock();
    create_and_register_subjects();
    register_fonts();
    lv_obj_t* screen = nullptr;
#if LV_USE_XML
    if (lv_xml_component_register_from_data(
            "calendar_screen",
            reinterpret_cast<const char*>(calendar_screen_xml_start)) ==
        LV_RESULT_OK) {
        screen =
            (lv_obj_t*)lv_xml_create(lv_screen_active(), "calendar_screen",
                                     nullptr);
    }
#endif  // LV_USE_XML
    lv_unlock();

    if (screen == nullptr) return false;
    s_initialized = true;
    return true;
}

void set_header(const char* text) {
    if (!s_initialized) return;
    lv_lock();
    lv_subject_copy_string(&s_header_text, text != nullptr ? text : "");
    lv_unlock();
}

void set_offline(bool offline) {
    if (!s_initialized) return;
    lv_lock();
    lv_subject_copy_string(&s_status_text, offline ? "OFFLINE" : "");
    lv_unlock();
}

void set_events(const calendar_model::UiEvent* events, size_t count) {
    if (!s_initialized) return;
    if (events == nullptr) count = 0;
    if (count > kRows) count = kRows;

    // Net callbacks arrive from another task: poke subjects under the lock.
    lv_lock();
    for (size_t i = 0; i < kRows; i++) {
        if (i < count) {
            const calendar_model::UiEvent& ev = events[i];
            const std::string hhmm = calendar_model::fmt_hhmm(ev.start_ms);
            lv_subject_copy_string(&s_row_title[i], ev.title.c_str());
            lv_subject_copy_string(&s_row_time[i], hhmm.c_str());
            lv_subject_set_int(&s_row_pic[i], ev.pic.empty() ? 0 : 1);
            lv_subject_set_int(&s_row_present[i], 1);
        } else {
            lv_subject_copy_string(&s_row_title[i], "");
            lv_subject_copy_string(&s_row_time[i], "");
            lv_subject_set_int(&s_row_pic[i], 0);
            lv_subject_set_int(&s_row_present[i], 0);
        }
    }
    lv_unlock();
}

}  // namespace calendar_ui
