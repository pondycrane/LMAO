/*
 * RoboDog Phase 3 — omni realtime voice path (see robodog_realtime.h).
 *
 * One WebSocket session to a vLLM-Omni OpenAI-Realtime endpoint replaces the
 * three serial HTTP hops of the Phase 2 loop.  The board streams its mic into
 * the session WHILE capturing (no utterance upload), commits when its own
 * energy VAD says the user stopped, and then asks for either
 *   - modalities ["audio","text"]: the omni model answers and speaks (fast
 *     conversational path), or
 *   - modalities ["text"]: transcript only, so claw_core answers with its
 *     skills/MCP/tools and Qwen3-TTS speaks (full-capability path).
 * esp-claw decides which, per turn (robodog_rt_route).
 *
 * Threading
 *   - esp_websocket_client owns the socket task; our event handler only
 *     assembles fragments and dispatches JSON.  It NEVER blocks on audio.
 *   - audio deltas are decoded (base64 -> PCM16) into fixed-size PSRAM buffers
 *     and queued; the playback task writes them to the shared APP mixer track.
 *     The mixer ring provides backpressure, so no fixed sleep pacing is
 *     needed (the Phase 2 19 ms-step loop ran ~5 % fast).
 *   - the turn itself runs on the voice task (same caller as Phase 2).
 *
 * Memory invariants: no allocation per delta beyond the fixed queue slots
 * (allocated once at init), so the Phase 2 R7 heap-churn class cannot recur.
 * A delta larger than CONFIG_ROBODOG_RT_DELTA_MAX_BYTES is dropped with a log
 * rather than growing a buffer mid-stream.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_realtime.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

#include "audio_capture.h"
#include "audio_hub.h"
#include "audio_mixer.h"
#include "esp_console.h"
#include "esp_websocket_client.h"

#include "robodog_speaker.h"
#include "robodog_ui.h"
#include "voice_pcm.h"
#include "voice_route.h"
#include "voice_think.h"

static const char *TAG = "robodog_rt";

/* ── tunables ───────────────────────────────────────────────────────────── */

#define RT_RATE_IN          16000   /* session input rate (model-mandated)   */
#define RT_RATE_OUT_DEFAULT 24000   /* used only when a delta omits the rate  */
#define RT_CH               1
#define RT_BITS             16
#define RT_FRAME_MS         20
#define RT_FRAME_SAMPLES    ((RT_RATE_IN * RT_FRAME_MS) / 1000)
#define RT_FRAME_BYTES      (RT_FRAME_SAMPLES * 2)
/* VAD identical to Phase 2 (voice_bridge parity): mean-abs amplitude > 300. */
#define RT_VAD_ENERGY       300
#define RT_VAD_MIN_SPEECH_MS 100
#define RT_NO_SPEECH_MS     12000
/* One audio delta is ~54 KB of JSON (base64 of ~40 KB of PCM16).  A
 * smaller buffer fragments it, and esp_websocket_client's fragment delivery
 * loses bytes when the handler does work between reads — measured as every
 * audio delta failing to parse.  Hold a whole message in one event. */
#define RT_WS_BUF           65536
#define RT_WS_TASK_STACK    8192
#define RT_WS_TIMEOUT_MS    10000
#define RT_PLAY_STACK       4096
#define RT_PLAY_PRIO        4
#define RT_STALL_LIMIT      200     /* consecutive empty writes before abort  */

/* Event bits */
#define RT_BIT_CONNECTED    BIT4    /* WEBSOCKET_EVENT_CONNECTED seen          */
#define RT_BIT_READY        BIT0    /* session.created seen                   */
#define RT_BIT_UPDATED      BIT5    /* session.updated seen (config applied)  */
#define RT_BIT_DONE         BIT1    /* response.done seen                     */
#define RT_BIT_ERR          BIT2    /* fatal session/protocol error           */

/* ── state ──────────────────────────────────────────────────────────────── */

typedef struct {
    uint8_t *data;      /* PSRAM, PCM16 */
    size_t   len;
    uint32_t rate;
} rt_audio_t;

static esp_websocket_client_handle_t s_ws;
static EventGroupHandle_t            s_evts;
static QueueHandle_t                 s_play_q;      /* rt_audio_t */
static TaskHandle_t                  s_play_task;
static audio_mixer_track_handle_t    s_track;       /* guarded by s_track_mtx */
static SemaphoreHandle_t             s_track_mtx;
static uint32_t                      s_play_rate = RT_RATE_OUT_DEFAULT;
static volatile bool                 s_play_abort;
static bool                          s_inited;
static bool                          s_session_ready;

/* Fragment assembly: a JSON message may arrive in several WEBSOCKET_EVENT_DATA
 * chunks.  One growable SPIRAM buffer, reset per message. */
static char  *s_rx;
static size_t s_rx_len;
static size_t s_rx_cap;

/* Per-turn accumulators.  Written by the WS task, read by the voice task after
 * RT_BIT_DONE, so no lock is needed (the event group is the barrier). */
static robodog_rt_turn_t s_turn;
static int64_t           s_commit_us;
static bool              s_audio_started;
static bool              s_want_audio;     /* this turn's modality choice   */
static int               s_listen_retries; /* explicit re-asks after listen */
static bool              s_saw_think;      /* the reply carried a think block */
static bool              s_think_suppress; /* ... so server audio is dropped  */
static uint32_t          s_dropped_deltas;  /* audio deltas discarded (think)  */
static volatile bool     s_play_go;        /* text known -> playback may run   */

/* Playback queue slots, allocated once. */
static uint8_t *s_slots[CONFIG_ROBODOG_RT_QUEUE_DEPTH];
static uint8_t *s_dec;   /* base64 decode scratch, allocated once */

/* ── base64 ─────────────────────────────────────────────────────────────── */

static char *rt_b64_encode(const uint8_t *src, size_t len)
{
    const size_t cap = 4 * ((len + 2) / 3) + 1;
    char *out = malloc(cap);
    if (out == NULL) {
        return NULL;
    }
    size_t olen = 0;
    if (mbedtls_base64_encode((unsigned char *)out, cap, &olen, src, len) != 0) {
        free(out);
        return NULL;
    }
    out[olen] = '\0';
    return out;
}

