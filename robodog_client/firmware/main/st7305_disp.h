// ST7305 300x400 1-bit reflective LCD driver + LVGL 9 display glue for the
// Waveshare ESP32-S3 RLCD-4.2 board.
//
// LVGL renders a landscape 400x300 surface (st7305_disp_get returns that
// display); the flush callback transposes it into the portrait 300x400 frame
// the banded panel push drives. Landscape mirrors the reference RLCD-4.2 demo.
//
// Port of robodog_client/st7305.py (itself a faithful port of Waveshare's
// u8g2_st7305.c — same init command table and 4x4-LUT 8x8-tile column remap).
//
// NOTE: after st7305_disp_init() the application task must call
// lv_timer_handler() periodically (under lv_lock()) to drive rendering and
// flushing; the LVGL tick itself is sourced from esp_timer here.
#pragma once

#ifndef __cplusplus
#include <stdbool.h>
#endif

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

// Initialise GPIO, the SPI master, the panel (full init sequence), LVGL and
// the LVGL display. Idempotent; returns false on the first failing step.
bool st7305_disp_init(void);

// The LVGL display created by st7305_disp_init(), or NULL before init.
lv_display_t* st7305_disp_get(void);

#ifdef __cplusplus
}
#endif
