/*
 * RoboDog Phase 2 voice loop (LMAO-owned).
 *
 * The standalone on-device voice loop for the ESP-Claw board (DESIGN.md
 * Phase 2).  No AEC/barge-in in v1: the mic subscriber is open only while
 * capturing a turn and closed before TTS playback — natural half-duplex that
 * never routes the mic into the speaker path.
 *
 * Console hooks for the host E2E (robodog-claw-e2e asserts on these):
 *   - `rd_listen` command        -> "RD_LISTEN" turn request log
 *   - capture start              -> "listen started" log
 *   - Whisper transcription      -> `ASR text="..."` log
 *   - agent reply finalized      -> `agent reply: <text>` log
 *   - TTS playback bracket       -> `TTS start` / `TTS stop` logs
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_voice.h"
#include "robodog_ui.h"
#include "robodog_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_console.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "driver/gpio.h"
#include "esp_intr_alloc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "cJSON.h"

#include "claw_agent_mgr.h"
#include "claw_core.h"
#include "audio_hub.h"
#include "wifi_manager.h"

static const char *TAG = "robodog_voice";

/* ── Capture / VAD geometry ─────────────────────────────────────────────── */
/* The board's audio hub (and I2S codec) runs 16 kHz / 1 ch / 16-bit (see
 * board_peripherals.yaml + app_claw_audio_start_service).  VAD thresholds
 * match the bridge exactly (voice_bridge.py VAD_SILENCE_MS=800, VAD_ENERGY=300)
 * so the E2E turn semantics carry over unchanged. */
#define VOICE_RATE            16000
#define VOICE_CH              1
#define VOICE_BITS            16
#define VOICE_FRAME_MS        20
#define VOICE_FRAME_SAMPLES   ((VOICE_RATE * VOICE_FRAME_MS) / 1000) /* 320 */
#define VOICE_FRAME_BYTES     (VOICE_FRAME_SAMPLES * VOICE_CH * (VOICE_BITS / 8))
#define VAD_ENERGY            300  /* int16 mean-abs amplitude = speech */
#define VAD_SILENCE_MS        800  /* trailing silence ends the utterance */

#define VOICE_MAX_SAMPLES     (CONFIG_ROBODOG_VOICE_MAX_SECS * VOICE_RATE)

#define EVT_LISTEN            BIT0
#define VOICE_STACK           8192

static EventGroupHandle_t s_voice_evts;
static TaskHandle_t s_voice_task;
static volatile bool s_turn_active;

/* ── Energy VAD (software; DESIGN's fallback since esp-sr is absent) ────── */

static bool frame_is_speech(const int16_t *pcm, size_t samples)
{
    int64_t sum = 0;
    for (size_t i = 0; i < samples; i++) {
        int32_t v = pcm[i];
        sum += (v < 0) ? -(int32_t)v : (int32_t)v;
    }
    return (sum / (int64_t)samples) > VAD_ENERGY;
}

/* ── Capture one utterance (16 kHz s16 mono) ────────────────────────────── */
/* Opens the shared audio_hub APP subscriber, energy-VADs to the end of the
 * utterance (800 ms trailing silence after the first speech frame), caps at
 * CONFIG_ROBODOG_VOICE_MAX_SECS, and bails early if no speech is detected at
 * all.  Returns the captured SAMPLES via *out_samples and a SPIRAM buffer the
 * caller owns. */