/* Decode into `dst` (capacity `cap`).  Returns bytes written, 0 on failure. */
static size_t rt_b64_decode(const char *src, size_t src_len,
                            uint8_t *dst, size_t cap)
{
    size_t olen = 0;
    if (mbedtls_base64_decode(dst, cap, &olen,
                              (const unsigned char *)src, src_len) != 0) {
        return 0;
    }
    return olen;
}

/* ── playback ───────────────────────────────────────────────────────────── */

/* Feed one PCM buffer into the open track.  The mixer ring is ~8 frames deep,
 * so a short write is backpressure, not a fault: retry until the ring drains.
 * This is what paces playback at the codec rate without a sleep-per-frame
 * (the Phase 2 loop slept 19 ms per 20 ms frame and ran ~5 % fast). */
static esp_err_t rt_track_write(audio_mixer_track_handle_t track,
                                const uint8_t *pcm, size_t len)
{
    size_t off = 0;
    unsigned stalls = 0;
    while (off < len) {
        const size_t w = audio_mixer_track_write(track, pcm + off, len - off);
        if (w == 0) {
            if (++stalls >= RT_STALL_LIMIT) {
                ESP_LOGE(TAG, "playback stalled (mixer ring not draining)");
                return ESP_ERR_TIMEOUT;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        stalls = 0;
        off += w;
    }
    return ESP_OK;
}

static void rt_play_task(void *arg)
{
    (void)arg;
    for (;;) {
        rt_audio_t item;
        if (xQueueReceive(s_play_q, &item, pdMS_TO_TICKS(50)) != pdTRUE) {
            continue;
        }
        if (s_play_abort || item.data == NULL || item.len == 0) {
            continue; /* slots are never freed; the item is just dropped */
        }
        if (s_think_suppress) {
            s_dropped_deltas++;
            continue; /* the reply had a think block; its audio is discarded */
        }
        if (!s_play_go) {
            /* Hold until the first transcript delta proves the reply is clean.
             * This MUST be strict: audio deltas can arrive BEFORE the text, so
             * a timed fallback would start playing a reasoning block the
             * stripper has not seen yet (measured: the first audio delta came
             * 14.5 s after commit, well before the text).  The turn's own
             * timeout bounds the wait, and the turn drains the queue at
             * response.done. */
            while (!s_play_go && !s_think_suppress && !s_play_abort) {
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            if (s_think_suppress || s_play_abort) {
                continue;
            }
        }
        if (xSemaphoreTake(s_track_mtx, pdMS_TO_TICKS(200)) != pdTRUE) {
            ESP_LOGW(TAG, "no track lock; dropping %u B", (unsigned)item.len);
            continue;
        }
        audio_mixer_track_handle_t track = s_track;
        const uint32_t play_rate = s_play_rate;
        xSemaphoreGive(s_track_mtx);
        if (track == NULL) {
            continue;
        }

        const size_t in_samples = item.len / sizeof(int16_t);
        const int16_t *src = (const int16_t *)item.data;
        int16_t *conv = NULL;
        size_t out_samples = in_samples;
        if (item.rate != 0 && item.rate != play_rate) {
            /* Rare: the omni endpoint emits the mixer's own rate (24 kHz). */
            const size_t cap = (size_t)(((uint64_t)in_samples * play_rate) /
                                        item.rate + 4);
            conv = heap_caps_malloc(cap * sizeof(int16_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (conv == NULL) {
                ESP_LOGW(TAG, "no SPIRAM to resample %u B; dropping",
                         (unsigned)item.len);
                continue;
            }
            out_samples = robodog_pcm_resample(src, in_samples, conv, cap,
                                               item.rate, play_rate);
            src = conv;
        }
        const esp_err_t err = rt_track_write(track, (const uint8_t *)src,
                                             out_samples * sizeof(int16_t));
        if (conv != NULL) {
            heap_caps_free(conv);
        }
        if (err != ESP_OK) {
            s_play_abort = true; /* the turn sees it through RT_BIT_ERR */
            xEventGroupSetBits(s_evts, RT_BIT_ERR);
        }
    }
}

/* Forward declaration: the dispatch path (response.listen) re-asks with an
 * explicit response.create, so it needs the sender before its definition. */
static esp_err_t rt_send(const char *json);

/* ── event dispatch ─────────────────────────────────────────────────────── */

/* Raw (unstripped) reply text for the current turn: `s_turn.reply` holds the
 * CLEAN text, this holds everything the server sent so a tag split across
 * deltas is recognised once the block completes. */
static char s_raw_reply[1024];
static size_t s_ui_shown;   /* clean-text length already rendered on the RLCD */

/* Append one delta: accumulate raw, re-derive clean, render the new suffix. */
static size_t rt_reply_delta(const char *delta)
{
    if (delta == NULL) {
        return strlen(s_turn.reply);
    }
    const size_t have = strlen(s_raw_reply);
    if (have + 1 < sizeof(s_raw_reply)) {
        size_t room = sizeof(s_raw_reply) - have - 1;
        size_t n = strlen(delta);
        if (n > room) {
            /* Back off to a UTF-8 boundary: never split a codepoint. */
            n = room;
            while (n > 0 && ((unsigned char)delta[n] & 0xC0) == 0x80) {
                n--;
            }
        }
        memcpy(s_raw_reply + have, delta, n);
        s_raw_reply[have + n] = '\0';
    }
    const size_t clean = robodog_think_strip(s_raw_reply, s_turn.reply,
                                             sizeof(s_turn.reply));
    if (clean > s_ui_shown) {
        robodog_ui_append_assistant(s_turn.reply + s_ui_shown);
        s_ui_shown = clean;
    }
    return clean;
}

/* Plain append for the ASR transcript (no think stripping there). */
static void rt_append_text(char *dst, size_t cap, const char *delta)
{
    if (delta == NULL) {
        return;
    }
    const size_t have = strlen(dst);
    if (have + 1 >= cap) {
        return;
    }
    size_t room = cap - have - 1;
    size_t n = strlen(delta);
    if (n > room) {
        n = room;
        while (n > 0 && ((unsigned char)delta[n] & 0xC0) == 0x80) {
            n--;
        }
    }
    memcpy(dst + have, delta, n);
    dst[have + n] = '\0';
}
/* Push a decoded PCM delta onto the playback queue.  Slots are fixed; when the
 * queue is full the delta is dropped (a wedged playback path is already
 * reported through RT_BIT_ERR). */
static void rt_enqueue_audio(const uint8_t *pcm, size_t len, uint32_t rate)
{
    static size_t next_slot;
    if (len == 0 || len > CONFIG_ROBODOG_RT_DELTA_MAX_BYTES) {
        ESP_LOGW(TAG, "audio delta %u B outside 1..%d; dropped",
                 (unsigned)len, CONFIG_ROBODOG_RT_DELTA_MAX_BYTES);
        return;
    }
    uint8_t *slot = s_slots[next_slot];
    if (slot == NULL) {
        return;
    }
    /* Copy FIRST: the play task can pick the item up the instant it is
     * queued, so enqueueing before the copy would let it read stale bytes. */
    memcpy(slot, pcm, len);
    const rt_audio_t item = { .data = slot, .len = len, .rate = rate };
    if (xQueueSend(s_play_q, &item, 0) != pdTRUE) {
        ESP_LOGW(TAG, "playback queue full; dropping %u B delta",
                 (unsigned)len);
        return;
    }
    next_slot = (next_slot + 1) % CONFIG_ROBODOG_RT_QUEUE_DEPTH;
    if (!s_audio_started) {
        s_audio_started = true;
        s_turn.first_audio_ms = (uint32_t)((esp_timer_get_time() - s_commit_us) / 1000);
        s_turn.spoke = true;
        ESP_LOGI(TAG, "RT first audio %u ms after commit", s_turn.first_audio_ms);
    }
}

static const char *rt_str(cJSON *obj, const char *key)
{
    cJSON *it = cJSON_GetObjectItemCaseSensitive(obj, key);
    return (cJSON_IsString(it) && it->valuestring != NULL) ? it->valuestring : NULL;
}

static void rt_dispatch(cJSON *root)
{
    const char *type = rt_str(root, "type");
    if (type == NULL) {
        return;
    }

    if (strcmp(type, "session.created") == 0) {
        cJSON *session = cJSON_GetObjectItemCaseSensitive(root, "session");
        const char *id = rt_str(session, "id");
        const char *model = rt_str(session, "model");
        cJSON *caps = session != NULL
                          ? cJSON_GetObjectItemCaseSensitive(session, "capabilities")
                          : NULL;
        ESP_LOGI(TAG, "RT session=%s model=%s caps=%s",
                 id != NULL ? id : "?", model != NULL ? model : "?",
                 caps != NULL ? "yes" : "no");
        s_session_ready = true;
        xEventGroupSetBits(s_evts, RT_BIT_READY);
        return;
    }
    if (strcmp(type, "session.updated") == 0) {
        /* The server applies session.update asynchronously and RESETS the
         * input audio buffer when it lands, so anything streamed before this
         * event is discarded and the commit then produces nothing.  The turn
         * waits for it before capturing. */
        ESP_LOGI(TAG, "RT session updated (config applied)");
        xEventGroupSetBits(s_evts, RT_BIT_UPDATED);
        return;
    }
    if (strcmp(type, "rate_limits.updated") == 0) {
        return;
    }
    if (strcmp(type, "error") == 0) {
        const char *code = rt_str(root, "code");
        const char *msg = rt_str(root, "message");
        ESP_LOGE(TAG, "RT error code=%s message=%s",
                 code != NULL ? code : "?", msg != NULL ? msg : "?");
        /* A rejected response.create is definitive, not a health signal; the
         * turn is over either way. */
        xEventGroupSetBits(s_evts, RT_BIT_ERR);
        return;
    }

    if (strcmp(type, "conversation.item.input_audio_transcription.completed") == 0) {
        const char *t = rt_str(root, "transcript");
        if (t == NULL) {
            t = rt_str(root, "text");
        }
        if (t != NULL) {
            rt_append_text(s_turn.transcript, sizeof(s_turn.transcript), t);
            ESP_LOGI(TAG, "RT ASR text=\"%s\"", s_turn.transcript);
        }
        return;
    }
    if (strcmp(type, "response.output_audio_transcript.delta") == 0 ||
        strcmp(type, "response.output_text.delta") == 0 ||
        strcmp(type, "response.text.delta") == 0) {
        const char *d = rt_str(root, "delta");
        rt_reply_delta(d);
        if (!s_think_suppress && robodog_think_present(s_raw_reply)) {
            /* MiniCPM-o emitted a reasoning block.  It is VOICED by the
             * Talker (measured: ASR of the turn's audio heard "Gate, rot,
             * zoo, rot, okay, 21..." — the model pronouncing the tag), and
             * there is no server-side switch that removes it.  So this turn's
             * server audio is discarded and the CLEAN text is re-spoken with
             * Qwen3-TTS after response.done.  Rare (only on repeated
             * questions in one session), so the fast path is unaffected. */
            ESP_LOGW(TAG, "RT reply carries a think block; suppressing the "
                     "server audio for this turn");
            s_saw_think = true;
            s_think_suppress = true;
            xQueueReset(s_play_q); /* drop whatever was already queued */
        }
        if (!s_think_suppress) {
            /* The text is known and clean: let playback run.  The gate exists
             * so audio queued BEFORE this delta cannot play while the reply
             * is still unknown. */
            s_play_go = true;
        }
        return;
    }
    if (strcmp(type, "response.output_audio.delta") == 0) {
        const char *b64 = rt_str(root, "delta");
        if (b64 == NULL) {
            b64 = rt_str(root, "audio");
        }
        if (b64 == NULL) {
            return;
        }
        cJSON *sr = cJSON_GetObjectItemCaseSensitive(root, "sample_rate_hz");
        const uint32_t rate = cJSON_IsNumber(sr) && sr->valueint > 0
                                  ? (uint32_t)sr->valueint
                                  : RT_RATE_OUT_DEFAULT;
        if (s_dec == NULL) {
            return;
        }
        const size_t n = rt_b64_decode(b64, strlen(b64), s_dec,
                                       CONFIG_ROBODOG_RT_DELTA_MAX_BYTES);
        if (n == 0) {
            ESP_LOGW(TAG, "audio delta did not decode (%u b64 chars, cap %d)",
                     (unsigned)strlen(b64), CONFIG_ROBODOG_RT_DELTA_MAX_BYTES);
            return;
        }
        rt_enqueue_audio(s_dec, n, rate);
        return;
    }
    if (strcmp(type, "response.listen") == 0) {
        /* The model's native turn-taking decided the utterance was not a
         * complete turn.  A committed utterance MUST still be answered, so
         * re-ask ONCE with an explicit response.create; a second listen
         * verdict is honoured (the user really did pause mid-sentence and
         * the next listen continues the turn).  Measured on hardware: a
         * 2.1 s committed utterance produced `listen` first and `speak`
         * right after the explicit re-ask. */
        if (s_listen_retries < 1) {
            s_listen_retries++;
            ESP_LOGI(TAG, "RT decision listen; re-asking with an explicit "
                     "response.create");
            char resp[160];
            snprintf(resp, sizeof(resp),
                     "{\"type\":\"response.create\",\"response\":"
                     "{\"modalities\":[%s]}}",
                     s_want_audio ? "\"audio\",\"text\"" : "\"text\"");
            if (rt_send(resp) != ESP_OK) {
                xEventGroupSetBits(s_evts, RT_BIT_ERR);
            }
            return;
        }
        ESP_LOGI(TAG, "RT decision listen (kept listening; turn ends silent)");
        xEventGroupSetBits(s_evts, RT_BIT_DONE);
        return;
    }
    if (strcmp(type, "response.done") == 0) {
        ESP_LOGI(TAG, "RT response.done");
        xEventGroupSetBits(s_evts, RT_BIT_DONE);
        return;
    }
    if (strcmp(type, "response.created") == 0) {
        ESP_LOGI(TAG, "RT response.created");
        return;
    }
    /* Anything else is logged once so the console shows the server's real
     * vocabulary (the probe/E2E reads these lines). */
    ESP_LOGD(TAG, "RT event %s", type);
}

static void rt_ws_handler(void *arg, esp_event_base_t base, int32_t id,
                          void *data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *ev = (esp_websocket_event_data_t *)data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "RT websocket connected");
        xEventGroupSetBits(s_evts, RT_BIT_CONNECTED);
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        ESP_LOGW(TAG, "RT websocket closed/disconnected");
        s_session_ready = false;
        xEventGroupSetBits(s_evts, RT_BIT_ERR);
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "RT websocket error type=%d status=%d errno=%d",
                 (int)ev->error_handle.error_type,
                 ev->error_handle.esp_ws_handshake_status_code,
                 ev->error_handle.esp_transport_sock_errno);
        xEventGroupSetBits(s_evts, RT_BIT_ERR);
        break;
    case WEBSOCKET_EVENT_DATA: {
        if (ev->op_code != 0x1 /* text */ && ev->op_code != 0x0 /* cont */) {
            return;
        }
        /* Reassemble a fragmented message.  esp_websocket_client posts ONE
         * event per fragment and reports the WHOLE message length in
         * `payload_len` every time, so size the buffer from that ONCE —
         * sizing from `payload_offset + data_len` reallocs on every fragment
         * (a 100 KB delta arrives as ~50 events, each growing the buffer,
         * and the SPIRAM allocator fails long before the message completes). */
        const size_t whole = (size_t)ev->payload_len + 1;
        if (whole > s_rx_cap) {
            char *n = heap_caps_realloc(s_rx, whole,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (n == NULL) {
                ESP_LOGE(TAG, "no SPIRAM for a %u B event (free=%u largest=%u)",
                         (unsigned)whole,
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
                s_rx_len = 0;
                return;
            }
            s_rx = n;
            s_rx_cap = whole;
        }
        if (ev->payload_offset == 0) {
            s_rx_len = 0;
        }
        memcpy(s_rx + ev->payload_offset, ev->data_ptr, (size_t)ev->data_len);
        s_rx_len = (size_t)ev->payload_len;
        if ((size_t)ev->payload_offset + (size_t)ev->data_len <
            (size_t)ev->payload_len) {
            return; /* more fragments to come */
        }
        s_rx[s_rx_len] = '\0';
        cJSON *root = cJSON_Parse(s_rx);
        if (root != NULL) {
            rt_dispatch(root);
            cJSON_Delete(root);
        } else {
            ESP_LOGW(TAG, "RT unparseable event (%.80s)", s_rx);
        }
        s_rx_len = 0;
        break;
    }
    default:
        break;
    }
}

/* ── session ────────────────────────────────────────────────────────────── */

static esp_err_t rt_send(const char *json)
{
    if (s_ws == NULL || !esp_websocket_client_is_connected(s_ws)) {
        /* Drop the cached session so the next turn reconnects rather than
         * reusing a dead handle (a dead engine or a dropped socket would
         * otherwise make every later turn fail identically). */
        s_session_ready = false;
        return ESP_ERR_INVALID_STATE;
    }
    const int n = esp_websocket_client_send_text(s_ws, json, (int)strlen(json),
                                                 pdMS_TO_TICKS(RT_WS_TIMEOUT_MS));
    if (n < 0) {
        s_session_ready = false;
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* Build + send the open handshake.  `auto_response=false` + `turn_detection:
 * null` keeps the turn boundary OURS: a server VAD/auto-response would answer
 * mid-sentence, and the board's own VAD is what the Phase 2 loop already
 * proved. */
static esp_err_t rt_session_update(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *session = cJSON_CreateObject();
    if (!root || !session) {
        cJSON_Delete(root);
        cJSON_Delete(session);
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(root, "type", "session.update");
    /* Minimal accepted payload.  Measured on hardware: adding `modalities`,
     * `turn_detection`, `input_audio_format` or `extra_body` to this request
     * makes the server accept the update but then silently drop the commit —
     * no `input_audio_buffer.committed`, no response.  `session.type` is
     * MANDATORY (OpenAI's schema); without it the update is rejected with
     * `invalid_event` and the session keeps its defaults. */
    cJSON_AddStringToObject(session, "type", "realtime");
    cJSON_AddStringToObject(session, "model", CONFIG_ROBODOG_RT_MODEL);
    cJSON_AddStringToObject(session, "instructions",
                            CONFIG_ROBODOG_RT_INSTRUCTIONS);
    /* `enable_thinking: false` is a TEMPLATE switch, not a prompt request:
     * MiniCPM-o's chat template emits an empty ` thinking\n\n</think>\n\n`
     * block when it is false, so the model skips reasoning entirely.  This is
     * load-bearing, not cosmetic: when the model DOES emit a think block it
     * produces no `<|tts_bos|>` span, and the Talker stage then dies with
     * `ValueError: MiniCPM-o Talker requires tensor tts_token_ids and
     * tts_hidden_states conditioning` — killing Stage-1 for the whole engine,
     * after which EVERY later turn fails and the board falls back to idle with
     * no answer.  Measured: this also halves time-to-first-audio (2.5 s ->
     * 1.06 s) because the think tokens are no longer generated. */
    cJSON *extra = cJSON_AddObjectToObject(session, "extra_body");
    if (extra != NULL) {
        cJSON *tmpl = cJSON_AddObjectToObject(extra, "chat_template_kwargs");
        if (tmpl != NULL) {
            cJSON_AddBoolToObject(tmpl, "enable_thinking", false);
        }
    }
    /* Input transcription: the AGENT shape routes on the USER's words, and a
     * turn-mode session does not produce them otherwise.  The handler accepts
     * this field and echoes it (its own docs note no separate ASR stage runs,
     * so the transcript comes from the model — see the fallback in
     * rt_dispatch, which also reads the reply text when the user transcript
     * is absent). */
    cJSON *tx = cJSON_AddObjectToObject(session, "input_audio_transcription");
    if (tx != NULL) {
        cJSON_AddStringToObject(tx, "model", "whisper-1");
    }
    cJSON_AddItemToObject(root, "session", session);

    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (json == NULL) {
        return ESP_ERR_NO_MEM;
    }
    const esp_err_t err = rt_send(json);
    free(json);
    return err;
}

esp_err_t robodog_rt_connect(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_session_ready && s_ws != NULL &&
        esp_websocket_client_is_connected(s_ws)) {
        return ESP_OK;
    }
    xEventGroupClearBits(s_evts, RT_BIT_READY | RT_BIT_UPDATED |
                         RT_BIT_DONE | RT_BIT_ERR);

    if (s_ws == NULL) {
        const esp_websocket_client_config_t cfg = {
            .uri = CONFIG_ROBODOG_RT_URL,
            .buffer_size = RT_WS_BUF,
            .task_stack = RT_WS_TASK_STACK,
            .network_timeout_ms = RT_WS_TIMEOUT_MS,
            .reconnect_timeout_ms = 3000,
            /* The turn code owns reconnection: an auto-reconnect mid-turn
             * would replay the handshake under an in-flight response. */
            .disable_auto_reconnect = true,
        };
        s_ws = esp_websocket_client_init(&cfg);
        if (s_ws == NULL) {
            ESP_LOGE(TAG, "RT websocket init failed");
            return ESP_FAIL;
        }
        esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY,
                                      rt_ws_handler, NULL);
    }

    ESP_LOGI(TAG, "RT connecting %s", CONFIG_ROBODOG_RT_URL);
    xEventGroupClearBits(s_evts, RT_BIT_CONNECTED);
    esp_err_t err = esp_websocket_client_start(s_ws);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "RT websocket start failed: %s", esp_err_to_name(err));
        return err;
    }
    /* The upgrade completes asynchronously: esp_websocket_client_start()
     * returns before the socket is usable, so sending session.update here
     * would fail with ESP_ERR_INVALID_STATE ("not connected").  Wait for the
     * CONNECTED event first. */
    const EventBits_t cbits = xEventGroupWaitBits(
        s_evts, RT_BIT_CONNECTED | RT_BIT_ERR, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(10000));
    if ((cbits & RT_BIT_CONNECTED) == 0) {
        ESP_LOGE(TAG, "RT websocket did not connect (%s)",
                 (cbits & RT_BIT_ERR) ? "error" : "timeout");
        return (cbits & RT_BIT_ERR) ? ESP_FAIL : ESP_ERR_TIMEOUT;
    }
    err = rt_session_update();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "RT session.update failed: %s", esp_err_to_name(err));
        return err;
    }
    const EventBits_t bits = xEventGroupWaitBits(
        s_evts, RT_BIT_READY | RT_BIT_ERR, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(15000));
    if ((bits & RT_BIT_READY) == 0) {
        ESP_LOGE(TAG, "RT no session.created (%s)",
                 (bits & RT_BIT_ERR) ? "error/disconnected" : "timeout");
        return (bits & RT_BIT_ERR) ? ESP_FAIL : ESP_ERR_TIMEOUT;
    }
    /* Wait for the config to actually land before any audio is streamed. */
    const EventBits_t ubits = xEventGroupWaitBits(
        s_evts, RT_BIT_UPDATED | RT_BIT_ERR, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(10000));
    if ((ubits & RT_BIT_UPDATED) == 0) {
        ESP_LOGE(TAG, "RT session.updated not received (%s)",
                 (ubits & RT_BIT_ERR) ? "error" : "timeout");
        return (ubits & RT_BIT_ERR) ? ESP_FAIL : ESP_ERR_TIMEOUT;
    }
    ESP_LOGI(TAG, "RT session ready");
    return ESP_OK;
}

void robodog_rt_disconnect(void)
{
    if (s_ws != NULL) {
        (void)esp_websocket_client_close(s_ws, pdMS_TO_TICKS(3000));
        (void)esp_websocket_client_stop(s_ws);
    }
    s_session_ready = false;
}

bool robodog_rt_connected(void)
{
    return s_session_ready && s_ws != NULL &&
           esp_websocket_client_is_connected(s_ws);
}

/* ── router ─────────────────────────────────────────────────────────────── */

/* The capability router is the pure-C, host-tested `voice_route` module (one
 * keyword table, compiled by both the firmware and the Bazel unit test).  The
 * realtime module only maps its verdict onto the session's turn shape. */
robodog_rt_mode_t robodog_rt_route(const char *text, const char **out_reason)
{
    const robodog_route_t r = robodog_route_turn(text, out_reason);
    return (r == ROBODOG_ROUTE_AGENT) ? ROBODOG_RT_MODE_AGENT
                                      : ROBODOG_RT_MODE_OMNI;
}

/* ── one turn ───────────────────────────────────────────────────────────── */

static esp_err_t rt_playback_begin(audio_mixer_track_handle_t *out_track)
{
    audio_mixer_handle_t mix = NULL;
    audio_mixer_track_handle_t track = NULL;
    if (audio_hub_get_mixer(&mix) != ESP_OK ||
        audio_mixer_open_track(mix, AUDIO_MIXER_TRACK_APP, "robodog_rt",
                               &track) != ESP_OK) {
        ESP_LOGE(TAG, "RT could not open the APP mixer track");
        return ESP_ERR_INVALID_STATE;
    }
    uint32_t rate = RT_RATE_OUT_DEFAULT;
    (void)audio_mixer_track_info(track, &rate, NULL, NULL);
    if (rate < 8000 || rate > 96000) {
        rate = RT_RATE_OUT_DEFAULT;
    }
    if (xSemaphoreTake(s_track_mtx, pdMS_TO_TICKS(500)) != pdTRUE) {
        audio_mixer_close_track(track);
        return ESP_ERR_TIMEOUT;
    }
    s_track = track;
    s_play_rate = rate;
    xSemaphoreGive(s_track_mtx);
    *out_track = track;

    /* The board-manager leaves the PA pin low and the codec muted; the reply
     * is silent until all three are asserted (Phase 2 finding, shared helper). */
    (void)robodog_speaker_assert_output();
    return ESP_OK;
}

static void rt_playback_end(audio_mixer_track_handle_t track)
{
    if (xSemaphoreTake(s_track_mtx, pdMS_TO_TICKS(500)) == pdTRUE) {
        s_track = NULL;
        xSemaphoreGive(s_track_mtx);
    }
    (void)audio_mixer_track_flush(track);
    audio_mixer_close_track(track);
}

/* Capture the utterance and stream it into the session as it arrives.
 * Returns samples streamed (0 = nothing captured) and sets `fault` on a real
 * capture fault (never on "the user said nothing"). */
static size_t rt_stream_utterance(bool *fault)
{
    *fault = false;
    static audio_capture_sub_handle_t sub;
    if (sub == NULL) {
        audio_capture_handle_t cap = NULL;
        if (audio_hub_get_capture(&cap) != ESP_OK) {
            ESP_LOGE(TAG, "RT capture hub unavailable");
            *fault = true;
            return 0;
        }
        const audio_capture_sub_format_t fmt = {
            .sample_rate = RT_RATE_IN, .channels = RT_CH, .bits = RT_BITS,
        };
        if (audio_capture_open_subscriber(cap, AUDIO_CAPTURE_SUB_APP, &fmt,
                                          "robodog_rt", &sub) != ESP_OK ||
            sub == NULL) {
            ESP_LOGE(TAG, "RT open APP capture subscriber failed");
            *fault = true;
            sub = NULL;
            return 0;
        }
    }
    (void)audio_capture_sub_flush(sub);
    ESP_LOGI(TAG, "RT listen started (16k/1/16, VAD energy=%d silence=%dms)",
             RT_VAD_ENERGY, CONFIG_ROBODOG_RT_SILENCE_MS);
    robodog_ui_set_agent_state("listening");

    static int16_t frame[RT_FRAME_SAMPLES];
    size_t fi = 0;
    size_t streamed = 0;
    size_t streamed_bytes = 0;
    uint32_t silence_ms = 0;
    uint32_t speech_ms = 0;
    bool ever_speech = false;
    int64_t speech_t0 = -1;
    const int64_t t0 = esp_timer_get_time();
    const int64_t no_speech_us = (int64_t)RT_NO_SPEECH_MS * 1000LL;
    const int64_t cap_us = (int64_t)CONFIG_ROBODOG_RT_MAX_SECS * 1000000LL;

    for (;;) {
        const int64_t now = esp_timer_get_time();
        if (now - t0 >= cap_us) {
            ESP_LOGW(TAG, "RT utterance capped at %d s",
                     CONFIG_ROBODOG_RT_MAX_SECS);
            break;
        }
        if (!ever_speech && now - t0 >= no_speech_us) {
            ESP_LOGW(TAG, "RT no speech within %d ms, bailing", RT_NO_SPEECH_MS);
            break;
        }
        if (ever_speech && now - speech_t0 >= cap_us) {
            break;
        }

        const size_t want = RT_FRAME_SAMPLES - fi;
        const size_t n = audio_capture_sub_read(sub, (uint8_t *)(frame + fi),
                                                want * sizeof(int16_t), 150);
        if (n > 0) {
            streamed_bytes += n;
            fi += n / sizeof(int16_t);
        }
        if (fi < RT_FRAME_SAMPLES) {
            /* Still waiting for a whole frame; the settle point below must
             * still run so a quiet ring can end a real utterance. */
        } else {
            int64_t sum = 0;
            for (size_t i = 0; i < RT_FRAME_SAMPLES; i++) {
                sum += (frame[i] < 0) ? -(int64_t)frame[i] : (int64_t)frame[i];
            }
            const bool speech = (sum / (int64_t)RT_FRAME_SAMPLES) > RT_VAD_ENERGY;

            /* Stream EVERY frame once speech has begun (word tails included);
             * pre-speech silence is not sent, so the model never hears the
             * room before the utterance. */
            if (speech || ever_speech) {
                /* The realtime PCM contract is 24 kHz mono PCM16 (the
                 * handler hardcodes SAMPLE_RATE_HZ), while this board's
                 * capture hub runs 16 kHz.  Send the resampled frame —
                 * appending raw 16 kHz audio makes the model hear the
                 * utterance 1.5x fast and transcribe garbage. */
                static int16_t up[RT_FRAME_SAMPLES * RT_RATE_OUT_DEFAULT /
                                   RT_RATE_IN + 8];
                const size_t up_n = robodog_pcm_resample(
                    frame, RT_FRAME_SAMPLES, up,
                    sizeof(up) / sizeof(up[0]), RT_RATE_IN,
                    RT_RATE_OUT_DEFAULT);
                char *b64 = rt_b64_encode((const uint8_t *)up,
                                          up_n * sizeof(int16_t));
                if (b64 != NULL) {
                    /* One frame is 640 B -> 856 base64 chars; the JSON
                     * envelope fits the fixed buffer with margin. */
                    static char msg[1400];
                    const int mlen = snprintf(
                        msg, sizeof(msg),
                        "{\"type\":\"input_audio_buffer.append\","
                        "\"audio\":\"%s\"}",
                        b64);
                    free(b64);
                    if (mlen <= 0 || (size_t)mlen >= sizeof(msg)) {
                        ESP_LOGE(TAG, "RT append message overflow (%d)", mlen);
                        *fault = true;
                        return streamed;
                    }
                    if (rt_send(msg) != ESP_OK) {
                        ESP_LOGE(TAG, "RT append failed (socket down)");
                        *fault = true;
                        return streamed;
                    }
                }
                streamed += RT_FRAME_SAMPLES;
            }
            if (speech) {
                if (!ever_speech) {
                    speech_t0 = now;
                }
                ever_speech = true;
                silence_ms = 0;
                speech_ms += RT_FRAME_MS;
            } else if (ever_speech) {
                silence_ms += RT_FRAME_MS;
            }
            fi = 0;
        }

        if (ever_speech && silence_ms >= CONFIG_ROBODOG_RT_SILENCE_MS) {
            if (speech_ms >= RT_VAD_MIN_SPEECH_MS) {
                break; /* trailing silence after real speech */
            }
            ESP_LOGI(TAG, "RT ignoring a %u ms blip; still listening",
                     (unsigned)speech_ms);
            speech_ms = 0;
            silence_ms = 0;
            ever_speech = false;
            speech_t0 = -1;
        }
    }

    if (streamed_bytes == 0) {
        ESP_LOGE(TAG, "RT capture stream produced no audio (codec/I2S RX?)");
        *fault = true;
    }
    return streamed;
}

esp_err_t robodog_rt_turn(robodog_rt_mode_t mode, robodog_rt_turn_t *out)
{
    if (!s_inited || out == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    memset(out, 0, sizeof(*out));
    memset(&s_turn, 0, sizeof(s_turn));
    s_commit_us = 0;
    s_audio_started = false;
    s_play_abort = false;
    s_listen_retries = 0;
    s_raw_reply[0] = '\0';
    s_ui_shown = 0;
    s_saw_think = false;
    s_think_suppress = false;
    s_play_go = false;
    s_dropped_deltas = 0;

    esp_err_t err = robodog_rt_connect();
    if (err != ESP_OK) {
        out->fault = true;
        return err;
    }
    xEventGroupClearBits(s_evts, RT_BIT_DONE | RT_BIT_ERR);

    bool cap_fault = false;
    const size_t samples = rt_stream_utterance(&cap_fault);
    if (cap_fault) {
        out->fault = true;
        ESP_LOGE(TAG, "RT capture fault; ending turn");
        robodog_ui_set_agent_state("error");
        return ESP_FAIL;
    }
    if (samples == 0) {
        ESP_LOGW(TAG, "RT no usable utterance");
        robodog_ui_set_agent_state("idle");
        return ESP_OK;
    }

    s_turn.captured = true; /* the utterance is now in the session */
    const bool want_audio = (mode == ROBODOG_RT_MODE_OMNI);
    s_want_audio = want_audio;
    /* Commit seals the utterance into a user item.  `response_create` is left
     * false so the turn boundary and the modality choice stay ours. */
    char commit[160];
    /* Turn mode: the plain OpenAI commit; `auto_response` answers it. */
    snprintf(commit, sizeof(commit),
             "{\"type\":\"input_audio_buffer.commit\"}");
    if (rt_send(commit) != ESP_OK) {
        out->fault = true;
        out->captured = true;
        return ESP_FAIL;
    }

    char resp[256];
    snprintf(resp, sizeof(resp),
             "{\"type\":\"response.create\",\"response\":{\"modalities\":[%s]%s}}",
             want_audio ? "\"audio\",\"text\"" : "\"text\"",
             want_audio ? "" : ",\"max_output_tokens\":256");
    s_commit_us = esp_timer_get_time();
    if (rt_send(resp) != ESP_OK) {
        out->fault = true;
        out->captured = true;
        return ESP_FAIL;
    }
    robodog_ui_set_agent_state(want_audio ? "responding" : "processing");

    audio_mixer_track_handle_t track = NULL;
    if (want_audio) {
        err = rt_playback_begin(&track);
        if (err != ESP_OK) {
            out->fault = true;
            return err;
        }
    }

    const EventBits_t bits = xEventGroupWaitBits(
        s_evts, RT_BIT_DONE | RT_BIT_ERR, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(CONFIG_ROBODOG_RT_TIMEOUT_MS));

    /* Release the playback gate: if no think block was seen the remaining
     * audio is legitimate and must play; if one was, it is all discarded.
     * Without this a turn whose reply never produced a text delta would leave
     * the playback task spinning on the gate and the drain below would never
     * finish. */
    if (s_think_suppress) {
        s_play_abort = true;
        xQueueReset(s_play_q);
    } else {
        s_play_go = true;
    }

    /* Drain the playback queue before releasing the track, so the last
     * sentence is not cut off by the close. */
    if (want_audio && track != NULL) {
        while (uxQueueMessagesWaiting(s_play_q) > 0 && !s_play_abort) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        vTaskDelay(pdMS_TO_TICKS(60));
        rt_playback_end(track);
    }

    if ((bits & RT_BIT_ERR) != 0) {
        out->fault = true;
        robodog_ui_set_agent_state("error");
        return ESP_FAIL;
    }
    if ((bits & RT_BIT_DONE) == 0) {
        ESP_LOGE(TAG, "RT no response.done within %d ms",
                 CONFIG_ROBODOG_RT_TIMEOUT_MS);
        out->fault = true;
        robodog_ui_set_agent_state("error");
        return ESP_ERR_TIMEOUT;
    }

    memcpy(out, &s_turn, sizeof(*out));
    ESP_LOGI(TAG, "RT turn done: transcript=\"%.80s\" reply=\"%.80s\" "
             "first_audio=%u ms captured=%d think=%d dropped=%u", out->transcript,
             out->reply, (unsigned)out->first_audio_ms, (int)out->captured,
             (int)s_saw_think, (unsigned)s_dropped_deltas);

    /* The server's audio was dropped because the reply carried a think
     * block.  Speak the CLEAN text instead so the answer is still heard. */
    if (s_think_suppress) {
        const char *say = out->reply[0] != '\0'
                              ? out->reply
                              : "Sorry, I lost that one. Please say it again.";
        if (out->reply[0] == '\0') {
            /* Every byte was reasoning, so the clean reply is empty: without
             * this the turn is silent and the panel just says idle — the
             * reported "hits idle and no response". */
            ESP_LOGW(TAG, "RT reply was entirely a think block; speaking a "
                     "retry prompt");
        } else {
            ESP_LOGI(TAG, "RT speaking the cleaned reply via Qwen3-TTS "
                     "(think block removed)");
        }
        if (robodog_voice_speak(say) == ESP_OK) {
            out->spoke = true;
        } else {
            ESP_LOGW(TAG, "RT clean-reply TTS failed");
        }
        /* The think block comes from the accumulated session history (it
         * appears on repeated questions and a re-ask does NOT clear it —
         * measured).  Drop the session so the NEXT turn starts with a clean
         * history, which is the state that never emits one. */
        robodog_rt_disconnect();
        ESP_LOGI(TAG, "RT session reset (fresh history after a think turn)");
    }
    return ESP_OK;
}

/* ── init + console ─────────────────────────────────────────────────────── */

esp_err_t robodog_rt_init(void)
{
    if (s_inited) {
        return ESP_OK;
    }
    s_evts = xEventGroupCreate();
    s_track_mtx = xSemaphoreCreateMutex();
    if (s_evts == NULL || s_track_mtx == NULL) {
        ESP_LOGE(TAG, "RT primitives failed");
        return ESP_ERR_NO_MEM;
    }
    /* The fragment buffer starts modest and grows only when the server sends a
     * bigger message (a 64 KB audio delta is ~87 KB of base64). */
    /* One audio delta is ~40 KB of PCM -> ~55 KB of base64 inside a JSON
     * envelope; a transcript delta is small.  64 KB avoids a realloc on the
     * common path and still grows for a bigger message. */
    s_rx_cap = 65536;
    s_rx = heap_caps_malloc(s_rx_cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_rx == NULL) {
        ESP_LOGE(TAG, "RT no SPIRAM for the event buffer");
        return ESP_ERR_NO_MEM;
    }
    s_rx_len = 0;

    s_dec = heap_caps_malloc(CONFIG_ROBODOG_RT_DELTA_MAX_BYTES,
                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_dec == NULL) {
        ESP_LOGE(TAG, "RT no SPIRAM for the delta decode buffer");
        return ESP_ERR_NO_MEM;
    }
    s_play_q = xQueueCreate(CONFIG_ROBODOG_RT_QUEUE_DEPTH, sizeof(rt_audio_t));
    if (s_play_q == NULL) {
        ESP_LOGE(TAG, "RT no queue");
        return ESP_ERR_NO_MEM;
    }
    size_t slots = 0;
    for (; slots < CONFIG_ROBODOG_RT_QUEUE_DEPTH; slots++) {
        s_slots[slots] = heap_caps_malloc(CONFIG_ROBODOG_RT_DELTA_MAX_BYTES,
                                          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_slots[slots] == NULL) {
            break;
        }
    }
    if (slots < 4) {
        ESP_LOGE(TAG, "RT only %u playback slot(s) of %d x %d B",
                 (unsigned)slots, CONFIG_ROBODOG_RT_QUEUE_DEPTH,
                 CONFIG_ROBODOG_RT_DELTA_MAX_BYTES);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "RT playback slots: %u x %d B (~%u s buffered)",
             (unsigned)slots, CONFIG_ROBODOG_RT_DELTA_MAX_BYTES,
             (unsigned)((slots * CONFIG_ROBODOG_RT_DELTA_MAX_BYTES) /
                        (RT_RATE_OUT_DEFAULT * 2)));

    if (xTaskCreate(rt_play_task, "robodog_rt_play", RT_PLAY_STACK, NULL,
                    RT_PLAY_PRIO, &s_play_task) != pdPASS) {
        ESP_LOGE(TAG, "RT playback task failed");
        return ESP_ERR_NO_MEM;
    }
    s_inited = true;
    ESP_LOGI(TAG, "RoboDog Phase 3 realtime ready: %s (model %s)",
             CONFIG_ROBODOG_RT_URL, CONFIG_ROBODOG_RT_MODEL);
    return ESP_OK;
}

static int cmd_rd_rt(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    const esp_err_t err = robodog_rt_connect();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rd_rt: session failed: %s", esp_err_to_name(err));
        return 1;
    }
    ESP_LOGI(TAG, "rd_rt: session ok, connected=%d", (int)robodog_rt_connected());
    return 0;
}

static int cmd_rd_rt_route(int argc, char **argv)
{
    if (argc < 2) {
        ESP_LOGI(TAG, "usage: rd_rt_route <text>");
        return 1;
    }
    char text[256];
    size_t o = 0;
    for (int i = 1; i < argc && o + 1 < sizeof(text); i++) {
        if (i > 1 && o + 1 < sizeof(text)) {
            text[o++] = ' ';
        }
        for (const char *p = argv[i]; *p != '\0' && o + 1 < sizeof(text); p++) {
            text[o++] = *p;
        }
    }
    text[o] = '\0';
    const char *reason = NULL;
    const robodog_rt_mode_t m = robodog_rt_route(text, &reason);
    ESP_LOGI(TAG, "rd_rt_route: %s -> %s (reason=%s)", text,
             m == ROBODOG_RT_MODE_AGENT ? "agent" : "omni", reason);
    return 0;
}

esp_err_t robodog_rt_register_commands(void)
{
    const esp_console_cmd_t rt = {
        .command = "rd_rt",
        .help = "omni realtime: connect + log the session handshake",
        .func = &cmd_rd_rt,
    };
    if (esp_console_cmd_register(&rt) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_rt'");
        return ESP_FAIL;
    }
    const esp_console_cmd_t route = {
        .command = "rd_rt_route",
        .help = "show the capability router's decision for a transcript",
        .func = &cmd_rd_rt_route,
    };
    if (esp_console_cmd_register(&route) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_rt_route'");
        return ESP_FAIL;
    }
    return ESP_OK;
}
