/*
 * rd_render.h -- ST7305 rendering for the RoboDog calendar panel.
 *
 * The renderer owns the display driver, the display clock (derived from the
 * server's epoch-ms watermark; the board has no RTC/SNTP) and the RD_SHOT
 * capture dump.  Panel geometry lives in rd_layout.h so the layout can be
 * host-tested; this module only draws at those positions.
 */
#ifndef RD_RENDER_H
#define RD_RENDER_H

#include <stdbool.h>
#include <stdint.h>

#include "rd_store.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    RD_STATE_FRESH,   /* synced just now */
    RD_STATE_STALE,   /* connected but sync failed, showing cache */
    RD_STATE_OFFLINE, /* no wifi, showing cache (or an offline note) */
} rd_state_t;

/* Initialise the ST7305 + u8g2 full-frame buffer. */
bool rd_render_init(void);

/* Pin the display's SPI pins to their idle level and keep them there across
 * light sleep (gpio_sleep_sel_dis), so the ST7305 is never reset or clocked
 * by the sleep state machine and its GRAM -- the picture -- survives the
 * hold.  Call once after rd_render_init(). */
void rd_render_sleep_prepare(void);

/* Set the display clock base from the server's watermark (epoch ms). */
void rd_render_set_clock(uint64_t utc_ms);

/* Current display time in epoch ms, 0 before the first sync. */
uint64_t rd_render_now_ms(void);

/* Draw the whole panel for `state` from `cache`, push it to the panel and
 * (when a USB host is attached) stream the exact frame as RD_SHOT.  `cfg`
 * supplies the calendar identity label (`family`, or the kid name on a kid
 * panel, drawn LEFT of the clock) and `current` the conditions from the last
 * reply (RAM only; never persisted, so a boot render shows none). */
void rd_render_screen(rd_state_t state, const rd_config_t *cfg,
                      const lma_weather_t *current, const rd_cache_t *cache);

#ifdef __cplusplus
}
#endif

#endif /* RD_RENDER_H */