static int16_t *voice_capture(size_t *out_samples)
{
    *out_samples = 0;
    audio_capture_handle_t cap = NULL;
    if (audio_hub_get_capture(&cap) != ESP_OK) {
        ESP_LOGE(TAG, "capture hub unavailable (audio_hub not started?)");
        return NULL;
    }
    const audio_capture_sub_format_t fmt = {
        .sample_rate = VOICE_RATE,
        .channels = VOICE_CH,
        .bits = VOICE_BITS,
    };
    audio_capture_sub_handle_t sub = NULL;
    esp_err_t err = audio_capture_open_subscriber(cap, AUDIO_CAPTURE_SUB_APP,
                                                  &fmt, "robodog_voice", &sub);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "open APP capture subscriber failed: %s", esp_err_to_name(err));
        return NULL;
    }
    ESP_LOGI(TAG, "listen started (16k/1/16, VAD energy=%d silence=%dms)",
             VAD_ENERGY, VAD_SILENCE_MS);
    robodog_ui_set_agent_state("listening");

    int16_t *buf = heap_caps_malloc(VOICE_MAX_SAMPLES * sizeof(int16_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "no SPIRAM for %u-sample capture buffer",
                 (unsigned)VOICE_MAX_SAMPLES);
        audio_capture_close_subscriber(sub);
        return NULL;
    }

    const int64_t t0 = esp_timer_get_time();
    const int64_t no_speech_us = (int64_t)CONFIG_ROBODOG_VOICE_NO_SPEECH_TIMEOUT_MS * 1000LL;
    int16_t frame[VOICE_FRAME_SAMPLES];
    size_t fi = 0;      /* samples buffered in frame */
    size_t got = 0;     /* total samples committed to buf */
    bool ever_speech = false;
    uint32_t silence_ms = 0;

    for (;;) {
        const int64_t now = esp_timer_get_time();
        if (now - t0 >= (int64_t)CONFIG_ROBODOG_VOICE_MAX_SECS * 1000000LL) {
            break; /* hard cap */
        }
        if (!ever_speech && now - t0 >= no_speech_us) {
            ESP_LOGW(TAG, "no speech within %d ms, bailing",
                     CONFIG_ROBODOG_VOICE_NO_SPEECH_TIMEOUT_MS);
            break;
        }
        size_t want = (size_t)(VOICE_FRAME_SAMPLES - fi);
        size_t n = audio_capture_sub_read(sub, (uint8_t *)(frame + fi),
                                          want * (VOICE_BITS / 8), 150);
        if (n == 0) {
            if (ever_speech && silence_ms >= VAD_SILENCE_MS) {
                break; /* trailing silence elapsed */
            }
            continue;
        }
        fi += n / (VOICE_BITS / 8);
        if (fi < VOICE_FRAME_SAMPLES) {
            continue; /* wait for a whole frame */
        }
        const bool speech = frame_is_speech(frame, VOICE_FRAME_SAMPLES);
        if (speech) {
            ever_speech = true;
            silence_ms = 0;
        } else if (ever_speech) {
            silence_ms += VOICE_FRAME_MS;
        }
        size_t space = VOICE_MAX_SAMPLES - got;
        size_t cp = (space < VOICE_FRAME_SAMPLES) ? space : VOICE_FRAME_SAMPLES;
        memcpy(buf + got, frame, cp * sizeof(int16_t));
        got += cp;
        fi = 0;
        if (ever_speech && silence_ms >= VAD_SILENCE_MS) {
            break;
        }
    }

    audio_capture_close_subscriber(sub);
    *out_samples = got;
    return buf;
}

/* ── ASR: 16 kHz PCM -> Whisper text ────────────────────────────────────── */

/* Build a canonical RIFF/WAV header for 16 kHz mono 16-bit PCM. */
static size_t build_wav_header(uint8_t *dst, size_t dst_cap, size_t data_bytes)
{
    const size_t hdr = 44;
    if (dst_cap < hdr) {
        return 0;
    }
    const uint32_t sample_rate = VOICE_RATE;
    const uint16_t channels = VOICE_CH;
    const uint16_t bits = VOICE_BITS;
    const uint32_t byte_rate = sample_rate * channels * (bits / 8);
    const uint16_t block_align = (uint16_t)(channels * (bits / 8));
#define PUT32(p, v) do {                                  \
        (p)[0] = (uint8_t)((v) & 0xff);                   \
        (p)[1] = (uint8_t)(((v) >> 8) & 0xff);            \
        (p)[2] = (uint8_t)(((v) >> 16) & 0xff);           \
        (p)[3] = (uint8_t)(((v) >> 24) & 0xff);           \
    } while (0)
#define PUT16(p, v) do {                                  \
        (p)[0] = (uint8_t)((v) & 0xff);                   \
        (p)[1] = (uint8_t)(((v) >> 8) & 0xff);            \
    } while (0)
    uint8_t *o = dst;
    memcpy(o, "RIFF", 4);   o += 4;
    PUT32(o, (uint32_t)(36 + data_bytes)); o += 4;
    memcpy(o, "WAVE", 4);   o += 4;
    memcpy(o, "fmt ", 4);   o += 4;
    PUT32(o, 16);           o += 4;
    PUT16(o, 1);            o += 2;   /* PCM */
    PUT16(o, channels);     o += 2;
    PUT32(o, sample_rate);  o += 4;
    PUT32(o, byte_rate);    o += 4;
    PUT16(o, block_align);  o += 2;
    PUT16(o, bits);         o += 2;
    memcpy(o, "data", 4);   o += 4;
    PUT32(o, (uint32_t)data_bytes); o += 4;
