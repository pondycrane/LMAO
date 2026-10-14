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

#include "esp_log.h"

#include "calendar_model.h"
#include "config.h"  // RD_TZ_OFFSET_MINUTES
#include <string.h>
#include <vector>

#include "lvgl.h"
#include "lma_proto.h"   // LMA_PB_PIC_TILE(_BYTES) — same tile contract
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
lv_subject_t s_row_pic[kRows];       /* int 0/1: tile present */

// Static descriptors over the cached tile bytes filled by set_events().
// I1 1bpp tiles come over the wire from lma_proto (32x32, MSB first,
// bit 1 = ink — the same contract the u8g2 build rendered with).
lv_image_dsc_t s_row_tile_dsc[kRows];

// Tile buffers are the socle the C core owns (s_cache[i].pic_data) — but the
// swap-in place in robodog_core invalidates bytes, so calendar_ui keeps its
// own copies.
static uint8_t s_pic_tile_copy[kRows][LMA_PB_PIC_TILE_BYTES];


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
        memset(&s_row_tile_dsc[i], 0, sizeof(lv_image_dsc_t));
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

/* The six XML pic boxes resolved after instantiation: LVML has no src
 * binding, so apply_tile() points the widgets at the descriptors in C. */
lv_obj_t* s_row_img[kRows];
static size_t s_img_idx = 0;

static lv_obj_tree_walk_res_t img_collector(lv_obj_t* obj, void*) {
    if (s_img_idx < kRows && lv_obj_check_type(obj, &lv_image_class)) {
        s_row_img[s_img_idx++] = obj;
    }
    return LV_OBJ_TREE_WALK_NEXT;
}

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
    if (screen != nullptr) {
        // Rows are emitted row0..row5 in the XML, so walk order == row order.
        s_img_idx = 0;
        lv_obj_tree_walk(screen, img_collector, nullptr);
    }
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
            const std::string hhmm =
                calendar_model::fmt_hhmm(ev.start_ms, RD_TZ_OFFSET_MINUTES);
            lv_subject_copy_string(&s_row_title[i], ev.title.c_str());
            lv_subject_copy_string(&s_row_time[i], hhmm.c_str());
            lv_subject_set_int(&s_row_present[i], 1);
        } else {
            lv_subject_copy_string(&s_row_title[i], "");
            lv_subject_copy_string(&s_row_time[i], "");
            lv_subject_set_int(&s_row_pic[i], 0);
            if (s_row_img[i] != nullptr) {
                lv_obj_add_flag(s_row_img[i], LV_OBJ_FLAG_HIDDEN);
            }
            lv_subject_set_int(&s_row_pic[i], 0);
            lv_subject_set_int(&s_row_present[i], 0);
        }
    }
    lv_unlock();
}

/* Push the C core's 1bpp 32x32 tile (lma_proto pic_data) into the row's
 * pointer-subject image descriptor. I1 payload per lma_proto.h contract:
 * MSB first, bit 1 = ink — matches LV_COLOR_FORMAT_I1 rendering here.
 * The XML binds row{i}_img (pointer subject) — set_events_data below. */
static void apply_tile(size_t i, const uint8_t* tile, size_t len) {
    memset(&s_row_tile_dsc[i], 0, sizeof(lv_image_dsc_t));
    if (tile != nullptr && len == LMA_PB_PIC_TILE_BYTES) {
        memcpy(s_pic_tile_copy[i], tile, len);
        s_row_tile_dsc[i].header.cf = LV_COLOR_FORMAT_I1;
        s_row_tile_dsc[i].header.w  = LMA_PB_PIC_TILE;
        s_row_tile_dsc[i].header.h  = LMA_PB_PIC_TILE;
        s_row_tile_dsc[i].header.stride = LMA_PB_PIC_TILE / 8;
        s_row_tile_dsc[i].header.magic  = LV_IMAGE_HEADER_MAGIC;
        s_row_tile_dsc[i].data = s_pic_tile_copy[i];
        s_row_tile_dsc[i].data_size = len;
        if (s_row_img[i] != nullptr) {
            /* LVML has no runtime src binding — set the widget source in C */
            lv_image_set_src(s_row_img[i], &s_row_tile_dsc[i]);
            lv_obj_invalidate(s_row_img[i]);
        }
        lv_subject_set_int(&s_row_pic[i], 1);  /* XML shows the box */
    } else if (s_row_img[i] != nullptr) {
        lv_obj_add_flag(s_row_img[i], LV_OBJ_FLAG_HIDDEN);
        lv_subject_set_int(&s_row_pic[i], 0);
    }
}

}  // namespace calendar_ui

/* C-shim entry the robodog_core.c sync loop calls after each successful
 * fetch: pushes the whole cache (title/time/present/pic subjects..) through
 * set_events and injects one tile per event with pic data present. */
void set_events_data(const lma_cal_event_t* events, size_t count,
                     uint64_t now_ms, bool stale_badge) {
    if (!s_initialized) return;

    using namespace calendar_ui;

    // Map wire events onto the UI filter/sort model and swap into the rows.
    std::vector<calendar_model::UiEvent> ui;
    ui.reserve(count);
    for (size_t i = 0; i < count && i < LMA_PB_EVENT_MAX; i++) {
        calendar_model::UiEvent e;
        e.uid = events[i].uid;
        e.title = events[i].title;
        e.notes = events[i].notes;
        e.pic = events[i].pic;
        e.start_ms = events[i].start_ms;
        e.end_ms = events[i].end_ms;
        e.deleted = events[i].deleted;
        if (events[i].pic_data_len > 0) {
            e.pic = events[i].pic;  // non-empty: box binding stays visible
        }
        ui.push_back(e);
    }
    auto out = calendar_model::select_events(ui, now_ms);
    const std::string hdr =
        calendar_model::header_text(now_ms, RD_TZ_OFFSET_MINUTES);
    set_header(hdr.c_str());
    set_offline(stale_badge);
    set_events(out.data(), out.size());

    // Tiles: push the C-core's tile bytes for whichever rows made the cut,
    // matching them by loro uid order (select_events preserves order of ui).
    for (size_t i = 0; i < out.size() && i < kRows; i++) {
        for (size_t j = 0; j < count && j < LMA_PB_EVENT_MAX; j++) {
            if (strcmp(events[j].uid, out[i].uid.c_str()) == 0) {
                ESP_LOGI("calui", "row %u uid=%s pic_len=%u", (unsigned)i,
                         events[j].uid, (unsigned)events[j].pic_data_len);
                apply_tile(i, events[j].pic_data, events[j].pic_data_len);
                break;
            }
        }
    }
}

