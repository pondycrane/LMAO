/*
 * RoboDog Phase 0 overlay — persistent conversation session (LMAO-owned).
 *
 * NVS-backed conversation identity (see robodog_session.h).  The session id
 * is a random hex string minted once and reused across turns/reboots so the
 * agent carries context; a long-press clear (rd_session reset / the board
 * button) mints a fresh id.  The friendly name is word-truncated from the
 * first question.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_session.h"

#include "robodog_ui.h"

#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "claw_agent_mgr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "nvs_flash.h"

static const char *TAG = "robodog_session";

#define SESSION_NS        "robodog"
#define SESSION_ID_KEY    "session_id"
#define SESSION_TITLE_KEY "session_title"
#define SESSION_ID_LEN    64   /* buffer size; we mint 12 hex chars (48 bits) */
#define SESSION_TITLE_LEN 32   /* buffer size; friendly word-truncated ~22 */

static char s_id[SESSION_ID_LEN];
static char s_title[SESSION_TITLE_LEN];
static bool s_initialized;

/* ── NVS helpers ─────────────────────────────────────────────────────────── */

static nvs_handle_t session_nvs_open(void)
{
    nvs_handle_t h = 0;
    if (nvs_open(SESSION_NS, NVS_READWRITE, &h) != ESP_OK) {
        /* NVS not ready (RAM-only session this boot — IDs still unique per
         * boot, just not persisted). */
        ESP_LOGW(TAG, "NVS unavailable; session will not survive reboot");
        return 0;
    }
    return h;
}

static void load_or_init(void)
{
    memset(s_id, 0, sizeof(s_id));
    memset(s_title, 0, sizeof(s_title));
    nvs_handle_t h = session_nvs_open();
    if (h != 0) {
        size_t len = sizeof(s_id);
        nvs_get_str(h, SESSION_ID_KEY, s_id, &len);
        len = sizeof(s_title);
        nvs_get_str(h, SESSION_TITLE_KEY, s_title, &len);
        nvs_close(h);
    }
    if (s_id[0] == '\0') {
        /* Mint a fresh 12-hex-char id (48 bits — collision-negligible). */
        char tmp[16];
        uint32_t a = esp_random();
        uint32_t b = esp_random();
        snprintf(tmp, sizeof(tmp), "%08lx%04lx",
                 (unsigned long)a, (unsigned long)(b & 0xFFFF));
        memcpy(s_id, tmp, 12);
        s_id[12] = '\0';
        nvs_handle_t w = session_nvs_open();
        if (w != 0) {
            nvs_set_str(w, SESSION_ID_KEY, s_id);
            nvs_commit(w);
            nvs_close(w);
        }
        ESP_LOGI(TAG, "minted new session id %s", s_id);
    } else {
        ESP_LOGI(TAG, "loaded session id %s%s%s", s_id,
                 s_title[0] ? " title \"" : "", s_title[0] ? s_title : "");
    }
    robodog_ui_set_session(s_title[0] ? s_title : s_id);
    s_initialized = true;
}

static void persist_id(void)
{
    nvs_handle_t h = session_nvs_open();
    if (h != 0) {
        nvs_set_str(h, SESSION_ID_KEY, s_id);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void persist_title(void)
{
    nvs_handle_t h = session_nvs_open();
    if (h != 0) {
        nvs_set_str(h, SESSION_TITLE_KEY, s_title);
        nvs_commit(h);
        nvs_close(h);
    }
}

/* ── Public API ──────────────────────────────────────────────────────────── */

esp_err_t robodog_session_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }
    load_or_init();
    return ESP_OK;
}

const char *robodog_session_id(void)
{
    return s_id;
}

const char *robodog_session_title(void)
{
    return s_title;
}

esp_err_t robodog_session_note_question(const char *question)
{
    if (question == NULL || question[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_title[0] != '\0') {
        return ESP_OK; /* already titled by the first question */
    }
    /* ASCII-safe friendly name: skip leading whitespace, copy up to a
     * word boundary within an 18-char display cap (fits the centered
     * top-middle label at font16), never splitting a word. */
    enum { TITLE_CAP = 18 };
    const char *src = question;
    while (*src != '\0' && isspace((unsigned char)*src)) {
        src++;
    }
    size_t i = 0;
    size_t last_break = 0;
    while (src[i] != '\0' && i + 1 <= TITLE_CAP) {
        const unsigned char c = (unsigned char)src[i];
        if (c < 0x80 && isalnum(c)) {
            s_title[i] = (char)c;
        } else if (isspace(c)) {
            s_title[i] = ' ';
            last_break = i;
        } else {
            break; /* punctuation / non-ASCII ends the title */
        }
        i++;
    }
    s_title[i] = '\0';
    if (i == 0) {
        /* nothing titleable — fall back to a short id form */
        snprintf(s_title, sizeof(s_title), "S#%.6s", s_id);
    } else if (i == TITLE_CAP && last_break > 0) {
        /* hit the cap mid-word: truncate at the last word boundary */
        s_title[last_break] = '\0';
    }
    persist_title();
    robodog_ui_set_session(s_title);
    ESP_LOGI(TAG, "session titled \"%s\" (id %s)", s_title, s_id);
    return ESP_OK;
}

esp_err_t robodog_session_reset(void)
{
    if (s_id[0] != '\0') {
        /* Best-effort: our id may not be a registered catalog id (upstream
         * maps catalog <-> session), so NOT_FOUND / busy are expected — the
         * FRESH id below is what really starts a clean session. */
        esp_err_t d = claw_agent_mgr_delete_session(s_id);
        if (d != ESP_OK) {
            ESP_LOGW(TAG, "agent session delete (best-effort) %s: %s", s_id,
                     esp_err_to_name(d));
        }
    }
    nvs_handle_t h = session_nvs_open();
    if (h != 0) {
        nvs_erase_key(h, SESSION_ID_KEY);
        nvs_erase_key(h, SESSION_TITLE_KEY);
        nvs_commit(h);
        nvs_close(h);
    }
    s_title[0] = '\0';
    s_id[0] = '\0';
    load_or_init(); /* mints + persists the fresh id, refreshes the UI */
    ESP_LOGI(TAG, "rd_session: reset -> %s", s_id);
    return ESP_OK;
}