#undef PUT32
#undef PUT16
    return hdr;
}

/* POST captured PCM to Whisper; returns heap text (caller frees) or NULL. */
static char *voice_asr(const int16_t *pcm, size_t samples)
{
    const size_t pcm_bytes = samples * sizeof(int16_t);
    static const char boundary[] = "----RoboDogClawVoice";
    char header[384];
    const size_t head_len = (size_t)snprintf(
        header, sizeof(header),
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"model\"\r\n\r\n"
        "%s\r\n"
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n",
        boundary, CONFIG_ROBODOG_ASR_MODEL, boundary);
    char footer[80];
    const size_t foot_len = (size_t)snprintf(footer, sizeof(footer),
                                             "\r\n--%s--\r\n", boundary);

    uint8_t wav_hdr[44];
    const size_t wav_len = build_wav_header(wav_hdr, sizeof(wav_hdr), pcm_bytes);
    const size_t body_len = head_len + wav_len + pcm_bytes + foot_len;

    char *body = heap_caps_malloc(body_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (body == NULL) {
        ESP_LOGE(TAG, "ASR: no memory for %u B multipart body", (unsigned)body_len);
        return NULL;
    }
    size_t o = 0;
    memcpy(body + o, header, head_len);            o += head_len;
    memcpy(body + o, wav_hdr, wav_len);            o += wav_len;
    memcpy(body + o, pcm, pcm_bytes);              o += pcm_bytes;
    memcpy(body + o, footer, foot_len);            o += foot_len;

    robodog_http_rx_t rx;
    robodog_http_rx_init(&rx, CONFIG_ROBODOG_ASR_MAX_RESPONSE);
    char *text = NULL;
    if (rx.buf == NULL) {
        ESP_LOGE(TAG, "ASR: no memory for response buffer");
        heap_caps_free(body);
        return NULL;
    }
    esp_http_client_config_t cfg = {
        .url = CONFIG_ROBODOG_ASR_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 60000,
        .event_handler = robodog_http_event_handler,
        .user_data = &rx,
        .buffer_size = 512,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(TAG, "ASR: http client init failed");
        goto out;
    }
    char ctype[96];
    snprintf(ctype, sizeof(ctype), "multipart/form-data; boundary=%s", boundary);
    esp_http_client_set_header(client, "Content-Type", ctype);
    esp_http_client_set_post_field(client, body, body_len);
    const esp_err_t err = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err == ESP_OK && status >= 200 && status < 300 && rx.len > 0) {
        cJSON *root = cJSON_Parse(rx.buf);
        cJSON *t = root != NULL ? cJSON_GetObjectItemCaseSensitive(root, "text") : NULL;
        if (t != NULL && cJSON_IsString(t) && t->valuestring != NULL) {
            text = strdup(t->valuestring);
        }
        if (root != NULL) {
            cJSON_Delete(root);
        }
    } else {
        ESP_LOGE(TAG, "ASR: round-trip failed: http=%d err=%s body='%.160s'",
                 status, esp_err_to_name(err), rx.buf != NULL ? rx.buf : "");
    }
out:
    ESP_LOGI(TAG, "ASR text=\"%s\"", text != NULL ? text : "(empty)");
    robodog_http_rx_free(&rx);
    heap_caps_free(body);
    return text;
}

/* ── TTS playback: reply text -> 24 kHz WAV -> 16 kHz -> ES8311 ─────────── */

/* Detect the PCM payload offset of an RIFF/WAVE blob (robust to non-44-byte
 * headers). */
static size_t wav_data_offset(const uint8_t *wav, size_t len)
{
    if (len < 12 || memcmp(wav, "RIFF", 4) != 0 || memcmp(wav + 8, "WAVE", 4) != 0) {
        return 0;
    }
    size_t o = 12;
    while (o + 8 <= len) {
        const uint32_t chunk_size = (uint32_t)wav[o + 4] | ((uint32_t)wav[o + 5] << 8) |
                                    ((uint32_t)wav[o + 6] << 16) | ((uint32_t)wav[o + 7] << 24);
        if (memcmp(wav + o, "data", 4) == 0) {
            return o + 8;
        }
        o += 8 + chunk_size + (chunk_size & 1u);
    }
    return 0;
}

