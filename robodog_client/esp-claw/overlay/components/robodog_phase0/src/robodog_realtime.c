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
 *   - the CAPTURE loop only reads the mic and pushes raw frames into the
 *     uplink ring; a dedicated sender task resamples/base64s/coalesces them
 *     and writes to the socket with a short timeout.  A slow socket drops the
 *     oldest frame (counted) instead of stalling the reader — the turn keeps
 *     its realtime pace and can still reach its own cap.
 *   - audio deltas are decoded (base64 -> PCM16) into fixed-size PSRAM buffers
 *     and queued; the playback task writes them to the shared APP mixer track.
 *     The mixer ring provides backpressure, so no fixed sleep pacing is
 *     needed (the Phase 2 19 ms-step loop ran ~5 % fast).
 *   - the turn itself runs on the voice task (same caller as Phase 2).
 *
 * Memory invariants: no allocation per delta beyond the fixed queue slots
 * (allocated once at init), so the Phase 2 R7 heap-churn class cannot recur.
 * The decode scratch is sized for the WHOLE reply delta the Qwen lane sends
 * (~214 KB for a 4.5 s answer); a larger one is dropped with a log rather than
 * growing a buffer mid-stream.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_realtime.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
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

#include "robodog_http.h"
#include "robodog_speaker.h"
#include "robodog_ui.h"
#include "robodog_utf8.h"
#include "voice_asr.h"
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

/* ── capture -> socket decoupling ──────────────────────────────────────────
 * The capture loop must NEVER block on the network.  It reads the mic ring at
 * real-time pace and pushes raw 16 kHz frames into `s_up_ring`; the sender task
 * below drains them, resamples to the session rate, base64s, coalesces up to
 * RT_UP_COALESCE frames into ONE input_audio_buffer.append (one JSON message
 * per 40-60 ms), and sends with a SHORT timeout.  When the ring is full the
 * OLDEST frame is dropped (counted) instead of stalling the reader, so a slow
 * socket can never wedge the turn or overflow the capture subscriber ring. */
#define RT_UP_FRAMES          64   /* 64 x 20 ms = 1.28 s of raw 16 kHz PCM  */
#define RT_UP_COALESCE        6    /* frames per append (120 ms messages)     */
/* The append timeout must ABSORB Wi-Fi backpressure, not police it:
 * esp_websocket_client ABORTS the whole connection when a write returns 0
 * (`esp_transport_write() returned 0` -> abort_connection, issue #301), so a
 * short budget turns a momentarily full TCP send buffer into a dead socket.
 * The sender is a background task and the ring drops the oldest frame when
 * full, so blocking here is harmless. */
#define RT_UP_SEND_TIMEOUT_MS 3000 /* absorb a full TCP send buffer           */
#define RT_UP_DRAIN_MS        5000 /* wait for the ring to flush before commit */
/* The omni server accepts ~20 s of streamed audio but CLOSES the socket on a
 * ~30 s stream (probed on the deployed lane: 10 s and 20 s answered, 30 s
 * closed the connection).  So the model gets at most this much of a long
 * utterance; the capture keeps running for the ASR/VAD (a 30 s utterance is
 * still transcribed from its first 8 s). */
#define RT_STREAM_MAX_MS      20000
#define RT_B64_CHUNK          1536 /* multiple of 3 for streaming base64       */
#define RT_UP_MSG_MAX         8448 /* 6 frames -> 5760 B PCM -> 7680 b64 + JSON */
/* Explicit ASR (the Qwen lane never emits the transcription event): keep the
 * utterance PCM for the chat-completions round trip.  8 s of 16 kHz mono
 * covers a normal turn; a longer one is truncated with a loud log (the realtime
 * stream still carries the whole utterance). */
#define RT_ASR_PCM_MAX        (256 * 1024)
#define RT_ASR_RESP_MAX       (64 * 1024)
/* Generous: the POST carries ~160 KB of base64 audio and the engine may be
 * finishing the previous turn's generation; the host probe measures 0.8 s on a
 * warm engine, but a 10 s cap aborted the round trip before the answer (#301). */
#define RT_ASR_TIMEOUT_MS     30000
/* The Qwen lane sends the WHOLE reply audio in ONE delta (~214 KB for 4.5 s),
 * far past the 64 KB playback slot: decode into this scratch and split across
 * the fixed slots. */
#define RT_DECODE_MAX         (256 * 1024)

/* The prompt asks for the language + transcript as ONE JSON object so the
 * console can carry an honest detected language (`RT ASR language=`) instead of
 * a script guess.  The parser also accepts a plain transcript. */
#define RT_ASR_PROMPT                                                         \
    "Transcribe the audio verbatim. Reply with only a JSON object, no prose: " \
    "{\"language\":\"<ISO 639-1 code>\",\"text\":\"<verbatim transcript>\"}"

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
static bool              s_render_reply;   /* stream the reply onto the RLCD */
/* Turn-stage watchdog: the turn runs on the voice task, and a blocking HTTP /
 * socket call is invisible in the console until it returns.  The stage + its
 * age are logged once a stage is held >2 s, so a wedged commit/ASR is named in
 * the gate's console diagnostics instead of looking like a silent turn. */
enum { RT_STAGE_IDLE = 0, RT_STAGE_CAPTURE, RT_STAGE_COMMIT, RT_STAGE_ASR,
       RT_STAGE_RESPONSE, RT_STAGE_DONE };
static volatile int     s_stage;
static volatile int64_t s_stage_since;
static TaskHandle_t     s_wd_task;
static int               s_listen_retries; /* explicit re-asks after listen */
static bool              s_saw_think;      /* the reply carried a think block */
static bool              s_think_suppress; /* ... so server audio is dropped  */
static uint32_t          s_dropped_deltas;  /* audio deltas discarded (think)  */
static volatile bool     s_play_go;        /* text known -> playback may run   */

/* Playback queue slots, allocated once. */
static uint8_t *s_slots[CONFIG_ROBODOG_RT_QUEUE_DEPTH];
static uint8_t *s_dec;   /* base64 decode scratch, allocated once */

/* Capture -> socket uplink ring, allocated once (PSRAM): raw 16 kHz frames. */
static uint8_t        *s_up_ring;
static size_t          s_up_head;      /* next write slot (monotonic) */
static size_t          s_up_tail;      /* next read slot  (monotonic) */
static SemaphoreHandle_t s_up_mtx;
static TaskHandle_t    s_up_task;
static volatile uint32_t s_up_dropped;  /* frames dropped (ring full)     */
static volatile bool     s_up_sending;  /* sender inside build+send       */
static volatile bool     s_up_fault;    /* the socket is down             */
static volatile uint32_t s_up_sent_bytes;
static volatile uint32_t s_up_msgs;
static volatile uint32_t s_up_last_ms;
static volatile uint32_t s_up_max_ms;
static volatile uint64_t s_up_ms_sum;

/* Explicit ASR: the captured utterance (16 kHz PCM16) + the response body.
 * Both allocated once in PSRAM. */