/* Linear 24 kHz -> 16 kHz resample (ratio 2/3). */
static size_t resample_24_to_16(const int16_t *in, size_t in_n,
                                int16_t *out, size_t out_cap)
{
    size_t o = 0;
    double pos = 0.0;
    const double step = 16000.0 / 24000.0;
    while (o < out_cap && (size_t)pos + 1 < in_n) {
        const size_t i = (size_t)pos;
        const double frac = pos - (double)i;
        const double v = (double)in[i] * (1.0 - frac) + (double)in[i + 1] * frac;
        out[o++] = (int16_t)v;
        pos += step;
    }
    return o;
}

/* ── TTS playback: reply text -> 24 kHz WAV -> 16 kHz -> ES8311 ─────────── */

/* JSON-escape a string for embedding in the TTS `input` field.  Reply text
 * contains newlines/quotes (the agent's multi-line markdown), and an unescaped
 * control char makes the JSON invalid -> FastAPI 422.  Caller frees. */
static char *json_escape(const char *s)
{
    size_t cap = strlen(s) * 6 + 8;
    char *out = malloc(cap);
    if (out == NULL) {
        return NULL;
    }
    size_t o = 0;
    for (const char *p = s; *p != '\0'; p++) {
        unsigned char c = (unsigned char)*p;
        const char *esc = NULL;
        switch (c) {
        case '"':  esc = "\\\""; break;
        case '\\': esc = "\\\\"; break;
        case '\n': esc = "\\n";  break;
        case '\r': esc = "\\r";  break;
        case '\t': esc = "\\t";  break;
        default:   break;
        }
        if (esc != NULL) {
            size_t l = strlen(esc);
            memcpy(out + o, esc, l);
            o += l;
        } else if (c < 0x20) {
            o += (size_t)snprintf(out + o, cap - o, "\\u%04x", c);
        } else {
            out[o++] = (char)c;
        }
    }
    out[o] = '\0';
    return out;
}

/* Bound the text spoken by TTS to a length that yields a reasonable audio
 * duration (the esp-claw root agent is verbose by default and will happily
 * dump multi-paragraph capability lists -> 50 s of speech -> the resample
 * buffer blows up).  The full reply still renders on the RLCD; only what is
 * spoken is clipped.  Clips at a sentence/line break within the cap, falling
 * back to the last space, then a hard cutoff. */
static void tts_clip(const char *reply, char *out, size_t out_cap)
{
    const size_t len = strlen(reply);
    size_t max = CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS;
    if (out_cap - 1 < max) {
        max = out_cap - 1;
    }
    if (len <= max) {
        strlcpy(out, reply, out_cap);
        return;
    }
    size_t cut = 0;
    for (size_t i = max; i > 0; i--) {
        const char c = reply[i];
        if (c == '.' || c == '!' || c == '?' || c == ';' || c == '\n') {
            cut = i + 1;
            break;
        }
    }
    if (cut == 0) {
        for (size_t i = max; i > 0; i--) {
            if (reply[i] == ' ') {
                cut = i;
                break;
            }
        }
    }
    if (cut == 0) {
        cut = max;
    }
    memcpy(out, reply, cut);
    out[cut] = '\0';
}

static void voice_speak(const char *reply)
{
    char clip[CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS + 1];
    tts_clip(reply, clip, sizeof(clip));
    char *esc = json_escape(clip);
    if (esc == NULL) {
        ESP_LOGE(TAG, "TTS: json_escape failed");
        return;
    }
    char *json = NULL;
    const int json_len = snprintf(NULL, 0,
                                  "{\"model\":\"%s\",\"input\":\"%s\",\"voice\":\"%s\"}",
                                  CONFIG_ROBODOG_TTS_MODEL, esc,
                                  CONFIG_ROBODOG_TTS_VOICE);
    if (json_len > 0) {
        json = malloc((size_t)json_len + 1);
        if (json != NULL) {
            snprintf(json, (size_t)json_len + 1,
                     "{\"model\":\"%s\",\"input\":\"%s\",\"voice\":\"%s\"}",
                     CONFIG_ROBODOG_TTS_MODEL, esc, CONFIG_ROBODOG_TTS_VOICE);
        }
    }
    free(esc);
    if (json == NULL) {
        ESP_LOGE(TAG, "TTS: no memory for request JSON");
        return;
    }

    robodog_http_rx_t rx;
    robodog_http_rx_init(&rx, CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE);
    if (rx.buf == NULL) {
        ESP_LOGE(TAG, "TTS: no memory for response buffer");
        free(json);
        return;
    }
    esp_http_client_config_t cfg = {
        .url = CONFIG_ROBODOG_TTS_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 60000,
        .event_handler = robodog_http_event_handler,
        .user_data = &rx,
        .buffer_size = 512,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(TAG, "TTS: http client init failed");
        free(json);
        goto out;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    /* Keep `json` alive across perform(): set_post_field stores the pointer,
     * it does not copy.  Freeing now -> freed-memory body -> Kokoro 400. */
    esp_http_client_set_post_field(client, json, strlen(json));
    const esp_err_t err = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(json);
    if (err != ESP_OK || !(status >= 200 && status < 300)) {
        ESP_LOGE(TAG, "TTS: round-trip failed: http=%d err=%s", status,
                 esp_err_to_name(err));
        goto out;
    }
    const size_t data_off = wav_data_offset((const uint8_t *)rx.buf, rx.len);
    if (data_off == 0 || data_off == rx.len) {
        ESP_LOGE(TAG, "TTS: response is not a RIFF/WAVE blob (%u B)", (unsigned)rx.len);
        goto out;
    }
    const size_t in_samples = (rx.len - data_off) / sizeof(int16_t);
    const size_t out_cap = in_samples * 2 / 3 + 4;
    int16_t *out16 = heap_caps_malloc(out_cap * sizeof(int16_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (out16 == NULL) {
        ESP_LOGE(TAG, "TTS: no SPIRAM for %u-sample resample buffer",
                 (unsigned)out_cap);
        goto out;
    }
    const size_t out_samples = resample_24_to_16(
        (const int16_t *)(rx.buf + data_off), in_samples, out16, out_cap);

    audio_mixer_handle_t mix = NULL;
    audio_mixer_track_handle_t track = NULL;
    if (audio_hub_get_mixer(&mix) != ESP_OK ||
        audio_mixer_open_track(mix, AUDIO_MIXER_TRACK_APP, "robodog_voice",
                               &track) != ESP_OK) {
        ESP_LOGE(TAG, "TTS: could not open APP mixer track");
        heap_caps_free(out16);
        goto out;
    }
    ESP_LOGI(TAG, "TTS start (%u samples -> %u)", (unsigned)in_samples,
             (unsigned)out_samples);
    robodog_ui_set_agent_state("responding");
    /* Write blocks on a full ring, draining in real-time at 16 kHz — this is
     * the free downlink pacing (the old Opus DOWNLINK_PACE problem). */
    (void)audio_mixer_track_write(track, out16, out_samples * sizeof(int16_t));
    (void)audio_mixer_track_flush(track); /* wait until the panel drains */
    ESP_LOGI(TAG, "TTS stop");
    audio_mixer_close_track(track);
    heap_caps_free(out16);
out:
    robodog_http_rx_free(&rx);
}

/* ── One listen->ASR->agent->TTS turn ───────────────────────────────────── */

static void voice_turn(void)
{
    if (s_turn_active) {
        ESP_LOGW(TAG, "a voice turn is already running; ignoring");
        return;
    }
    s_turn_active = true;

    wifi_manager_status_t ws;
    wifi_manager_get_status(&ws);
    if (!ws.sta_connected) {
        ESP_LOGE(TAG, "WiFi not connected yet; aborting voice turn");
        robodog_ui_set_agent_state("error");
        s_turn_active = false;
        return;
    }

    size_t samples = 0;
    int16_t *pcm = voice_capture(&samples);
    if (pcm == NULL || samples < VOICE_FRAME_SAMPLES * 2) {
        ESP_LOGW(TAG, "no usable utterance captured (%u samples)",
                 (unsigned)samples);
        if (pcm != NULL) {
            heap_caps_free(pcm);
        }
        robodog_ui_set_agent_state("idle");
        s_turn_active = false;
        return;
    }

    char *text = voice_asr(pcm, samples);
    heap_caps_free(pcm);
    if (text == NULL || text[0] == '\0') {
        ESP_LOGW(TAG, "ASR returned no text; ending turn");
        if (text != NULL) {
            free(text);
        }
        robodog_ui_set_agent_state("idle");
        s_turn_active = false;
        return;
    }

    /* Render the user transcript (same role/timing as a user message
     * finalizing), then mark the agent as reasoning while the LLM is in
     * flight; the reply is appended when it finalizes (stale-caption safe). */
    robodog_ui_show_user(text);

    uint32_t request_id = 0;
    esp_err_t err = claw_agent_mgr_start_root_run_text(
        text, NULL, 0, CONFIG_ROBODOG_LLM_TIMEOUT_MS, &request_id);
    free(text);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "start_root_run_text failed: %s", esp_err_to_name(err));
        robodog_ui_set_agent_state("error");
        s_turn_active = false;
        return;
    }
    robodog_ui_set_agent_state("processing");

    claw_core_response_t resp;
    memset(&resp, 0, sizeof(resp));
    err = claw_agent_mgr_receive_root_for(request_id, &resp,
                                          CONFIG_ROBODOG_LLM_TIMEOUT_MS + 30000);
    if (err == ESP_OK && resp.text != NULL) {
        ESP_LOGI(TAG, "agent reply: %s", resp.text);
        robodog_ui_show_assistant(resp.text);
        voice_speak(resp.text);
    } else {
        ESP_LOGE(TAG, "agent run failed (%s): %s", esp_err_to_name(err),
                 resp.error_message != NULL ? resp.error_message : "");
        robodog_ui_set_agent_state("error");
    }
    robodog_ui_set_agent_state("idle");
    s_turn_active = false;
}

/* ── Task + trigger ─────────────────────────────────────────────────────── */

/* BOOT button (GPIO0): physical push-to-talk.  Falling-edge ISR with a 300 ms
 * debounce sets the same EVT_LISTEN bit the `rd_listen` command sets, so the
 * one voice task wakes for both. */
#define BOOT_BUTTON_GPIO GPIO_NUM_0
#define BOOT_BUTTON_DEBOUNCE_US 300000UL

static void IRAM_ATTR boot_button_isr(void *arg)
{
    (void)arg;
    static uint64_t s_last_us;
    const uint64_t now = esp_timer_get_time();
    if (now - s_last_us < BOOT_BUTTON_DEBOUNCE_US) {
        return;
    }
    s_last_us = now;
    BaseType_t hpw = pdFALSE;
    if (s_voice_evts != NULL) {
        xEventGroupSetBitsFromISR(s_voice_evts, EVT_LISTEN, &hpw);
    }
    if (hpw) {
        portYIELD_FROM_ISR(hpw);
    }
}

static esp_err_t voice_init_boot_button(void)
{
#if CONFIG_ROBODOG_VOICE_BOOT_BUTTON
    const gpio_config_t io = {
        .pin_bit_mask = BIT64(BOOT_BUTTON_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    gpio_config(&io);
    esp_err_t err = gpio_install_isr_service(ESP_INTR_FLAG_LOWMED);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "gpio isr service install: %s", esp_err_to_name(err));
    }
    err = gpio_isr_handler_add(BOOT_BUTTON_GPIO, boot_button_isr, NULL);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "BOOT button ISR add failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "BOOT button armed on GPIO%d -> rd_listen (push to talk)",
             BOOT_BUTTON_GPIO);
#else
    ESP_LOGI(TAG, "BOOT button disabled (CONFIG_ROBODOG_VOICE_BOOT_BUTTON=n)");
#endif
    return ESP_OK;
}