static uint8_t *s_asr_pcm;
static size_t   s_asr_len;
static int16_t *s_asr_up;      /* resample scratch (PSRAM), kept across turns */
static size_t   s_asr_up_cap;
static bool     s_asr_truncated;
static char    *s_asr_resp;

/* ── base64 ─────────────────────────────────────────────────────────────── */

/* Encode into a caller-owned buffer (no per-message malloc: the sender runs
 * once per 20-60 ms and heap churn is the Phase 2 R7 class of bug). */
static size_t rt_b64_encode_into(const uint8_t *src, size_t len,
                                 char *dst, size_t cap)
{
    if (dst == NULL || cap < 4 * ((len + 2) / 3) + 1) {
        return 0;
    }
    size_t olen = 0;
    if (mbedtls_base64_encode((unsigned char *)dst, cap, &olen, src, len) != 0) {
        return 0;
    }
    dst[olen] = '\0';
    return olen;
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
    if (s_render_reply && clean > s_ui_shown) {
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
/* Push one decoded PCM chunk onto the playback queue.  Slots are fixed; when
 * the queue is full the chunk is dropped (a wedged playback path is already
 * reported through RT_BIT_ERR).  A chunk is capped at the slot size, so the
 * caller splits a big delta across several calls. */
static void rt_enqueue_audio(const uint8_t *pcm, size_t len, uint32_t rate)
{
    static size_t next_slot;
    if (len == 0 || len > CONFIG_ROBODOG_RT_DELTA_MAX_BYTES) {
        ESP_LOGW(TAG, "audio chunk %u B outside 1..%d; dropped",
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
        ESP_LOGW(TAG, "playback queue full; dropping %u B chunk",
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

/* One WS audio delta may be the WHOLE reply (the Qwen lane sends ~214 KB in a
 * single event).  Decode into s_dec, then split it across the fixed slots so
 * playback still runs through the paced mixer track. */
static void rt_queue_audio_delta(const uint8_t *pcm, size_t len, uint32_t rate)
{
    size_t off = 0;
    while (off < len) {
        size_t chunk = len - off;
        if (chunk > CONFIG_ROBODOG_RT_DELTA_MAX_BYTES) {
            chunk = CONFIG_ROBODOG_RT_DELTA_MAX_BYTES;
        }
        chunk &= ~(size_t)1; /* whole int16 samples */
        if (chunk == 0) {
            break;
        }
        rt_enqueue_audio(pcm + off, chunk, rate);
        off += chunk;
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
        /* The text-only (AGENT-shape) response is NOT this turn's answer:
         * claw_core answers from the transcript.  Its reply must not be
         * rendered, and a think block in it must not trigger the re-speak
         * fallback (that would voice the unused model answer over the agent's
         * TTS read-out).  Only an audio-shaped (OMNI) response is ours. */
        if (s_want_audio && !s_think_suppress &&
            robodog_think_present(s_raw_reply)) {
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
        if (!s_want_audio) {
            /* The Qwen lane emits audio even for a modalities:["text"] request
             * (measured).  This turn asked for text only, so the audio is not
             * ours: do not decode it (no slots, no spoke flag, no CPU). */
            return;
        }
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
                                       RT_DECODE_MAX);
        if (n == 0) {
            ESP_LOGW(TAG, "audio delta did not decode (%u b64 chars, cap %d)",
                     (unsigned)strlen(b64), RT_DECODE_MAX);
            return;
        }
        rt_queue_audio_delta(s_dec, n, rate);
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
    /* The bench gate prints only the last 12 console lines that match its
     * keyword set (ASR / capture / speech / utterance / SPIRAM / bail), so a
     * bare "websocket closed" is INVISIBLE in a failed-turn report: every
     * turn-ending path must carry one of those words (#301). */
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        ESP_LOGW(TAG, "RT websocket closed/disconnected; the capture/ASR turn "
                 "cannot continue (status=%d errno=%d)",
                 ev->error_handle.esp_ws_handshake_status_code,
                 ev->error_handle.esp_transport_sock_errno);
        s_session_ready = false;
        xEventGroupSetBits(s_evts, RT_BIT_ERR);
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "RT websocket error in the capture/ASR turn type=%d "
                 "status=%d errno=%d",
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

/* Send one text frame with an explicit timeout.  A timeout on an audio append
 * is NOT a dead socket: the sender counts the dropped message and keeps the
 * session (the next turn must not pay a full reconnect for a slow moment). */
static esp_err_t rt_send_timeout(const char *json, uint32_t timeout_ms)
{
    if (s_ws == NULL || !esp_websocket_client_is_connected(s_ws)) {
        /* Drop the cached session so the next turn reconnects rather than
         * reusing a dead handle (a dead engine or a dropped socket would
         * otherwise make every later turn fail identically). */
        s_session_ready = false;
        return ESP_ERR_INVALID_STATE;
    }
    const int n = esp_websocket_client_send_text(s_ws, json, (int)strlen(json),
                                                 pdMS_TO_TICKS(timeout_ms));
    if (n < 0) {
        if (!esp_websocket_client_is_connected(s_ws)) {
            s_session_ready = false;
            return ESP_ERR_INVALID_STATE;
        }
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* Control messages (handshake, commit, response.create) may wait for the tx
 * buffer; audio appends use the short timeout above. */
static esp_err_t rt_send(const char *json)
{
    return rt_send_timeout(json, RT_WS_TIMEOUT_MS);
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
    /* NO input_audio_transcription here: the deployed Qwen lane accepts and
     * echoes the field but NEVER emits
     * `conversation.item.input_audio_transcription.completed` (bench-probed),
     * and the code's claimed rt_dispatch fallback never existed — the
     * transcript was only ever assigned from that event.  The transcript now
     * comes from the explicit chat-completions ASR round trip in
     * rt_asr_utterance(), so the dead field is gone. */
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

/* ── uplink: capture task -> sender task ────────────────────────────────── */

static void rt_up_reset(void)
{
    if (s_up_mtx == NULL) {
        return;
    }
    xSemaphoreTake(s_up_mtx, portMAX_DELAY);
    s_up_head = 0;
    s_up_tail = 0;
    xSemaphoreGive(s_up_mtx);
    s_up_dropped = 0;
    s_up_sent_bytes = 0;
    s_up_msgs = 0;
    s_up_last_ms = 0;
    s_up_max_ms = 0;
    s_up_ms_sum = 0;
    s_up_fault = false;
}

/* Capture side: push one raw 16 kHz frame.  A FULL ring drops the OLDEST frame
 * and counts it; the reader NEVER blocks on the network. */
static void rt_up_push(const int16_t *frame)
{
    if (s_up_ring == NULL || s_up_mtx == NULL) {
        return;
    }
    xSemaphoreTake(s_up_mtx, portMAX_DELAY);
    if (s_up_head - s_up_tail >= RT_UP_FRAMES) {
        s_up_tail++;
        s_up_dropped++;
    }
    memcpy(s_up_ring + (s_up_head % RT_UP_FRAMES) * RT_FRAME_BYTES,
           frame, RT_FRAME_BYTES);
    s_up_head++;
    xSemaphoreGive(s_up_mtx);
}

static bool rt_up_pop(uint8_t *out)
{
    bool ok = false;
    xSemaphoreTake(s_up_mtx, portMAX_DELAY);
    if (s_up_head != s_up_tail) {
        memcpy(out, s_up_ring + (s_up_tail % RT_UP_FRAMES) * RT_FRAME_BYTES,
               RT_FRAME_BYTES);
        s_up_tail++;
        ok = true;
    }
    xSemaphoreGive(s_up_mtx);
    return ok;
}

static bool rt_up_empty(void)
{
    xSemaphoreTake(s_up_mtx, portMAX_DELAY);
    const bool empty = (s_up_head == s_up_tail);
    xSemaphoreGive(s_up_mtx);
    return empty;
}

/* Wait until every captured frame has been sent (or dropped) so the commit
 * seals the WHOLE utterance. */
static bool rt_up_drain(uint32_t timeout_ms)
{
    const int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    for (;;) {
        if (rt_up_empty() && !s_up_sending) {
            return true;
        }
        if (esp_timer_get_time() >= deadline) {
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

/* Sender task: drain the ring, resample to the session rate, base64, coalesce
 * up to RT_UP_COALESCE frames into ONE append, sent with a budget that absorbs
 * backpressure (a zero-length write makes esp_websocket_client abort the whole
 * connection, so the timeout must never be tight).  The
 * wall time of build+send is what the once-per-second pump log reports, so
 * "pump too slow" and "socket blocked" are distinguishable in one run. */
static void rt_uplink_task(void *arg)
{
    (void)arg;
    static uint8_t raw[RT_UP_COALESCE][RT_FRAME_BYTES];
    static int16_t up[RT_UP_COALESCE * RT_FRAME_SAMPLES *
                      RT_RATE_OUT_DEFAULT / RT_RATE_IN + 8];
    static char b64[RT_UP_MSG_MAX];
    static char msg[RT_UP_MSG_MAX];
    for (;;) {
        if (!rt_up_pop(raw[0])) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        /* Set BEFORE the coalesce pops: the turn's drain must not see the ring
         * empty while this batch is still about to be sent (it would commit
         * before the last frames reached the session). */
        s_up_sending = true;
        size_t nf = 1;
        while (nf < RT_UP_COALESCE && rt_up_pop(raw[nf])) {
            nf++;
        }
        const int64_t t0 = esp_timer_get_time();
        size_t out_n = 0;
        for (size_t i = 0; i < nf; i++) {
            out_n += robodog_pcm_resample(
                (const int16_t *)raw[i], RT_FRAME_SAMPLES, up + out_n,
                sizeof(up) / sizeof(up[0]) - out_n, RT_RATE_IN,
                RT_RATE_OUT_DEFAULT);
        }
        const size_t b64_len = rt_b64_encode_into(
            (const uint8_t *)up, out_n * sizeof(int16_t), b64, sizeof(b64));
        int mlen = -1;
        if (b64_len > 0) {
            mlen = snprintf(msg, sizeof(msg),
                            "{\"type\":\"input_audio_buffer.append\","
                            "\"audio\":\"%s\"}", b64);
        }
        esp_err_t err = ESP_FAIL;
        if (mlen > 0 && (size_t)mlen < sizeof(msg)) {
            err = rt_send_timeout(msg, RT_UP_SEND_TIMEOUT_MS);
        } else {
            ESP_LOGE(TAG, "RT append message build failed (%d)", mlen);
        }
        const uint32_t ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        s_up_last_ms = ms;
        if (ms > s_up_max_ms) {
            s_up_max_ms = ms;
        }
        s_up_msgs++;
        s_up_ms_sum += ms;
        if (err == ESP_OK) {
            s_up_sent_bytes += (uint32_t)(out_n * sizeof(int16_t));
        } else {
            s_up_dropped += (uint32_t)nf;
            if (err == ESP_ERR_INVALID_STATE && !robodog_rt_connected()) {
                /* The client tore the link down (a zero-length write aborts
                 * the connection): nothing can reach the session any more, so
                 * this is the only append failure that ends the turn. */
                s_up_fault = true;
                ESP_LOGE(TAG, "RT append failed: socket down, the capture/ASR "
                         "turn ends");
            } else {
                ESP_LOGW(TAG, "RT append did not complete in %u ms; dropped "
                         "%u capture frame(s) (socket still up=%d)",
                         (unsigned)ms, (unsigned)nf,
                         (int)robodog_rt_connected());
            }
        }
        s_up_sending = false;
    }
}

static void rt_pump_log(uint32_t t_ms, uint32_t frames, uint32_t captured_bytes,
                        bool ever_speech, uint32_t speech_ms,
                        uint32_t silence_ms, int32_t vad_max, int32_t peak)
{
    const uint32_t avg_ms =
        s_up_msgs != 0 ? (uint32_t)(s_up_ms_sum / s_up_msgs) : 0;
    /* `vad_max`/`peak` are the mic levels the VAD actually saw: the gate reads
     * a silent capture (level below RT_VAD_ENERGY for the whole window) apart
     * from a stalled pump without a second run (issue #301). */
    ESP_LOGI(TAG, "RT pump t=%ums frames=%u captured=%u B up_dropped=%u "
             "ever_speech=%d speech=%ums silence=%ums vad_max=%d peak=%d "
             "send last=%ums avg=%ums max=%ums msgs=%u sent=%u B",
             (unsigned)t_ms, (unsigned)frames, (unsigned)captured_bytes,
             (unsigned)s_up_dropped, (int)ever_speech, (unsigned)speech_ms,
             (unsigned)silence_ms, (int)vad_max, (int)peak,
             (unsigned)s_up_last_ms, (unsigned)avg_ms,
             (unsigned)s_up_max_ms, (unsigned)s_up_msgs,
             (unsigned)s_up_sent_bytes);
}

static const char *rt_stage_name(int stage)
{
    switch (stage) {
    case RT_STAGE_CAPTURE:  return "capture";
    case RT_STAGE_COMMIT:   return "commit";
    case RT_STAGE_ASR:      return "asr";
    case RT_STAGE_RESPONSE: return "response";
    case RT_STAGE_DONE:     return "done";
    default:                return "idle";
    }
}

static void rt_stage_set(int stage)
{
    s_stage = stage;
    s_stage_since = esp_timer_get_time();
}

/* ONE line for every turn-ending fault, carrying the gate's diagnostic
 * keywords: the bench prints only the last 12 matching lines, so a failure
 * logged under a bare "turn failed" never reaches the report (#301). */
static void rt_turn_fail(const char *why, esp_err_t err)
{
    ESP_LOGE(TAG, "RT capture/ASR turn failed: why=%s err=%s ws=%d "
             "up_fault=%d up_dropped=%u stage=%s",
             why, esp_err_to_name(err), (int)robodog_rt_connected(),
             (int)s_up_fault, (unsigned)s_up_dropped,
             rt_stage_name(s_stage));
}

/* Logs the current stage (with its age + heap + socket state) once it has been
 * held for >2 s: the one diagnostic that distinguishes a wedged socket write
 * from a wedged HTTP round trip on the bench console. */
static void rt_watchdog_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        const int stage = s_stage;
        if (stage == RT_STAGE_IDLE) {
            continue;
        }
        const int64_t held_us = esp_timer_get_time() - s_stage_since;
        if (held_us < 2000000) {
            continue;
        }
        /* The prefix carries the gate's diagnostic keywords (ASR / capture):
         * the bench gate prints only the LAST 12 console lines matching them,
         * and the idle ring-overflow warnings would otherwise hide this. */
        ESP_LOGW(TAG, "RT turn watchdog ASR/capture: stage=%s held=%us ws=%d "
                 "int_free=%u psram_free=%u",
                 rt_stage_name(stage), (unsigned)(held_us / 1000000),
                 (int)robodog_rt_connected(),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    }
}

/* ── explicit ASR (chat/completions, same omni model) ───────────────────── */

/* The deployed Qwen lane never emits the realtime transcription event, so the
 * transcript comes from one HTTP round trip to the SAME model endpoint:
 *   POST <realtime host>/v1/chat/completions  with input_audio (base64 WAV).
 * No new service, and the console contract stays `RT ASR text="..."`. */
static void rt_asr_url(char *out, size_t cap)
{
    const char *u = CONFIG_ROBODOG_RT_URL;
    const char *scheme = "http://";
    const char *rest = u;
    if (strncmp(u, "ws://", 5) == 0) {
        rest = u + 5;
    } else if (strncmp(u, "wss://", 6) == 0) {
        scheme = "https://";
        rest = u + 6;
    } else if (strncmp(u, "http://", 7) == 0) {
        rest = u + 7;
    } else if (strncmp(u, "https://", 8) == 0) {
        scheme = "https://";
        rest = u + 8;
    }
    const char *slash = strchr(rest, '/');
    const size_t host_len = slash != NULL ? (size_t)(slash - rest)
                                          : strlen(rest);
    snprintf(out, cap, "%s%.*s/v1/chat/completions", scheme,
             (int)host_len, rest);
}

/* Bytes the ASR post actually put on the wire: a Content-Length that does not
 * match what was written leaves the server waiting for the rest of the body,
 * so the response never comes (#301). */
static size_t s_asr_written;

static esp_err_t rt_http_write_all(esp_http_client_handle_t client,
                                   const void *data, size_t len)
{
    const uint8_t *p = (const uint8_t *)data;
    while (len > 0) {
        const int w = esp_http_client_write(client, (const char *)p, (int)len);
        if (w <= 0) {
            return ESP_FAIL;
        }
        p += (size_t)w;
        len -= (size_t)w;
        s_asr_written += (size_t)w;
    }
    return ESP_OK;
}

/* Streaming base64: encode `data` as it arrives, carrying the 1-2 byte
 * remainder between chunks, so the WAV header + PCM are ONE base64 stream
 * without a body-sized contiguous copy. */
static esp_err_t rt_b64_write(esp_http_client_handle_t client,
                              uint8_t *carry, size_t *carry_len,
                              const uint8_t *data, size_t len)
{
    static uint8_t in[RT_B64_CHUNK + 2];
    static char out[4 * (RT_B64_CHUNK + 2) / 3 + 8];
    while (len > 0) {
        const size_t take = len > RT_B64_CHUNK ? RT_B64_CHUNK : len;
        memcpy(in, carry, *carry_len);
        memcpy(in + *carry_len, data, take);
        const size_t total = *carry_len + take;
        const size_t n3 = (total / 3) * 3;
        if (n3 > 0) {
            size_t olen = 0;
            if (mbedtls_base64_encode((unsigned char *)out, sizeof(out),
                                      &olen, in, n3) != 0) {
                return ESP_FAIL;
            }
            if (rt_http_write_all(client, out, olen) != ESP_OK) {
                return ESP_FAIL;
            }
        }
        *carry_len = total - n3;
        memcpy(carry, in + n3, *carry_len);
        data += take;
        len -= take;
    }
    return ESP_OK;
}

static esp_err_t rt_b64_flush(esp_http_client_handle_t client,
                              const uint8_t *carry, size_t carry_len)
{
    if (carry_len == 0) {
        return ESP_OK;
    }
    char out[8];
    size_t olen = 0;
    if (mbedtls_base64_encode((unsigned char *)out, sizeof(out), &olen,
                              carry, carry_len) != 0) {
        return ESP_FAIL;
    }
    return rt_http_write_all(client, out, olen);
}

static void rt_asr_utterance(robodog_rt_turn_t *out)
{
    if (s_asr_pcm == NULL || s_asr_resp == NULL || s_asr_len == 0) {
        ESP_LOGW(TAG, "RT ASR: no captured PCM (pcm=%p resp=%p len=%u)",
                 (void *)s_asr_pcm, (void *)s_asr_resp, (unsigned)s_asr_len);
        ESP_LOGI(TAG, "RT ASR text=\"\"");
        return;
    }
    s_asr_resp[0] = '\0';
    ESP_LOGI(TAG, "RT ASR start: pcm=%u B resp_cap=%u int_free=%u "
             "psram_free=%u heap_ok=%d",
             (unsigned)s_asr_len, (unsigned)RT_ASR_RESP_MAX,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
             (int)heap_caps_check_integrity_all(true));
    if (s_asr_truncated) {
        ESP_LOGW(TAG, "RT ASR: utterance truncated to %u B (~%u s)",
                 (unsigned)s_asr_len,
                 (unsigned)(s_asr_len / (RT_RATE_IN * 2)));
    }

    /* Post at the omni server's audio rate (CONFIG_ROBODOG_RT_ASR_RATE): the
     * realtime handler hardcodes 24 kHz, so a WAV carrying the raw 16 kHz
     * capture rate comes back as an empty transcript (#301).  The capture stays
     * 16 kHz (the hub's rate); only this post is resampled up.  Falls back to
     * the raw capture when the SPIRAM scratch cannot be allocated. */
    const size_t pcm_samples = s_asr_len / sizeof(int16_t);
    const uint32_t asr_rate = CONFIG_ROBODOG_RT_ASR_RATE;
    int16_t *asr_pcm = NULL;
    size_t asr_samples = pcm_samples;
    if (asr_rate != RT_RATE_IN && pcm_samples > 0) {
        const size_t cap = pcm_samples * asr_rate / RT_RATE_IN + 8;
        /* ONE scratch, kept across turns: a per-turn malloc/free in this path
         * was followed by a `tlsf_free ... block already marked as free`
         * assert on the bench (#301), and the voice task is the only user. */
        if (s_asr_up == NULL || s_asr_up_cap < cap) {
            int16_t *n = heap_caps_realloc(s_asr_up, cap * sizeof(int16_t),
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (n != NULL) {
                s_asr_up = n;
                s_asr_up_cap = cap;
            }
        }
        asr_pcm = s_asr_up;
        if (asr_pcm != NULL && s_asr_up_cap >= cap) {
            asr_samples = robodog_pcm_resample((const int16_t *)s_asr_pcm,
                                               pcm_samples, asr_pcm, cap,
                                               RT_RATE_IN, asr_rate);
            ESP_LOGI(TAG, "RT ASR resampled: %u -> %u samples at %u Hz "
                     "(scratch=%p cap=%u heap_ok=%d)", (unsigned)pcm_samples,
                     (unsigned)asr_samples, (unsigned)asr_rate,
                     (void *)s_asr_up, (unsigned)s_asr_up_cap,
                     (int)heap_caps_check_integrity_all(true));
        } else {
            asr_pcm = NULL;
            ESP_LOGW(TAG, "RT ASR: no SPIRAM to resample to %u Hz; posting "
                     "the raw %d Hz capture", (unsigned)asr_rate, RT_RATE_IN);
        }
    }
    const uint8_t *post_pcm = asr_pcm != NULL ? (const uint8_t *)asr_pcm
                                              : s_asr_pcm;
    const size_t post_len = asr_samples * sizeof(int16_t);
    const uint32_t post_rate = asr_pcm != NULL ? asr_rate : RT_RATE_IN;
    uint8_t wav[44];
    const size_t wav_len = robodog_wav_header(wav, sizeof(wav), post_len,
                                              post_rate, RT_CH, RT_BITS);
    const size_t wav_bytes = wav_len + post_len;
    const size_t b64_len = 4 * ((wav_bytes + 2) / 3);

    /* Static (one turn at a time) so the voice task's 8 KB stack is not
     * consumed by the request envelope. */
    static char url[160];
    static char head[768];
    static char tail[768];
    rt_asr_url(url, sizeof(url));
    char *prompt = robodog_json_escape(RT_ASR_PROMPT);
    if (prompt == NULL) {
        ESP_LOGE(TAG, "RT ASR: no memory for the prompt escape");
        return;
    }
    const int hl = snprintf(
        head, sizeof(head),
        "{\"model\":\"%s\",\"modalities\":[\"text\"],\"messages\":["
        "{\"role\":\"user\",\"content\":["
        "{\"type\":\"input_audio\",\"input_audio\":{\"data\":\"",
        CONFIG_ROBODOG_RT_MODEL);
    const int tl = snprintf(
        tail, sizeof(tail),
        "\",\"format\":\"wav\"}},{\"type\":\"text\",\"text\":\"%s\"}]}]}",
        prompt);
    free(prompt);
    if (hl <= 0 || (size_t)hl >= sizeof(head) || tl <= 0 ||
        (size_t)tl >= sizeof(tail)) {
        ESP_LOGE(TAG, "RT ASR: request envelope overflow");
        ESP_LOGI(TAG, "RT ASR text=\"\"");
        return;
    }
    const size_t body_len = (size_t)hl + b64_len + (size_t)tl;
    ESP_LOGI(TAG, "RT ASR request: %s model=%s audio=%d Hz/%d ch/%d bit "
             "(%u B pcm, %u B wav, %u B body, int_free=%u psram_free=%u)",
             url, CONFIG_ROBODOG_RT_MODEL, (int)post_rate, RT_CH, RT_BITS,
             (unsigned)post_len, (unsigned)wav_bytes, (unsigned)body_len,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    const esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = RT_ASR_TIMEOUT_MS,
        .buffer_size = 512,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(TAG, "RT ASR: http client init failed");
        ESP_LOGI(TAG, "RT ASR text=\"\"");
        return;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    s_asr_written = 0;
    const int64_t t_post = esp_timer_get_time();
    esp_err_t err = esp_http_client_open(client, (int)body_len);
    ESP_LOGI(TAG, "RT ASR open: %s (content-length=%u)", esp_err_to_name(err),
             (unsigned)body_len);
    uint8_t carry[2] = { 0, 0 };
    size_t carry_len = 0;
    if (err == ESP_OK) {
        err = rt_http_write_all(client, head, (size_t)hl);
    }
    if (err == ESP_OK) {
        err = rt_b64_write(client, carry, &carry_len, wav, wav_len);
    }
    if (err == ESP_OK) {
        err = rt_b64_write(client, carry, &carry_len, post_pcm, post_len);
    }
    if (err == ESP_OK) {
        err = rt_b64_flush(client, carry, carry_len);
    }
    if (err == ESP_OK) {
        /* The closing `","format":"wav"}},{...}]}]}` tail is part of the
         * declared Content-Length: leaving it out makes the server wait for
         * the rest of the body and never answer (#301). */
        err = rt_http_write_all(client, tail, (size_t)tl);
    }
    /* Self-checking accounting: a short write against the declared length
     * means the server is still waiting for the rest, so the round trip can
     * only time out.  Fail loudly here instead of burning the deadline. */
    if (err == ESP_OK && s_asr_written != body_len) {
        ESP_LOGE(TAG, "RT ASR: body incomplete, wrote %u of %u B (head=%u "
                 "b64=%u tail=%u); the server cannot answer",
                 (unsigned)s_asr_written, (unsigned)body_len, (unsigned)hl,
                 (unsigned)b64_len, (unsigned)tl);
        err = ESP_FAIL;
    }
    ESP_LOGI(TAG, "RT ASR body sent: %s (wrote %u B of %u B in %u ms)",
             esp_err_to_name(err), (unsigned)s_asr_written,
             (unsigned)body_len,
             (unsigned)((esp_timer_get_time() - t_post) / 1000));
    size_t rx_len = 0;
    if (err == ESP_OK) {
        /* fetch_headers() must not be fire-and-forget: it is what parses the
         * status line, and a failure here is the ONLY thing `http=-1` used to
         * say (#301). */
        const esp_err_t fh = esp_http_client_fetch_headers(client);
        const int hdr_status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "RT ASR headers: %s status=%d content_len=%d",
                 esp_err_to_name(fh), hdr_status,
                 (int)esp_http_client_get_content_length(client));
        const int64_t t_rx = esp_timer_get_time();
        while (rx_len + 1 < RT_ASR_RESP_MAX) {
            const int r = esp_http_client_read(
                client, s_asr_resp + rx_len,
                (int)(RT_ASR_RESP_MAX - 1 - rx_len));
            if (r > 0) {
                rx_len += (size_t)r;
                continue;
            }
            if (r < 0) {
                ESP_LOGW(TAG, "RT ASR read: transport error after %u B",
                         (unsigned)rx_len);
                break;
            }
            /* r == 0 is "no bytes arrived YET", not EOF: the old loop broke
             * here and gave up while the server was still working.  Stop only
             * when the response is complete or the deadline has passed. */
            if (esp_http_client_is_complete_data_received(client)) {
                break;
            }
            if ((esp_timer_get_time() - t_rx) / 1000 > RT_ASR_TIMEOUT_MS) {
                ESP_LOGW(TAG, "RT ASR read: no data after %d ms (%u B so far)",
                         RT_ASR_TIMEOUT_MS, (unsigned)rx_len);
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        s_asr_resp[rx_len] = '\0';
    }
    const int status = esp_http_client_get_status_code(client);
    ESP_LOGI(TAG, "RT ASR http=%d rx=%u B in %u ms", status,
             (unsigned)rx_len,
             (unsigned)((esp_timer_get_time() - t_post) / 1000));
    /* The endpoint's answer is the only way to tell "rejected request" from
     * "model returned nothing" - log it before parsing (#301). */
    ESP_LOGI(TAG, "RT ASR body: '%.500s'", s_asr_resp[0] != '\0'
                                               ? s_asr_resp : "(empty)");
    esp_http_client_cleanup(client);
    ESP_LOGI(TAG, "RT ASR done: heap_ok=%d",
             (int)heap_caps_check_integrity_all(true));

    if (err != ESP_OK || status < 200 || status >= 300 || rx_len == 0) {
        ESP_LOGE(TAG, "RT ASR: round-trip failed (http=%d err=%s wrote=%u/%u B "
                 "body='%.160s')", status, esp_err_to_name(err),
                 (unsigned)s_asr_written, (unsigned)body_len,
                 s_asr_resp[0] != '\0' ? s_asr_resp : "(none)");
        ESP_LOGI(TAG, "RT ASR text=\"\" http=%d rx=%u B in %u ms audio=%u B "
                 "at %u Hz body='%.400s'", status, (unsigned)rx_len,
                 (unsigned)((esp_timer_get_time() - t_post) / 1000),
                 (unsigned)post_len, (unsigned)post_rate,
                 s_asr_resp[0] != '\0' ? s_asr_resp : "(none)");
        return;
    }
    char lang[16] = "";
    char text[512] = "";
    if (!robodog_asr_parse(s_asr_resp, lang, sizeof(lang), text, sizeof(text))) {
        ESP_LOGE(TAG, "RT ASR: no transcript in body='%.160s'", s_asr_resp);
        ESP_LOGI(TAG, "RT ASR text=\"\" http=%d rx=%u B in %u ms audio=%u B "
                 "at %u Hz body='%.400s'", status, (unsigned)rx_len,
                 (unsigned)((esp_timer_get_time() - t_post) / 1000),
                 (unsigned)post_len, (unsigned)post_rate, s_asr_resp);
        return;
    }
    if (lang[0] == '\0') {
        strlcpy(lang, robodog_utf8_has_cjk(text) ? "zh" : "en", sizeof(lang));
    }
    strlcpy(out->transcript, text, sizeof(out->transcript));
    ESP_LOGI(TAG, "RT ASR text=\"%s\" http=%d rx=%u B in %u ms audio=%u B at "
             "%u Hz", out->transcript, status, (unsigned)rx_len,
             (unsigned)((esp_timer_get_time() - t_post) / 1000),
             (unsigned)post_len, (unsigned)post_rate);
    ESP_LOGI(TAG, "RT ASR language=%s prob=0.00", lang);
    robodog_ui_show_user(out->transcript);
}

/* ── capture ────────────────────────────────────────────────────────────── */

/* Capture the utterance and push it into the session as it arrives.  The reader
 * only touches the mic ring + the uplink ring: the network work is the sender
 * task's, so a slow socket can never stall this loop (issue #301).
 * Returns samples captured (0 = nothing captured) and sets `fault` on a real
 * capture fault (never on "the user said nothing"). */
static size_t rt_stream_utterance(bool *fault, bool keep_pcm)
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
    rt_up_reset();
    s_asr_len = 0;
    s_asr_truncated = false;
    ESP_LOGI(TAG, "RT listen started (16k/1/16, VAD energy=%d silence=%dms)",
             RT_VAD_ENERGY, CONFIG_ROBODOG_RT_SILENCE_MS);
    robodog_ui_set_agent_state("listening");

    static int16_t frame[RT_FRAME_SAMPLES];
    size_t fi = 0;
    size_t frames = 0;          /* frames pushed into the uplink ring */
    size_t captured_bytes = 0;  /* bytes read from the mic ring        */
    uint32_t silence_ms = 0;
    uint32_t speech_ms = 0;
    bool ever_speech = false;
    bool stream_capped = false;
    int64_t stream_t0 = -1;
    int64_t speech_t0 = -1;
    const int64_t t0 = esp_timer_get_time();
    int64_t last_log = t0;
    int32_t vad_max = 0;   /* highest frame mean |sample| seen (the VAD metric) */
    int32_t vad_min = 0x7fffffff; /* lowest frame mean (the mic noise floor) */
    int32_t peak = 0;      /* highest single |sample| seen in the window      */
    const int64_t no_speech_us = (int64_t)RT_NO_SPEECH_MS * 1000LL;
    const int64_t cap_us = (int64_t)CONFIG_ROBODOG_RT_MAX_SECS * 1000000LL;

    for (;;) {
        const int64_t now = esp_timer_get_time();
        if (now - t0 >= cap_us) {
            ESP_LOGW(TAG, "RT utterance capped at %d s (vad_max=%d peak=%d)",
                     CONFIG_ROBODOG_RT_MAX_SECS, (int)vad_max, (int)peak);
            break;
        }
        if (!ever_speech && now - t0 >= no_speech_us) {
            /* `vad_max` well under RT_VAD_ENERGY = the mic heard nothing
             * (silent rig / dead input), not a stalled capture pump. */
            ESP_LOGW(TAG, "RT no speech within %d ms (vad_max=%d vad_min=%d "
                     "peak=%d), bailing", RT_NO_SPEECH_MS, (int)vad_max,
                     (int)vad_min, (int)peak);
            break;
        }
        if (ever_speech && now - speech_t0 >= cap_us) {
            break;
        }

        const size_t want = RT_FRAME_SAMPLES - fi;
        const size_t n = audio_capture_sub_read(sub, (uint8_t *)(frame + fi),
                                                want * sizeof(int16_t), 150);
        if (n > 0) {
            captured_bytes += n;
            fi += n / sizeof(int16_t);
        }
        if (fi < RT_FRAME_SAMPLES) {
            /* Still waiting for a whole frame; the settle point below must
             * still run so a quiet ring can end a real utterance. */
        } else {
            int64_t sum = 0;
            int32_t fpk = 0;
            for (size_t i = 0; i < RT_FRAME_SAMPLES; i++) {
                const int32_t v = frame[i];
                const int32_t a = v < 0 ? -v : v;
                sum += a;
                if (a > fpk) {
                    fpk = a;
                }
            }
            const int32_t mean = (int32_t)(sum / (int64_t)RT_FRAME_SAMPLES);
            const bool speech = mean > RT_VAD_ENERGY;
            if (mean > vad_max) {
                vad_max = mean;
            }
            if (mean < vad_min) {
                vad_min = mean;
            }
            if (fpk > peak) {
                peak = fpk;
            }

            /* Stream EVERY frame once speech has begun (word tails included);
             * pre-speech silence is not sent, so the model never hears the
             * room before the utterance.  The SENDER resamples to the session
             * rate; this loop only copies 640 B. */
            if (speech || ever_speech) {
                if (stream_t0 < 0) {
                    stream_t0 = now;
                }
                if (!stream_capped &&
                    now - stream_t0 >= (int64_t)RT_STREAM_MAX_MS * 1000) {
                    stream_capped = true;
                    ESP_LOGW(TAG, "RT stream capped at %d ms (model gets the "
                             "first %d ms; ASR keeps capturing)",
                             RT_STREAM_MAX_MS, RT_STREAM_MAX_MS);
                }
                if (!stream_capped) {
                    rt_up_push(frame);
                    frames++;
                }
                if (keep_pcm && s_asr_pcm != NULL) {
                    if (s_asr_len + RT_FRAME_BYTES <= RT_ASR_PCM_MAX) {
                        memcpy(s_asr_pcm + s_asr_len, frame, RT_FRAME_BYTES);
                        s_asr_len += RT_FRAME_BYTES;
                    } else {
                        s_asr_truncated = true;
                    }
                }
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

        const int64_t now2 = esp_timer_get_time();
        if (now2 - last_log >= 1000000LL) {
            rt_pump_log((uint32_t)((now2 - t0) / 1000), (uint32_t)frames,
                        (uint32_t)captured_bytes, ever_speech, speech_ms,
                        silence_ms, vad_max, peak);
            last_log = now2;
        }
    }

    rt_pump_log((uint32_t)((esp_timer_get_time() - t0) / 1000),
                (uint32_t)frames, (uint32_t)captured_bytes, ever_speech,
                speech_ms, silence_ms, vad_max, peak);
    /* A tlsf_free assert right after this line has twice killed a bench turn
     * (#301).  Probe both heaps here so the report says whether the corruption
     * predates the ASR round trip or is caused by it. */
    const bool heap_ok = heap_caps_check_integrity_all(true);
    ESP_LOGI(TAG, "RT capture done: frames=%u captured=%u B asr=%u B%s "
             "up_dropped=%u up_fault=%d ws=%d heap_ok=%d ever_speech=%d "
             "speech=%ums silence=%ums vad_max=%d vad_min=%d peak=%d",
             (unsigned)frames, (unsigned)captured_bytes, (unsigned)s_asr_len,
             s_asr_truncated ? " (truncated)" : "", (unsigned)s_up_dropped,
             (int)s_up_fault, (int)robodog_rt_connected(), (int)heap_ok,
             (int)ever_speech,
             (unsigned)speech_ms, (unsigned)silence_ms,
             (int)vad_max, (int)vad_min, (int)peak);
    /* Close the subscriber for the idle gap: the capture task only reads the
     * codec while a subscriber is open, so leaving it open between turns
     * captures ~32 KB/s into a ring nobody drains (the ring-overflow warning
     * flood the bench console showed in issue #301) and keeps the mic hot.
     * The next turn reopens it and flushes the ring. */
    ESP_LOGI(TAG, "RT capture close: begin sub=%p heap_ok=%d int_free=%u "
             "psram_free=%u", (void *)sub, (int)heap_caps_check_integrity_all(true),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    const esp_err_t cerr = audio_capture_close_subscriber(sub);
    ESP_LOGI(TAG, "RT capture close: done err=%s heap_ok=%d int_free=%u "
             "psram_free=%u", esp_err_to_name(cerr),
             (int)heap_caps_check_integrity_all(true),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    sub = NULL;
    if (captured_bytes == 0) {
        ESP_LOGE(TAG, "RT capture stream produced no audio (codec/I2S RX?)");
        *fault = true;
    }
    return frames * RT_FRAME_SAMPLES;
}

esp_err_t robodog_rt_turn(robodog_rt_mode_t mode, robodog_rt_turn_t *out)
{
    if (!s_inited || out == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    memset(out, 0, sizeof(*out));
    memset(&s_turn, 0, sizeof(s_turn));
    ESP_LOGI(TAG, "RT turn start: mode=%d (%s), keep_pcm=%d",
             (int)mode,
             mode == ROBODOG_RT_MODE_OMNI ? "omni"
                 : mode == ROBODOG_RT_MODE_AGENT ? "agent" : "auto",
             (int)(mode != ROBODOG_RT_MODE_OMNI));
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
    s_render_reply = false;

    esp_err_t err = robodog_rt_connect();
    if (err != ESP_OK) {
        out->fault = true;
        return err;
    }
    xEventGroupClearBits(s_evts, RT_BIT_DONE | RT_BIT_ERR);

    /* Let the previous turn's sender finish, then capture.  The reader only
     * fills the uplink ring; the sender owns the socket, so this loop can
     * always reach its own no-speech / cap / silence exits (issue #301). */
    (void)rt_up_drain(RT_UP_DRAIN_MS);

    rt_stage_set(RT_STAGE_CAPTURE);
    bool cap_fault = false;
    const bool keep_pcm = (mode != ROBODOG_RT_MODE_OMNI);
    const size_t samples = rt_stream_utterance(&cap_fault, keep_pcm);
    if (cap_fault) {
        out->fault = true;
        rt_turn_fail("capture-fault", ESP_FAIL);
        ESP_LOGE(TAG, "RT capture fault; ending turn");
        robodog_ui_set_agent_state("error");
        rt_stage_set(RT_STAGE_IDLE); /* never leave the watchdog on capture */
        return ESP_FAIL;
    }
    if (samples == 0) {
        ESP_LOGW(TAG, "RT no usable utterance");
        robodog_ui_set_agent_state("idle");
        rt_stage_set(RT_STAGE_IDLE); /* the turn ended: not still capturing */
        return ESP_OK;
    }
    s_turn.captured = true; /* the utterance is now in the session */

    if (s_up_fault) {
        out->fault = true;
        rt_turn_fail("uplink-down", ESP_FAIL);
        ESP_LOGE(TAG, "RT uplink socket down; ending the capture/ASR turn");
        robodog_ui_set_agent_state("error");
        rt_stage_set(RT_STAGE_IDLE);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "RT uplink drain: begin (post-capture) heap_ok=%d",
             (int)heap_caps_check_integrity_all(true));
    const bool drained = rt_up_drain(RT_UP_DRAIN_MS);
    ESP_LOGI(TAG, "RT uplink drain: done drained=%d heap_ok=%d ws=%d",
             (int)drained, (int)heap_caps_check_integrity_all(true),
             (int)robodog_rt_connected());
    if (!drained) {
        ESP_LOGW(TAG, "RT uplink drain timed out; committing the frames sent");
    }

    /* Explicit ASR FIRST, then the commit.  In turn mode the server starts
     * generating the reply as soon as the commit lands, and the engine
     * serialises requests: a POST issued after the commit queues behind that
     * generation and blew the ASR timeout with zero bytes read (#301).  The
     * utterance is already in the session's input buffer, so committing after
     * the ASR seals exactly the same audio. */
    if (mode != ROBODOG_RT_MODE_OMNI) {
        rt_stage_set(RT_STAGE_ASR);
        rt_asr_utterance(out);
        /* Keep the transcript in the per-turn accumulator too: the final
         * memcpy(out, &s_turn) must not lose it. */
        strlcpy(s_turn.transcript, out->transcript,
                sizeof(s_turn.transcript));
    }

    /* Commit: seals the streamed audio into a user item (a small control
     * message).  The response.create below picks the shape the router chose. */
    rt_stage_set(RT_STAGE_COMMIT);
    char commit[160];
    snprintf(commit, sizeof(commit),
             "{\"type\":\"input_audio_buffer.commit\"}");
    if (rt_send(commit) != ESP_OK) {
        out->fault = true;
        out->captured = true;
        rt_turn_fail("commit-send", ESP_FAIL);
        rt_stage_set(RT_STAGE_IDLE);
        return ESP_FAIL;
    }

    /* The AGENT shape routes on the USER's words, and the deployed Qwen lane
     * does not emit the realtime transcription event — so the transcript comes
     * from the explicit chat-completions ASR round trip (same model endpoint,
     * logged as `RT ASR text=`).  The OMNI shape needs no transcript. */
    bool want_audio;
    if (mode == ROBODOG_RT_MODE_OMNI) {
        want_audio = true;
    } else {
        const char *reason = NULL;
        const robodog_rt_mode_t route =
            (mode == ROBODOG_RT_MODE_AGENT)
                ? ROBODOG_RT_MODE_AGENT
                : robodog_rt_route(out->transcript, &reason);
        if (mode != ROBODOG_RT_MODE_AGENT) {
            ESP_LOGI(TAG, "RT route=%s reason=%s",
                     route == ROBODOG_RT_MODE_AGENT ? "agent" : "omni",
                     reason != NULL ? reason : "?");
        }
        want_audio = (route == ROBODOG_RT_MODE_OMNI);
        if (want_audio && out->transcript[0] == '\0') {
            ESP_LOGW(TAG, "RT no transcript; answering on the omni path");
        }
    }
    s_want_audio = want_audio;
    /* Only the audio-shaped (OMNI) response is this turn's answer and may be
     * rendered on the RLCD; an AGENT-shape text response is unused (claw_core
     * answers from the transcript). */
    s_render_reply = want_audio;

    char resp[256];
    snprintf(resp, sizeof(resp),
             "{\"type\":\"response.create\",\"response\":{\"modalities\":[%s]%s}}",
             want_audio ? "\"audio\",\"text\"" : "\"text\"",
             want_audio ? "" : ",\"max_output_tokens\":256");
    rt_stage_set(RT_STAGE_RESPONSE);
    s_commit_us = esp_timer_get_time();
    if (rt_send(resp) != ESP_OK) {
        out->fault = true;
        out->captured = true;
        rt_stage_set(RT_STAGE_IDLE);
        return ESP_FAIL;
    }
    robodog_ui_set_agent_state(want_audio ? "responding" : "processing");

    audio_mixer_track_handle_t track = NULL;
    if (want_audio) {
        err = rt_playback_begin(&track);
        if (err != ESP_OK) {
            out->fault = true;
            rt_stage_set(RT_STAGE_IDLE);
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
        rt_stage_set(RT_STAGE_IDLE);
        return ESP_FAIL;
    }
    if ((bits & RT_BIT_DONE) == 0) {
        ESP_LOGE(TAG, "RT no response.done within %d ms",
                 CONFIG_ROBODOG_RT_TIMEOUT_MS);
        out->fault = true;
        robodog_ui_set_agent_state("error");
        rt_stage_set(RT_STAGE_IDLE);
        return ESP_ERR_TIMEOUT;
    }

    rt_stage_set(RT_STAGE_DONE);
    memcpy(out, &s_turn, sizeof(*out));
    ESP_LOGI(TAG, "RT turn done: transcript=\"%.80s\" reply=\"%.80s\" "
             "first_audio=%u ms captured=%d think=%d dropped=%u",
             out->transcript, out->reply, (unsigned)out->first_audio_ms,
             (int)out->captured, (int)s_saw_think,
             (unsigned)s_dropped_deltas);

    /* Full, line-safe reply for the E2E gate (the `RT turn done` line above is
     * truncated): JSON-escaped so a quote/newline cannot split the log. */
    if (want_audio && out->reply[0] != '\0') {
        char *esc = robodog_json_escape(out->reply);
        ESP_LOGI(TAG, "RT reply text=%s",
                 esc != NULL ? esc : out->reply);
        free(esc);
    }

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
    rt_stage_set(RT_STAGE_IDLE);
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

    /* The decode scratch holds a WHOLE reply delta: the Qwen lane sends one
     * ~214 KB audio delta (4.5 s), then it is split across the 64 KB slots. */
    s_dec = heap_caps_malloc(RT_DECODE_MAX,
                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_dec == NULL) {
        ESP_LOGE(TAG, "RT no SPIRAM for the delta decode buffer (%d B)",
                 RT_DECODE_MAX);
        return ESP_ERR_NO_MEM;
    }

    /* Capture -> socket uplink ring (raw 16 kHz frames) + its mutex. */
    s_up_ring = heap_caps_malloc(RT_UP_FRAMES * RT_FRAME_BYTES,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_up_mtx = xSemaphoreCreateMutex();
    if (s_up_ring == NULL || s_up_mtx == NULL) {
        ESP_LOGE(TAG, "RT no SPIRAM/mutex for the uplink ring");
        return ESP_ERR_NO_MEM;
    }

    /* Explicit-ASR utterance PCM + response body. */
    s_asr_pcm = heap_caps_malloc(RT_ASR_PCM_MAX,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_asr_resp = heap_caps_malloc(RT_ASR_RESP_MAX,
                                  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_asr_pcm == NULL || s_asr_resp == NULL) {
        ESP_LOGE(TAG, "RT no SPIRAM for the ASR buffers (%d + %d B)",
                 RT_ASR_PCM_MAX, RT_ASR_RESP_MAX);
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
    if (xTaskCreate(rt_uplink_task, "robodog_rt_up", RT_PLAY_STACK, NULL,
                    RT_PLAY_PRIO, &s_up_task) != pdPASS) {
        ESP_LOGE(TAG, "RT uplink task failed");
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(rt_watchdog_task, "robodog_rt_wd", 3072, NULL, 2,
                    &s_wd_task) != pdPASS) {
        ESP_LOGE(TAG, "RT watchdog task failed");
        return ESP_ERR_NO_MEM;
    }
    s_inited = true;
    ESP_LOGI(TAG, "RoboDog Phase 3 realtime ready: %s (model %s)",
             CONFIG_ROBODOG_RT_URL, CONFIG_ROBODOG_RT_MODEL);
    ESP_LOGI(TAG, "RT memory: rx=%u B dec=%d B slots=%u x %d B uplink=%u x %u B "
             "asr=%d B",
             (unsigned)s_rx_cap, RT_DECODE_MAX, (unsigned)slots,
             CONFIG_ROBODOG_RT_DELTA_MAX_BYTES, RT_UP_FRAMES,
             (unsigned)RT_FRAME_BYTES, RT_ASR_PCM_MAX);
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