static void voice_task(void *arg)
{
    (void)arg;
    for (;;) {
        xEventGroupWaitBits(s_voice_evts, EVT_LISTEN, pdTRUE /* clear */,
                            pdFALSE, portMAX_DELAY);
        ESP_LOGI(TAG, "RD_LISTEN -> voice turn");
        voice_turn();
    }
}

void robodog_voice_listen_start(void)
{
    if (s_voice_evts != NULL) {
        xEventGroupSetBits(s_voice_evts, EVT_LISTEN);
    }
}

static int cmd_rd_listen(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    robodog_voice_listen_start();
    return 0;
}

esp_err_t robodog_voice_init(void)
{
    s_voice_evts = xEventGroupCreate();
    if (s_voice_evts == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(voice_task, "robodog_voice", VOICE_STACK, NULL, 5,
                    &s_voice_task) != pdPASS) {
        vEventGroupDelete(s_voice_evts);
        s_voice_evts = NULL;
        return ESP_ERR_NO_MEM;
    }
    (void)voice_init_boot_button();
    const esp_console_cmd_t cmd = {
        .command = "rd_listen",
        .help = "voice loop: mic -> ASR -> agent -> TTS -> speaker",
        .func = &cmd_rd_listen,
    };
    if (esp_console_cmd_register(&cmd) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_listen' console command");
    }
    ESP_LOGI(TAG, "RoboDog Phase 2 voice loop ready: rd_listen");
    return ESP_OK;
}
