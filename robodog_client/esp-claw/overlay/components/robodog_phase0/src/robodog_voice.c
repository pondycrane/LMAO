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
#include "voice_split.h"
#include "voice_md.h"
#include "voice_pipeline.h"
#include "robodog_realtime.h"
#include "robodog_speaker.h"
#include "voice_pcm.h"
#include "robodog_utf8.h"

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

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
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "cJSON.h"

#include "claw_agent_mgr.h"
#include "claw_core.h"
#include "robodog_session.h"
#include "audio_hub.h"
#include "wifi_manager.h"
#include "esp_codec_dev.h"
#include "esp_board_manager.h"
#include "dev_audio_codec.h"

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
/* Minimum ACCUMULATED speech before trailing silence may end the utterance.
 * Without it a single noise blip (a chair creak, a click, a door) counted as
 * "speech", and the very next 800 ms of quiet ended the listen — typically
 * BEFORE the user had said anything, so the turn captured ~1.5 s of room
 * noise, ASR returned no text, and the panel dropped straight back to `idle`.
 * That is the other half of the reported "hit listen, it switches right back
 * to idle": the trigger worked, but a blip closed the window.  Below this
 * threshold the buffered audio is discarded as a blip and listening
 * continues (still bounded by the no-speech timeout and the hard caps). */
#define VAD_MIN_SPEECH_MS     CONFIG_ROBODOG_VOICE_MIN_SPEECH_MS

#define VOICE_MAX_SAMPLES     (CONFIG_ROBODOG_VOICE_MAX_SECS * VOICE_RATE)

/* Max consecutive no-progress rounds in the paced write loop before the
 * playback is aborted (each round waits ~500 ms in audio_mixer_track_write,
 * so the limit bounds a wedged ring to ~10 s instead of hanging the task). */
#define TTS_WRITE_STALL_LIMIT 20U

#define EVT_LISTEN            BIT0
#define EVT_RESET             BIT1  /* long-press middle button: clear session */
#define VOICE_STACK           8192

/* TTS chunk PIPELINE (see voice_pipeline.[ch]).  Qwen3-TTS synthesizes at
 * roughly real time and returns the WHOLE WAV at once (no streaming: ttfb ==
 * total), so a chunk cannot start before its synthesis finished.  Playing the
 * reply strictly sequentially therefore leaves a multi-second silence at every
 * sentence boundary whenever a fetch is slower than the previous chunk's
 * playback.  A small background task therefore keeps CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD
 * fetches in flight, so synthesis runs ahead of the speaker and consecutive
 * sentences join seamlessly.
 *
 * The CONFIG_ROBODOG_VOICE_TTS_SLOTS slot buffers (each
 * CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE bytes) are allocated ONCE for the
 * firmware's lifetime and reset in place (robodog_http_rx_reset) — no
 * per-chunk and no per-turn alloc/free churn (the DESIGN.md R7 tlsf panic
 * site stays untouched), and never a free under the fetch task's writer. */
#define PF_STACK            4096
#define PF_JOB_DEPTH        8      /* must cover CONFIG_ROBODOG_VOICE_TTS_SLOTS */
#define PF_DONE_DEPTH       8
/* Bounded wait for a chunk's fetch: esp_http_client's own timeout is 60 s, so
 * a fetch that is still in flight past this is wedged, not merely slow. */
#define PF_WAIT_MS          70000

_Static_assert(CONFIG_ROBODOG_VOICE_TTS_SLOTS <= ROBODOG_VOICE_PIPE_MAX_SLOTS,
               "CONFIG_ROBODOG_VOICE_TTS_SLOTS exceeds "
               "ROBODOG_VOICE_PIPE_MAX_SLOTS");
_Static_assert(CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD >= 1,
               "the TTS pipeline needs at least one lookahead fetch");

/* The RLCD's speaker amp enable (same PA the hermes firmware drives on
 * GPIO46).  esp-board-manager registers gpio_pa_control here (board
 * peripherals yaml, active_level=1) but the peripheral init applies the
 * struct-default output level 0, so the amp stays OFF unless we raise it —
 * the DAC gets real-time audio yet the speaker is silent.  Keep it HIGH for
 * the whole playback (idempotent, no re-toggle clicks). */
#define VOICE_PA_GPIO         GPIO_NUM_46

static EventGroupHandle_t s_voice_evts;
static TaskHandle_t s_voice_task;
static volatile bool s_turn_active;

/* Which voice path a turn takes.
 *   CLASSIC — the Phase 2 chain (capture -> Whisper -> claw_core -> Qwen3-TTS)
 *   OMNI    — the Phase 3 realtime session, omni answers and speaks the turn
 *   AUTO    — realtime session; the capability router decides per turn
 * `rd_voice_mode` switches it at runtime so the two paths can be measured
 * back to back on the same board (the E2E does exactly that). */
typedef enum {
    VOICE_PATH_CLASSIC = 0,
    VOICE_PATH_AUTO = 1,
    VOICE_PATH_OMNI = 2,
} voice_path_t;
static voice_path_t s_voice_path =
    (CONFIG_ROBODOG_RT_TURN_MODE == 1) ? VOICE_PATH_OMNI
    : (CONFIG_ROBODOG_RT_TURN_MODE == 2) ? VOICE_PATH_CLASSIC
    : VOICE_PATH_AUTO;

/* Set when the realtime session already transcribed the utterance, so the
 * Phase 2 chain must skip its own capture + Whisper legs (ownership moves to
 * the turn, which frees it). */
static char *s_rt_transcript;

/* The language of the TTS requests for the CURRENT turn.  The endpoint would
 * otherwise infer each chunk independently from its script, so a short
 * English-only chunk inside a Chinese reply could flip to the English-native
 * timbre mid-readout ("different accent in different sections").  The turn
 * sets this once from the ASR language; probes use TTS_LANG_AUTO. */
#define TTS_LANG_AUTO 0
#define TTS_LANG_EN   1
#define TTS_LANG_ZH   2
static volatile int s_tts_lang = TTS_LANG_AUTO;

/* TTS chunk pipeline: persistent slot buffers + ONE background fetch task.
 *
 * The task is the ONLY writer of a slot's rx.  It never touches the turn's
 * pipeline model (which lives on the voice task's stack): it reports each
 * completed fetch through a per-slot event bit plus a per-slot result code,
 * and the voice task applies commit()/fail() when it consumes the chunk.
 * That keeps the buffers static (allocated once, never freed under a writer —
 * DESIGN R7 invariant #4) and the turn's bookkeeping single-owner. */
#define TTS_SLOTS      CONFIG_ROBODOG_VOICE_TTS_SLOTS
#define PF_WORKERS     CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD
#define PF_QUEUE_DEPTH TTS_SLOTS
/* One IDLE bit per worker: teardown waits for ALL of them (each worker acks
 * when it holds no slot). */
#define EVT_PF_IDLE(i) BIT(TTS_SLOTS + (i))
#define EVT_PF_IDLE_ALL ((EventBits_t)((1u << PF_WORKERS) - 1u) << TTS_SLOTS)
#define EVT_PF_SLOT(n) BIT(n)

_Static_assert(TTS_SLOTS + PF_WORKERS <= 16,
               "slot + worker event bits must fit the event group");

static EventGroupHandle_t s_pf_evts;
static QueueHandle_t s_pf_q;
static bool s_pf_ready;                 /* workers + event group created OK */
static size_t s_pf_workers;             /* workers actually created (<= K) */
static volatile bool s_pf_abort;        /* turn teardown: stop fetching */
static robodog_http_rx_t s_tts_slot[TTS_SLOTS];
static volatile esp_err_t s_slot_result[TTS_SLOTS];
static size_t s_slot_count;             /* slots actually allocated (0..TTS_SLOTS) */

typedef struct {
    const char *chunk;  /* chunk text (points into the turn's chunk arena) */
    size_t index;       /* 1-based chunk index being fetched */
    size_t total;       /* chunk count of the turn (diagnostic) */
    int slot;           /* destination slot (only the worker writes it) */
} voice_pf_job_t;

/* Drive the speaker power-amplifier on (GPIO46 sees the struct-default LOW
 * from the board manager's peripheral init even though the DAC device hook
 * declares active_level=1 — see VOICE_PA_GPIO).  Configure as push-pull
 * output HIGH ourselves so the amp is up regardless of bmgr state. */
static void voice_dump_dac_regs(const char *tag)
{
    esp_codec_dev_handle_t h = robodog_speaker_dac();
    if (h == NULL) {
        ESP_LOGW(TAG, "audio diag: no audio_dac codec handle");
        return;
    }
    int pwr = -1, dac = -1, mute = -1, vol = -1;
    (void)esp_codec_dev_read_reg(h, 0x0D, &pwr);  /* SYSTEM_REG0D  power */
    (void)esp_codec_dev_read_reg(h, 0x12, &dac);  /* SYSTEM_REG12  DAC enable */
    (void)esp_codec_dev_read_reg(h, 0x31, &mute); /* DAC_REG31     mute */
    (void)esp_codec_dev_read_reg(h, 0x32, &vol);  /* DAC_REG32     volume */
    ESP_LOGI(TAG, "audio diag %s: REG0D=%02X REG12=%02X REG31(mute)=%02X REG32(vol)=%02X",
             tag, pwr, dac, mute, vol);
}

/* ── Paced mixer playback (single copy: tone diag + TTS) ────────────────── */

/* Feed a PCM buffer to the APP mixer track in ~20 ms frames with a ~19 ms
 * step so playback rides the codec rate in real time.  A single
 * audio_mixer_track_write() only waits AUDIO_MIXER_WRITE_TIMEOUT_MS (500 ms)
 * for its ~8-frame ring to drain — writing a whole buffer at once saturates
 * the ring and times out after ~0.5 s, so almost none of the audio ever
 * plays ("responding but silent").  Generates the tone at the MIXER's actual
 * sample rate (the shared audio hub runs 24 kHz after the hermes-parity
 * patch), so the heard pitch is exactly `freq`.  The stall limit bounds a
 * wedged ring so neither the diag nor the voice task can hang forever. */
static esp_err_t mixer_write_paced(audio_mixer_track_handle_t track,
                                   const uint8_t *src, size_t total,
                                   uint32_t play_rate)
{
    const size_t frame_bytes = play_rate * 20 / 1000 * sizeof(int16_t);
    size_t off = 0;
    unsigned stalls = 0;
    while (off < total) {
        size_t chunk = total - off;
        if (chunk > frame_bytes) {
            chunk = frame_bytes;
        }
        const size_t w = audio_mixer_track_write(track, src + off, chunk);
        if (w == 0) {
            if (++stalls >= TTS_WRITE_STALL_LIMIT) {
                ESP_LOGE(TAG, "playback stalled (mixer ring not draining); aborting");
                return ESP_ERR_TIMEOUT;
            }
        } else {
            stalls = 0;
        }
        off += w;
        if (w < chunk) {
            /* Ring momentarily full: let the mixer drain a frame before retry. */
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(19)); /* ~real-time step */
    }
    return ESP_OK;
}

static void voice_play_tone(unsigned ms, int freq)
{
    audio_mixer_handle_t mix = NULL;
    audio_mixer_track_handle_t track = NULL;
    if (audio_hub_get_mixer(&mix) != ESP_OK ||
        audio_mixer_open_track(mix, AUDIO_MIXER_TRACK_APP, "robodog_voice_diag",
                               &track) != ESP_OK || track == NULL) {
        ESP_LOGE(TAG, "audio diag: could not open APP mixer track");
        return;
    }
    /* Generate the tone at the MIXER's actual sample rate (24 kHz after the
     * hermes-parity audio-hub patch) so the heard pitch is exactly `freq`. */
    uint32_t play_rate = VOICE_RATE;
    (void)audio_mixer_track_info(track, &play_rate, NULL, NULL);
    const unsigned n = (unsigned)((uint64_t)play_rate * ms / 1000u);
    int16_t *buf = heap_caps_malloc(n * sizeof(int16_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "audio diag: no SPIRAM for tone buffer");
        audio_mixer_close_track(track);
        return;
    }
    for (unsigned i = 0; i < n; i++) {
        float v = sinf(2.0f * 3.14159265f * (float)freq * (float)i /
                       (float)play_rate);
        buf[i] = (int16_t)(v * 10000.0f);
    }
    /* Shared paced write (see mixer_write_paced); the stall limit makes the
     * diag tone bounded where the old inline loop could spin forever on a
     * wedged ring. */
    const uint8_t *src = (const uint8_t *)buf;
    const size_t total = n * sizeof(int16_t);
    if (mixer_write_paced(track, src, total, play_rate) != ESP_OK) {
        ESP_LOGW(TAG, "audio diag: tone playback stalled/short");
    }
    (void)audio_mixer_track_flush(track);
    audio_mixer_close_track(track);
    heap_caps_free(buf);
}

static int cmd_audio_diag(int argc, char **argv)
{
    (void)argc; (void)argv;
    esp_codec_dev_handle_t h = robodog_speaker_dac();
    if (h == NULL) {
        ESP_LOGE(TAG, "audio diag: no audio_dac codec handle");
        return 1;
    }
    (void)robodog_speaker_assert_output();
    voice_dump_dac_regs("before");
    (void)esp_codec_dev_set_out_mute(h, false);
    (void)esp_codec_dev_set_out_vol(h, 80);
    ESP_LOGI(TAG, "audio diag: forced unmute + out_vol 80");
    voice_dump_dac_regs("after-unmute");
    voice_play_tone(2000, 1000);
    voice_dump_dac_regs("after-tone");
    return 0;
}


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
 * CONFIG_ROBODOG_VOICE_MAX_SECS, bails early if no speech is detected at
 * all, and (F2) caps the continuous-speech phase at
 * CONFIG_ROBODOG_VOICE_SPEECH_MAX_SECS so a noise-saturated mic cannot burn
 * the whole window.  The SPIRAM capture buffer is allocated lazily on the
 * first speech frame (F1) and GROWS in small steps (capture_grow) so no huge
 * contiguous allocation is ever required; the caller frees it.  A no-speech
 * bail returns NULL with zero samples — no allocation, no free, nothing to
 * corrupt.  Returns the captured SAMPLES via *out_samples and a SPIRAM buffer
 * the caller owns (or NULL when no usable utterance was captured);
 * *out_fault reports a buffer allocation fault (vs a genuinely silent
 * listen) so the turn can end in "error" instead of a false "idle". */
/* The APP capture subscriber is opened ONCE and kept across turns (F3, stress
 * round R7): voice_capture no longer re-opens/closes it per listen.  Closing
 * per turn meant tearing down the subscriber's 5 KB ring + rate-conversion
 * buffers on every turn — and the ring free there is the tlsf panic site
 * ("block already marked as free", `assert failed: tlsf_free tlsf.c:630`,
 * rst:0xc) reached when the shared PSRAM heap metadata is already corrupted
 * under sustained voice-loop load.  Keeping the subscriber (ring + converters)
 * alive removes that per-turn free entirely; each new listen just FLUSHES the
 * ring for a clean VAD window (also discards post-TTS echo that previously
 * kept the energy-VAD "busy" — the F2 stuck-listen).  Trade-off: the APP
 * capture role stays claimed by the voice loop (Lua `audio` capture not
 * available) — correct for this standalone-agent board. */
static audio_capture_sub_handle_t s_cap_sub = NULL;

/* Utterance buffer growth step: the capture buffer grows on demand in fixed
 * steps of CONFIG_ROBODOG_VOICE_CAPTURE_STEP_SECS (2 s = 64 KB at 16 kHz s16)
 * instead of being allocated at its VOICE_MAX_SECS worst case (45 s =
 * 1.44 MB) on the first speech frame.  A single 1.44 MB *contiguous* SPIRAM
 * block is NOT reliably available once a voice turn has run — the heap is
 * fragmented by the turn's own buffers (TTS pipeline slots, ASR
 * response, LVGL) — so the old all-at-once allocation failed, `voice_capture`
 * returned zero samples, and the turn collapsed straight back to idle.  That
 * was the reported "hit listen, the board switches right back to idle": the
 * FIRST turn after a boot worked, every following one died on that malloc.
 * Growing in small steps keeps every allocation comfortably satisfiable while
 * still allowing the full VOICE_MAX_SECS window for a long utterance. */
#define CAPTURE_STEP_SAMPLES \
    ((size_t)VOICE_RATE * (size_t)CONFIG_ROBODOG_VOICE_CAPTURE_STEP_SECS)

/* Grow `buf` (SPIRAM) to hold at least `need` samples, in whole steps and
 * never past VOICE_MAX_SAMPLES.  On failure the existing buffer stays valid
 * and *cap is unchanged (the caller then ends the utterance with what it
 * already has rather than dropping the turn). */
static int16_t *capture_grow(int16_t *buf, size_t *cap, size_t need)
{
    if (need <= *cap) {
        return buf;
    }
    size_t want = *cap;
    while (want < need) {
        want += CAPTURE_STEP_SAMPLES;
    }
    if (want > VOICE_MAX_SAMPLES) {
        want = VOICE_MAX_SAMPLES;
    }
    if (want <= *cap) {
        return buf; /* already at the hard cap */
    }
    int16_t *grown = heap_caps_realloc(buf, want * sizeof(int16_t),
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (grown == NULL) {
        return NULL;
    }
    *cap = want;
    return grown;
}

/* Append `n` samples of one captured frame to the utterance buffer, growing
 * it in steps first.  Returns false when the buffer cannot grow (the caller
 * decides: end the utterance, or report a capture fault when nothing was
 * buffered yet). */
static bool capture_append(int16_t **buf, size_t *cap, size_t *got,
                           const int16_t *frame, size_t n)
{
    const size_t space = VOICE_MAX_SAMPLES - *got;
    if (n > space) {
        n = space; /* hard cap reached */
    }
    if (n == 0) {
        return true;
    }
    if (*got + n > *cap) {
        int16_t *grown = capture_grow(*buf, cap, *got + n);
        if (grown == NULL) {
            return false;
        }
        *buf = grown;
    }
    memcpy(*buf + *got, frame, n * sizeof(int16_t));
    *got += n;
    return true;
}

/* Capture one utterance.  `*out_fault` distinguishes a capture/allocation
 * fault from a genuinely silent listen: a fault must end the turn in the
 * visible "error" state, never as a clean idle (the panel must not report
 * "user said nothing" when the board could not even buffer the audio). */
static int16_t *voice_capture(size_t *out_samples, bool *out_fault)
{
    *out_samples = 0;
    *out_fault = false;
    if (s_cap_sub == NULL) {
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
        esp_err_t err = audio_capture_open_subscriber(cap, AUDIO_CAPTURE_SUB_APP,
                                                      &fmt, "robodog_voice",
                                                      &s_cap_sub);
        if (err != ESP_OK || s_cap_sub == NULL) {
            ESP_LOGE(TAG, "open APP capture subscriber failed: %s",
                     esp_err_to_name(err));
            s_cap_sub = NULL;
            return NULL;
        }
    }
    /* Fresh VAD window: drop whatever the ring holds from the previous turn
     * (post-TTS echo / stale frames) before this listen starts. */
    (void)audio_capture_sub_flush(s_cap_sub);
    ESP_LOGI(TAG, "listen started (16k/1/16, VAD energy=%d silence=%dms)",
             VAD_ENERGY, VAD_SILENCE_MS);
    /* One-line memory state per listen: the capture buffer grows on demand,
     * so knowing the free/largest SPIRAM block tells whether a long utterance
     * can still be buffered (and diagnoses an allocation fault immediately). */
    ESP_LOGI(TAG, "spiram free=%u largest=%u (capture grows in %d s steps)",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM),
             CONFIG_ROBODOG_VOICE_CAPTURE_STEP_SECS);
    robodog_ui_set_agent_state("listening");

    /* F1: the capture buffer is allocated lazily on the FIRST speech frame,
     * NOT for the whole listen phase.  A no-speech bail can therefore never
     * allocate-and-then-free a SPIRAM block — the path that tripped
     * `tlsf_free: block already marked as free` under sustained voice-loop
     * load and panicked the board (unplanned reboot, rst:0xc).  Pre-speech
     * silence is dropped; from the first speech frame everything (trailing
     * silence included) is buffered.  The buffer grows in small steps (see
     * capture_grow) so no single huge contiguous allocation is ever needed. */
    int16_t *buf = NULL;
    size_t cap_samples = 0; /* samples the buffer holds (grown in steps) */
    const int64_t t0 = esp_timer_get_time();
    const int64_t no_speech_us = (int64_t)CONFIG_ROBODOG_VOICE_NO_SPEECH_TIMEOUT_MS * 1000LL;
    const int64_t speech_cap_us = (int64_t)CONFIG_ROBODOG_VOICE_SPEECH_MAX_SECS * 1000000LL;
    int64_t speech_t0 = -1; /* uptime of the first detected speech frame */
    int16_t frame[VOICE_FRAME_SAMPLES];
    size_t fi = 0;      /* samples buffered in frame */
    size_t got = 0;     /* total samples committed to buf */
    bool ever_speech = false;
    uint32_t silence_ms = 0;
    uint32_t speech_ms = 0;    /* accumulated speech, for the blip gate */
    size_t streamed_bytes = 0; /* any audio at all from the capture hub */

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
        /* F2: a saturated/echoing mic can keep this energy-VAD "speaking"
         * indefinitely; cap the continuous-speech phase so a noise listen
         * cannot burn the whole window (and transcribe half a minute of
         * noise).  The utterance still goes to ASR; this only bounds it. */
        if (ever_speech && now - speech_t0 >= speech_cap_us) {
            ESP_LOGW(TAG, "continuous speech > %d s, capping the utterance",
                     CONFIG_ROBODOG_VOICE_SPEECH_MAX_SECS);
            break;
        }
        size_t want = (size_t)(VOICE_FRAME_SAMPLES - fi);
        size_t n = audio_capture_sub_read(s_cap_sub, (uint8_t *)(frame + fi),
                                          want * (VOICE_BITS / 8), 150);
        if (n > 0) {
            streamed_bytes += n;
            fi += n / (VOICE_BITS / 8);
        }
        if (fi == VOICE_FRAME_SAMPLES) {
            /* A whole frame is ready: classify it and buffer it. */
            const bool speech = frame_is_speech(frame, VOICE_FRAME_SAMPLES);
            if (speech) {
                if (!ever_speech) {
                    speech_t0 = now;
                }
                if (!capture_append(&buf, &cap_samples, &got, frame,
                                    VOICE_FRAME_SAMPLES)) {
                    /* Growth failed on the FIRST buffered audio: nothing
                     * usable was captured -> a real fault, not a silent
                     * listen. */
                    ESP_LOGE(TAG, "no SPIRAM for the utterance buffer (free=%u "
                             "largest=%u); cannot capture this turn",
                             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
                    *out_fault = true;
                    break;
                }
                ever_speech = true;
                silence_ms = 0;
                speech_ms += VOICE_FRAME_MS;
            } else if (ever_speech) {
                /* Buffer trailing / inter-phrase audio too (word tails).  A
                 * growth failure here ends the utterance with everything
                 * already buffered — the user's speech start is kept and still
                 * goes to ASR instead of the whole turn collapsing to idle. */
                if (!capture_append(&buf, &cap_samples, &got, frame,
                                    VOICE_FRAME_SAMPLES)) {
                    ESP_LOGW(TAG, "utterance buffer growth failed at %u ms; "
                             "ending the utterance with %u samples",
                             (unsigned)(got * 1000 / VOICE_RATE), (unsigned)got);
                    break;
                }
                silence_ms += VOICE_FRAME_MS;
            }
            fi = 0;
        }
        /* ONE settle point for the trailing-silence verdict, reached whether
         * or not new audio arrived this iteration (a quiet ring must still be
         * able to end a real utterance, and a blip must be retired even when
         * the stream stalls). */
        if (ever_speech && silence_ms >= VAD_SILENCE_MS) {
            if (speech_ms >= VAD_MIN_SPEECH_MS) {
                break; /* trailing silence after real speech */
            }
            /* A blip, not an utterance: discard it and keep the window open
             * for the user's actual speech (the no-speech timeout still
             * bounds this).  Discarding means the buffered frames are
             * dropped, NOT freed — the buffer is reused as the utterance
             * grows. */
            ESP_LOGI(TAG, "ignoring a %u ms blip (< %d ms speech); "
                     "still listening", (unsigned)speech_ms,
                     VAD_MIN_SPEECH_MS);
            got = 0;
            speech_ms = 0;
            silence_ms = 0;
            ever_speech = false;
            speech_t0 = -1;
        }
    }

    /* A capture that delivered NOTHING at all is not "the user was silent":
     * the ADC/codec stream itself stalled (I2S RX overrun, a wedged codec, or
     * the hub's capture task stopped reading).  Reporting it as silence made
     * the turn end in a clean `idle` — the panel claimed "nothing heard" for
     * a broken capture path.  Surface it as a fault so the turn ends in
     * `error` and the console says why. */
    if (streamed_bytes == 0 && buf == NULL) {
        ESP_LOGE(TAG, "capture stream produced no audio in %d ms "
                 "(codec/I2S RX stalled?)", CONFIG_ROBODOG_VOICE_NO_SPEECH_TIMEOUT_MS);
        *out_fault = true;
    }
    /* The capture subscriber is deliberately NOT closed here (F3): keeping
     * the APP ring + converters alive across turns removes the per-turn ring
     * teardown free — the `tlsf_free` panic site under heap corruption. */
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

/* Write `len` bytes to an OPEN esp_http_client request body, looping over
 * partial writes.  Used to stream the ASR multipart body (see
 * robodog_voice_asr) instead of assembling it in one contiguous buffer. */
static esp_err_t http_write_all(esp_http_client_handle_t client,
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
    }
    return ESP_OK;
}

/* POST captured PCM to Whisper; returns heap text (caller frees) or NULL.
 * Non-static: the `rd_asr` probe reuses this exact path (DRY).
 *
 * The multipart body is STREAMED (open + write header / WAV header / PCM /
 * footer) rather than assembled into one `head_len + wav_len + pcm_bytes +
 * foot_len` SPIRAM buffer.  That contiguous copy was a second ~1.44 MB
 * allocation right behind the capture buffer, and after a turn it failed just
 * like the capture malloc did — turning every later listen into "no usable
 * utterance" (the "hit listen -> back to idle" regression).  Streaming keeps
 * the peak allocation at the capture buffer alone, and it never copies the
 * utterance. */
char *robodog_voice_asr(const int16_t *pcm, size_t samples,
                        robodog_asr_meta_t *meta)
{
    if (meta != NULL) {
        memset(meta, 0, sizeof(*meta));
    }
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

    robodog_http_rx_t rx;
    robodog_http_rx_init(&rx, CONFIG_ROBODOG_ASR_MAX_RESPONSE);
    char *text = NULL;
    if (rx.buf == NULL) {
        ESP_LOGE(TAG, "ASR: no memory for response buffer");
        return NULL;
    }
    esp_http_client_config_t cfg = {
        .url = CONFIG_ROBODOG_ASR_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 60000,
        .buffer_size = 512,
    };
    int status = 0;
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(TAG, "ASR: http client init failed");
        goto out;
    }
    char ctype[96];
    snprintf(ctype, sizeof(ctype), "multipart/form-data; boundary=%s", boundary);
    esp_http_client_set_header(client, "Content-Type", ctype);

    esp_err_t err = esp_http_client_open(client, (int)body_len);
    if (err == ESP_OK) {
        err = http_write_all(client, header, head_len);
    }
    if (err == ESP_OK) {
        err = http_write_all(client, wav_hdr, wav_len);
    }
    if (err == ESP_OK) {
        err = http_write_all(client, pcm, pcm_bytes);
    }
    if (err == ESP_OK) {
        err = http_write_all(client, footer, foot_len);
    }
    if (err == ESP_OK) {
        /* Send the request and consume the response headers; the body is then
         * read straight into the accumulator (no event handler needed). */
        (void)esp_http_client_fetch_headers(client);
        while (rx.len + 1 < rx.cap) {
            const int r = esp_http_client_read(client, rx.buf + rx.len,
                                               (int)(rx.cap - 1 - rx.len));
            if (r <= 0) {
                break;
            }
            rx.len += (size_t)r;
        }
        rx.buf[rx.len] = '\0';
    }
    status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err == ESP_OK && status >= 200 && status < 300 && rx.len > 0) {
        cJSON *root = cJSON_Parse(rx.buf);
        cJSON *t = root != NULL ? cJSON_GetObjectItemCaseSensitive(root, "text") : NULL;
        if (t != NULL && cJSON_IsString(t) && t->valuestring != NULL) {
            text = strdup(t->valuestring);
        }
        if (root != NULL) {
            /* The endpoint auto-detects the spoken language and reports it;
             * surfacing it is what lets the voice turn answer in the user's
             * language and the host E2E prove a Chinese utterance was heard
             * as Chinese (not forced through an English model). */
            cJSON *lang = cJSON_GetObjectItemCaseSensitive(root, "language");
            cJSON *prob = cJSON_GetObjectItemCaseSensitive(root, "probability");
            if (meta != NULL && cJSON_IsString(lang) && lang->valuestring != NULL) {
                strlcpy(meta->language, lang->valuestring,
                        sizeof(meta->language));
                if (cJSON_IsNumber(prob)) {
                    meta->probability = (float)prob->valuedouble;
                }
            }
            cJSON_Delete(root);
        }
        if (text == NULL) {
            ESP_LOGE(TAG, "ASR: no text in 2xx response body='%.160s'",
                     rx.buf != NULL ? rx.buf : "");
        }
    } else {
        ESP_LOGE(TAG, "ASR: round-trip failed: http=%d err=%s body='%.160s'",
                 status, esp_err_to_name(err), rx.buf != NULL ? rx.buf : "");
    }
out:
    ESP_LOGI(TAG, "ASR text=\"%s\"", text != NULL ? text : "(empty)");
    /* Deterministic console contract for the gates: always emit the language
     * line, `?` when the endpoint did not report one (older endpoint). */
    ESP_LOGI(TAG, "ASR language=%s prob=%.2f",
             (meta != NULL && meta->language[0] != '\0') ? meta->language : "?",
             meta != NULL ? meta->probability : 0.0f);
    robodog_http_rx_free(&rx);
    return text;
}

/* ── TTS playback: reply text -> WAV -> device 16 kHz -> ES8311 ──────────── */

/* WAV payload offset + declared sample rate come from the single shared
 * robodog_wav_pcm_offset() in robodog_http.c (used by the ASR probe too) —
 * no per-caller chunk walking.  The device must resample from the ACTUAL
 * WAV rate: the endpoint speaks 24 kHz today, but blindly assuming 24k when
 * it returns anything else (e.g. a 16 kHz WAV) down-converts to ~10.7k and
 * the reply plays slow with very low pitch — the reported "read out was very
 * slow / pitch very low" regression.  Fall back to Qwen3-TTS's default 24k only
 * when the header is missing/unparseable. */

/* Linear resample from the WAV's declared rate to the MIXER's sample rate.
 * A same-rate WAV is taken as-is (no-op copy) — the hermes-parity 24 kHz hub
 * makes the common Kaspi 24 kHz case a pure passthrough with zero software
 * processing; any other rate is interpolated at step out_rate/in_rate so
 * pitch and tempo stay exact. */
/* ── TTS playback: reply text -> WAV -> device 16 kHz -> ES8311 ──────────── */

/* The voice/language/instruct triple for a TTS request.  The per-language
 * voices are named explicitly (not left to the server's global pin) so an
 * English reply is never read by a Chinese-native timbre — the accent is a
 * property of the timbre and `instruct` cannot remove it.  An empty global
 * CONFIG_ROBODOG_TTS_VOICE means "per-language"; a non-empty one pins every
 * reply to that timbre. */
static const char *tts_voice_for(int lang)
{
    if (CONFIG_ROBODOG_TTS_VOICE[0] != '\0') {
        return CONFIG_ROBODOG_TTS_VOICE;
    }
    if (lang == TTS_LANG_ZH) {
        return CONFIG_ROBODOG_TTS_VOICE_ZH;
    }
    if (lang == TTS_LANG_EN) {
        return CONFIG_ROBODOG_TTS_VOICE_EN;
    }
    return ""; /* auto: let the endpoint pick the native speaker */
}

static const char *tts_language_for(int lang)
{
    if (lang == TTS_LANG_ZH) {
        return "chinese";
    }
    if (lang == TTS_LANG_EN) {
        return "english";
    }
    return "auto";
}

static const char *tts_instruct_for(int lang)
{
    if (lang == TTS_LANG_ZH) {
        return CONFIG_ROBODOG_TTS_INSTRUCT_ZH;
    }
    if (lang == TTS_LANG_EN) {
        return CONFIG_ROBODOG_TTS_INSTRUCT_EN;
    }
    return "";
}

/* POST `reply` to the Qwen3-TTS endpoint and buffer the returned WAV in `rx`
 * WITHOUT playing it.  The single copy of the TTS request (JSON-escape body,
 * the set_post_field-pointer-alive UAF fix, error mapping) shared by
 * voice_speak and the `rd_tts` probe.  `rx` MUST already be initialised and
 * stays owned by the caller on every return.
 *
 * `lang` is the turn's explicit language (TTS_LANG_AUTO for probes).  The
 * request always carries the fixed seed and the language's instruct, so every
 * chunk of a chunked reply is synthesized under the same voice/register
 * instead of the server re-inferring and re-sampling each sentence. */
static esp_err_t tts_fetch_lang(const char *reply, robodog_http_rx_t *rx,
                                int lang)
{
    if (rx == NULL || rx->buf == NULL) {
        ESP_LOGE(TAG, "TTS: no response buffer");
        return ESP_ERR_NO_MEM;
    }
    char *esc = robodog_json_escape(reply);
    char *ins = robodog_json_escape(tts_instruct_for(lang));
    if (esc == NULL || ins == NULL) {
        ESP_LOGE(TAG, "TTS: json_escape failed");
        free(esc);
        free(ins);
        return ESP_ERR_NO_MEM;
    }
    const char *voice = tts_voice_for(lang);
    const char *language = tts_language_for(lang);
    char *json = NULL;
    const int json_len = snprintf(
        NULL, 0,
        "{\"model\":\"%s\",\"input\":\"%s\",\"voice\":\"%s\","
        "\"language\":\"%s\",\"seed\":%d,\"instruct\":\"%s\"}",
        CONFIG_ROBODOG_TTS_MODEL, esc, voice, language,
        CONFIG_ROBODOG_TTS_SEED, ins);
    if (json_len > 0) {
        json = malloc((size_t)json_len + 1);
        if (json != NULL) {
            snprintf(
                json, (size_t)json_len + 1,
                "{\"model\":\"%s\",\"input\":\"%s\",\"voice\":\"%s\","
                "\"language\":\"%s\",\"seed\":%d,\"instruct\":\"%s\"}",
                CONFIG_ROBODOG_TTS_MODEL, esc, voice, language,
                CONFIG_ROBODOG_TTS_SEED, ins);
        }
    }
    free(esc);
    free(ins);
    if (json == NULL) {
        ESP_LOGE(TAG, "TTS: no memory for request JSON");
        return ESP_ERR_NO_MEM;
    }
    esp_http_client_config_t cfg = {
        .url = CONFIG_ROBODOG_TTS_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 60000,
        .event_handler = robodog_http_event_handler,
        .user_data = rx,
        /* A 512-byte RX buffer means one read per 512 bytes of a ~300 KB WAV
         * (600 round-trips through esp_http_client's state machine).  Measured
         * on this board that made the device's effective synthesis rate
         * (~1.0x real time) slower than the same request from a host (~0.88x),
         * which is what lets the playback cursor overtake the fetch cursor and
         * opens a silence at the seam.  4 KB cuts the read overhead and is
         * still a small internal-RAM buffer. */
        .buffer_size = 4096,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(TAG, "TTS: http client init failed");
        free(json);
        return ESP_ERR_NO_MEM;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    /* Keep `json` alive across perform(): set_post_field stores the pointer,
     * it does not copy.  Freeing now -> freed-memory body -> Qwen3-TTS 400. */
    esp_http_client_set_post_field(client, json, strlen(json));
    const esp_err_t err = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(json);
    if (err != ESP_OK || !(status >= 200 && status < 300)) {
        ESP_LOGE(TAG, "TTS: round-trip failed: http=%d err=%s", status,
                 esp_err_to_name(err));
        return (err != ESP_OK) ? err : ESP_FAIL;
    }
    return ESP_OK;
}

/* Public probe/legacy entry point: language auto (the endpoint infers it from
 * the text).  The voice turn uses tts_fetch_lang() with its explicit language
 * through voice_speak_fetch_sync(). */
esp_err_t robodog_voice_tts_fetch(const char *reply, robodog_http_rx_t *rx)
{
    return tts_fetch_lang(reply, rx, TTS_LANG_AUTO);
}

/* Split robodog_voice_speak into a FETCH phase and a PLAY phase so the turn
 * loop can overlap chunk i+1's fetch with chunk i's playback (one-step TTS
 * pipeline, voice_pipeline.[ch]).  The pieces the play loop uses below. */

/* SYNCHRONOUS fetch: reset the caller-owned rx slot IN PLACE (keeps buf/cap —
 * no per-chunk alloc/free), POST the chunk to Qwen3-TTS, and apply the SAME
 * round-trip + RIFF sanity mapping the old single shout used (http error ->
 * its esp_err, non-2xx / non-WAVE body -> ESP_FAIL), so a pipeline failure
 * can fall back with identical error semantics. */
static esp_err_t voice_speak_fetch_sync(const char *chunk, robodog_http_rx_t *rx)
{
    robodog_http_rx_reset(rx);
    const esp_err_t err = tts_fetch_lang(chunk, rx, s_tts_lang);
    if (err != ESP_OK) {
        return err; /* robodog_voice_tts_fetch already logged the round-trip */
    }
    uint32_t src_rate = 0;
    const size_t data_off = robodog_wav_pcm_offset((const uint8_t *)rx->buf,
                                                   rx->len, &src_rate);
    if (data_off == 0 || data_off == rx->len) {
        ESP_LOGE(TAG, "TTS: response is not a RIFF/WAVE blob (%u B)",
                 (unsigned)rx->len);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* Open the APP mixer track + read its actual sample rate (24 kHz on the
 * hermes-parity audio hub).  Called ONCE per turn: keeping the track open
 * across the whole chunk queue is what makes consecutive sentences join
 * without a seam — the old per-chunk close/flush drained the ring (up to
 * ~200 ms) and re-opened the codec at every sentence boundary, adding a
 * silence the speaker heard as a pause. */
static esp_err_t voice_track_open(audio_mixer_track_handle_t *out_track,
                                  uint32_t *out_rate)
{
    audio_mixer_handle_t mix = NULL;
    audio_mixer_track_handle_t track = NULL;
    if (audio_hub_get_mixer(&mix) != ESP_OK ||
        audio_mixer_open_track(mix, AUDIO_MIXER_TRACK_APP, "robodog_voice",
                               &track) != ESP_OK) {
        ESP_LOGE(TAG, "TTS: could not open APP mixer track");
        return ESP_ERR_INVALID_STATE;
    }
    uint32_t rate = VOICE_RATE;
    (void)audio_mixer_track_info(track, &rate, NULL, NULL);
    if (rate < 8000 || rate > 96000) {
        rate = VOICE_RATE; /* bogus mixer info; fall back */
    }
    *out_track = track;
    *out_rate = rate;
    return ESP_OK;
}

/* Play ONE already-buffered TTS WAV chunk from `rx` on the (already open)
 * mixer track: parse + trim boundary silence + resample (only when the WAV
 * rate differs) + the `TTS start`/`TTS stop` bracket + the R5 on-screen
 * render + the shared paced write.  NEVER inits/frees `rx`, NEVER opens,
 * flushes or closes the track — the caller owns the turn's track lifetime, so
 * chunk i+1's audio starts the moment chunk i's last frame was written.
 *
 * Both ends are trimmed to short budgets (TTS_LEAD_KEEP_MS / TTS_TAIL_KEEP_MS)
 * so the measured ~90 ms lead-in and ~130 ms tail padding the endpoint adds to
 * every chunk do not double up at the seam, while the sentence's onset and
 * final plosive stay intact. */
static esp_err_t voice_play_chunk(audio_mixer_track_handle_t track,
                                  uint32_t play_rate, const char *chunk,
                                  robodog_http_rx_t *rx)
{
    uint32_t src_rate = 0;
    const size_t data_off = robodog_wav_pcm_offset((const uint8_t *)rx->buf,
                                                   rx->len, &src_rate);
    if (data_off == 0 || data_off == rx->len) {
        ESP_LOGE(TAG, "TTS: response is not a RIFF/WAVE blob (%u B)",
                 (unsigned)rx->len);
        return ESP_FAIL;
    }
    const size_t in_samples = (rx->len - data_off) / sizeof(int16_t);
    if (src_rate < 8000 || src_rate > 96000) {
        src_rate = 24000; /* Qwen3-TTS default; header absent/garbage */
    }

    /* Chunk-boundary silence trim: Qwen3-TTS pads every WAV with a lead-in and
     * a tail.  Played verbatim, each sentence boundary doubles that silence
     * and the reply sounds like separate clips. */
    size_t trim_off = 0;
    size_t trim_n = in_samples;
    robodog_pcm_trim_silence((const int16_t *)(rx->buf + data_off), in_samples,
                             src_rate,
                             CONFIG_ROBODOG_VOICE_TTS_LEAD_KEEP_MS,
                             CONFIG_ROBODOG_VOICE_TTS_TAIL_KEEP_MS,
                             &trim_off, &trim_n);

    /* Qwen3-TTS's native 24 kHz WAV is a PURE passthrough on the 24 kHz hub —
     * zero copy, no software conversion.  A WAV at any other rate is
     * interpolated (rare). */
    const int16_t *pcm_src16;
    size_t out_samples;
    int16_t *out16 = NULL;
    if (src_rate == play_rate) {
        pcm_src16 = (const int16_t *)(rx->buf + data_off) + trim_off;
        out_samples = trim_n;
    } else {
        /* 64-bit multiply: in_samples * play_rate overflows uint32 for long
         * replies (~456 k * 24 k = 1.1e10 -> wraps to 27 k on 32-bit), which
         * capped the buffer at ~1.7 s of a 13 s reply.  Always use 64 bits. */
        const size_t out_cap =
            (size_t)(((uint64_t)trim_n * play_rate) / src_rate + 4);
        out16 = heap_caps_malloc(out_cap * sizeof(int16_t),
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (out16 == NULL) {
            ESP_LOGE(TAG, "TTS: no SPIRAM for %u-sample resample buffer",
                     (unsigned)out_cap);
            return ESP_ERR_NO_MEM;
        }
        out_samples = robodog_pcm_resample(
            (const int16_t *)(rx->buf + data_off) + trim_off, trim_n, out16,
            out_cap, src_rate, play_rate);
        pcm_src16 = out16;
    }

    ESP_LOGI(TAG, "TTS start (%u samples @%lu Hz -> %u @%lu Hz %s, trim %u+%u)",
             (unsigned)in_samples, (unsigned long)src_rate,
             (unsigned)out_samples, (unsigned long)play_rate,
             src_rate == play_rate ? "(passthrough)" : "(resampled)",
             (unsigned)trim_off, (unsigned)(in_samples - trim_off - trim_n));
    /* Render THIS chunk's text the instant its audio begins: the TTS fetch
     * happened "off-screen" (in the fetch task), so the on-screen sentence is
     * exactly the one the speaker starts speaking (no queue-time lead). */
    robodog_ui_append_assistant(chunk);
    robodog_ui_set_agent_state("responding");
    /* Shared paced write (mixer_write_paced): feed the ring in ~20 ms frames
     * with a ~19 ms step so playback rides the codec rate in real time.  A
     * single audio_mixer_track_write() only waits AUDIO_MIXER_WRITE_TIMEOUT_MS
     * (500 ms) for its 8-frame ring to drain — writing the whole reply at once
     * saturates the ring and times out after ~0.5 s (the "responding but
     * silent" failure). */
    const size_t total = out_samples * sizeof(int16_t);
    const esp_err_t perr =
        mixer_write_paced(track, (const uint8_t *)pcm_src16, total, play_rate);
    ESP_LOGI(TAG, "TTS stop");
    heap_caps_free(out16);
    return perr;
}

esp_err_t robodog_voice_speak(const char *reply)
{
    /* Public single-buffer wrapper: fetch + play in one blocking call — used by
     * the `rd_tts`-adjacent external callers and the sequential degradation
     * path.  The voice turn does NOT go through this wrapper: it drives the
     * N-slot pipeline so consecutive sentences join seamlessly. */
    robodog_http_rx_t rx;
    robodog_http_rx_init(&rx, CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE);
    if (rx.buf == NULL) {
        ESP_LOGE(TAG, "TTS: no memory for response buffer");
        return ESP_ERR_NO_MEM;
    }
    const esp_err_t fetch = voice_speak_fetch_sync(reply, &rx);
    if (fetch != ESP_OK) {
        robodog_http_rx_free(&rx); /* nothing played */
        return fetch;
    }
    (void)robodog_speaker_assert_output();
    audio_mixer_track_handle_t track = NULL;
    uint32_t play_rate = VOICE_RATE;
    esp_err_t play = voice_track_open(&track, &play_rate);
    if (play == ESP_OK) {
        play = voice_play_chunk(track, play_rate, reply, &rx);
        (void)audio_mixer_track_flush(track); /* drain the last ring frames */
        audio_mixer_close_track(track);
    }
    robodog_http_rx_free(&rx);
    return play;
}

/* ── TTS fetch workers (background, CONCURRENT, ahead of playback) ─────── */
/* Each worker is the ONLY writer of the slot it was handed.  It fetches the
 * jobs the turn armed and reports each completion through a per-slot event bit
 * + result code; the turn applies commit()/fail() to its own pipeline model
 * when it consumes the chunk.  On teardown a worker acks its own EVT_PF_IDLE
 * bit once it is no longer touching any slot, so the turn can safely start the
 * next one (the slots are never freed: they are allocated once for the
 * firmware's lifetime).
 *
 * WHY CONCURRENT: the endpoint serializes synthesis, but each request still
 * costs the board a fixed ~0.5 s of HTTP setup + a 300 KB WAV download.  With
 * ONE worker that overhead lands BETWEEN requests — the server sits idle while
 * the board reads the previous WAV — and the measured device-side rate fell to
 * ~1.1x real time, i.e. slower than playback, which is exactly what opens the
 * seam gaps.  PF_WORKERS overlapping fetches hide the transfer under the
 * server's next synthesis. */
static void voice_fetch_task(void *arg)
{
    const int idx = (int)(intptr_t)arg;
    const EventBits_t idle_bit = EVT_PF_IDLE(idx);
    for (;;) {
        voice_pf_job_t job;
        if (xQueueReceive(s_pf_q, &job, pdMS_TO_TICKS(20)) != pdTRUE) {
            if (s_pf_abort) {
                xEventGroupSetBits(s_pf_evts, idle_bit);
            }
            continue;
        }
        if (s_pf_abort) {
            /* Turn ended while the job sat in the queue: drop it untouched. */
            xEventGroupSetBits(s_pf_evts, idle_bit);
            continue;
        }
        const int64_t t0 = esp_timer_get_time();
        s_slot_result[job.slot] = voice_speak_fetch_sync(job.chunk,
                                                         &s_tts_slot[job.slot]);
        const unsigned ms = (unsigned)((esp_timer_get_time() - t0) / 1000);
        if (s_slot_result[job.slot] == ESP_OK) {
            ESP_LOGI(TAG, "fetched chunk %u/%u in %u ms (%u B)",
                     (unsigned)job.index, (unsigned)job.total, ms,
                     (unsigned)s_tts_slot[job.slot].len);
        } else {
            ESP_LOGW(TAG, "fetch chunk %u/%u failed in %u ms (%s)",
                     (unsigned)job.index, (unsigned)job.total, ms,
                     esp_err_to_name(s_slot_result[job.slot]));
        }
        xEventGroupSetBits(s_pf_evts, EVT_PF_SLOT(job.slot));
    }
}

/* Allocate the pipeline's slot buffers ONCE (first voiced turn) and keep them
 * for the firmware's lifetime: no per-turn 2.5 MB alloc/free churn and no
 * free under the fetch task's writer (DESIGN.md R7 tlsf panic site).  A
 * partial allocation is accepted — the pipeline simply runs with fewer slots;
 * fewer than two usable slots means "no pipeline" and the turn degrades to the
 * sequential single-buffer path. */
static bool tts_slots_ensure(void)
{
    if (s_slot_count != 0) {
        return true;
    }
    if (!s_pf_ready) {
        return false;
    }
    const size_t cap = CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE;
    size_t n = 0;
    for (; n < TTS_SLOTS; n++) {
        robodog_http_rx_init(&s_tts_slot[n], cap);
        if (s_tts_slot[n].buf == NULL) {
            break;
        }
    }
    if (n < 2) {
        for (size_t i = 0; i < n; i++) {
            robodog_http_rx_free(&s_tts_slot[i]);
        }
        ESP_LOGW(TAG, "TTS pipeline unavailable: only %u slot(s) of %u x %u B "
                 "could be allocated (spiram free=%u largest=%u)",
                 (unsigned)n, (unsigned)TTS_SLOTS, (unsigned)cap,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
        return false;
    }
    s_slot_count = n;
    ESP_LOGI(TAG, "TTS pipeline ready: %u slots x %u B (~%u s buffered ahead)",
             (unsigned)n, (unsigned)cap,
             (unsigned)((n - 1) * cap / (24000 * sizeof(int16_t))));
    return true;
}

/* Arm background fetches for as many upcoming chunks as the pipeline allows
 * (strictly in order, bounded by the in-flight budget and free slots). */
static void voice_pf_arm(robodog_voice_pipe_t *pipe, char *arena,
                         const size_t *offs)
{
    size_t idx = 0;
    int slot;
    while ((slot = robodog_voice_pipe_claim(pipe, &idx)) >= 0) {
        voice_pf_job_t job = {
            .chunk = arena + offs[idx - 1],
            .index = idx,
            .total = pipe->total,
            .slot = slot,
        };
        xEventGroupClearBits(s_pf_evts, EVT_PF_SLOT(slot));
        s_slot_result[slot] = ESP_ERR_INVALID_STATE; /* "not finished" */
        if (xQueueSend(s_pf_q, &job, 0) != pdTRUE) {
            /* Queue full (should not happen: depth == slots): the chunk stays
             * un-buffered and is fetched synchronously at its play turn. */
            robodog_voice_pipe_fail(pipe, slot);
            break;
        }
    }
}

/* Make chunk `i` READY, waiting for its armed fetch if necessary.
 * Returns the slot holding it, or -1 on failure/wedge.  NEVER re-fetches a
 * chunk that is already in flight (the old one-step code did: it waited 3 s,
 * then fired a second request for the same text while the first was still
 * running — doubling the endpoint load and the gap). */
static int voice_pf_take(robodog_voice_pipe_t *pipe, size_t i)
{
    int slot = robodog_voice_pipe_ready_slot(pipe, i);
    if (slot >= 0) {
        return slot;
    }
    const int fs = robodog_voice_pipe_fetching_slot(pipe, i);
    if (fs < 0) {
        return -1; /* not buffered, not armed -> caller sync-fetches it */
    }
    const EventBits_t bits = xEventGroupWaitBits(
        s_pf_evts, EVT_PF_SLOT(fs), pdTRUE /* clear */, pdFALSE,
        pdMS_TO_TICKS(PF_WAIT_MS));
    if ((bits & EVT_PF_SLOT(fs)) == 0) {
        /* The worker is still inside esp_http_client after its own 60 s
         * timeout: the endpoint is wedged.  Do NOT touch the slot (the worker
         * may still write it) — fail the turn instead of corrupting it. */
        ESP_LOGE(TAG, "TTS: chunk %u/%u fetch wedged (> %d ms)",
                 (unsigned)i, (unsigned)pipe->total, PF_WAIT_MS);
        return -2;
    }
    if (s_slot_result[fs] == ESP_OK) {
        robodog_voice_pipe_commit(pipe, fs);
    } else {
        robodog_voice_pipe_fail(pipe, fs);
    }
    return robodog_voice_pipe_ready_slot(pipe, i);
}

/* ── One listen->ASR->agent->TTS turn ───────────────────────────────────── */

/* The reply-language directive is the ONE seam this firmware controls (the
 * shared base prompt is upstream-owned), so it carries both the spoken-prose
 * constraint AND the language rule.  The ASR endpoint auto-detects the spoken
 * language, so a Chinese utterance must be ANSWERED in Chinese — and
 * Traditional (繁體中文), which is what this household reads and what the RLCD
 * font is built for; Whisper may transcribe in Simplified, so the rule has to
 * be explicit or the model mirrors the Simplified transcript. */
/* Both directives state the language EXPLICITLY.  The session memory carries
 * the previous turns, so a Chinese conversation would otherwise make the model
 * answer a later English question in Chinese (measured: the English gate got a
 * Chinese reply after a Chinese turn); "reply in the user's language" is not
 * enough, the language has to be named. */
static const char *const VOICE_STYLE_EN =
    "[This is a spoken voice conversation. Reply in ENGLISH, in plain prose "
    "with no markdown formatting: no asterisks, no # headings, no bullet "
    "lists, no code blocks.] ";
/* Mandarin ONLY.  The model mirrors the input's register: a Cantonese (or
 * Cantonese-flavoured) utterance made it answer in Cantonese (唔/係/嘅/咗/佢/
 * 嗰/咁/啲/冇/睇/嚟/攰/喇) about half the time, and the TTS then voiced those
 * characters — the board sounded like it switched dialect mid-conversation.
 * The vocabulary has to be named explicitly; "繁體中文" alone does not exclude
 * 粵語, and "reply in the user's language" actively invites it. */
static const char *const VOICE_STYLE_ZH =
    "[這是一段語音對話。請一律用標準華語（台灣國語）的繁體中文口語回答，"
    "絕對不要使用粵語／廣東話的詞彙或語法（例如唔、係、嘅、咗、佢、嗰、咁、"
    "啲、冇、睇、嚟、攰、喇、喺），即使使用者用粵語說話也一樣；"
    "只使用繁體字，絕對不要出現任何簡體字。不要使用任何 markdown 格式："
    "不要星號、不要 # 標題、不要項目符號、不要程式碼區塊。] ";

/* Chinese turn?  The endpoint's detected language tag is authoritative; fall
 * back to the transcript's script when an older endpoint omits it.
 *
 * Cantonese matters here: faster-whisper can tag it "yue", and a Cantonese
 * utterance is still Chinese — answering it in English (the non-Chinese
 * branch) was a real failure mode. "cmn" is Mandarin under the same ISO-639-3
 * scheme. The reply directive then pins Mandarin, never 粵語. */
static bool voice_turn_is_chinese(const robodog_asr_meta_t *meta,
                                  const char *text)
{
    if (meta->language[0] != '\0') {
        return strncmp(meta->language, "zh", 2) == 0
            || strncmp(meta->language, "yue", 3) == 0
            || strncmp(meta->language, "cmn", 3) == 0;
    }
    return robodog_utf8_has_cjk(text);
}

static void voice_turn(void)
{
    if (s_turn_active) {
        ESP_LOGW(TAG, "a voice turn is already running; ignoring");
        return;
    }
    s_turn_active = true;
    /* Probes/legacy callers see auto; the turn overrides this once the ASR
     * language is known.  Resetting here keeps a failed early turn from
     * leaving the previous turn's language in effect. */
    s_tts_lang = TTS_LANG_AUTO;

    wifi_manager_status_t ws;
    wifi_manager_get_status(&ws);
    if (!ws.sta_connected) {
        ESP_LOGE(TAG, "WiFi not connected yet; aborting voice turn");
        robodog_ui_set_agent_state("error");
        s_turn_active = false;
        return;
    }

    /* ── Phase 3: the omni realtime path ────────────────────────────────
     * One WebSocket session replaces the three serial HTTP hops below.  The
     * mic is streamed into the session AS IT IS CAPTURED (no utterance
     * upload), and the turn shape is chosen per turn:
     *   OMNI  -> the model answers and speaks the turn itself (fast);
     *   AGENT -> transcript only, then claw_core answers with its
     *            skills/MCP/web tools and Qwen3-TTS speaks (full capability).
     * AUTO resolves through robodog_rt_route(); the routing decision is
     * logged so the console shows WHY a turn took a given path.
     * A realtime fault falls through to the Phase 2 chain (never a lost
     * turn), and `rd_voice_mode` can pin either path for A/B measurement. */
    if (s_voice_path != VOICE_PATH_CLASSIC) {
        robodog_rt_turn_t rt;
        const robodog_rt_mode_t forced =
            (s_voice_path == VOICE_PATH_OMNI) ? ROBODOG_RT_MODE_OMNI : ROBODOG_RT_MODE_AUTO;
        const esp_err_t rterr = robodog_rt_turn(forced, &rt);
        if (rterr == ESP_OK && !rt.fault &&
            (rt.transcript[0] != '\0' || rt.spoke)) {
            ESP_LOGI(TAG, "RT turn: mode=%s transcript=\"%s\" spoke=%d",
                     forced == ROBODOG_RT_MODE_OMNI ? "omni" : "auto",
                     rt.transcript, (int)rt.spoke);
            if (rt.spoke) {
                /* OMNI answered and spoke: the panel already carries the
                 * streamed transcript + reply (rendered by the RT module). */
                robodog_ui_set_agent_state("idle");
                s_turn_active = false;
                return;
            }
            /* AGENT shape: the transcript came from the model, so the
             * capability router now decides whether claw_core takes over.
             * A non-capability turn is answered by a second, audio-shaped
             * omni response over the SAME committed utterance. */
            const char *reason = NULL;
            const robodog_rt_mode_t route = robodog_rt_route(rt.transcript, &reason);
            ESP_LOGI(TAG, "RT route=%s reason=%s",
                     route == ROBODOG_RT_MODE_AGENT ? "agent" : "omni", reason);
            if (route == ROBODOG_RT_MODE_OMNI) {
                robodog_rt_turn_t rt2;
                if (robodog_rt_turn(ROBODOG_RT_MODE_OMNI, &rt2) == ESP_OK &&
                    !rt2.fault) {
                    robodog_ui_set_agent_state("idle");
                    s_turn_active = false;
                    return;
                }
            }
            /* Capability turn (or the omni answer failed): hand the model's
             * transcript to the Phase 2 chain, which owns the claw_core run
             * and the Qwen3-TTS read-out.  Skip its capture + ASR legs — the
             * utterance is already transcribed. */
            s_rt_transcript = strdup(rt.transcript);
        } else if (rterr != ESP_OK || rt.fault) {
            ESP_LOGW(TAG, "RT path failed (%s); falling back to the Phase 2 "
                     "chain", esp_err_to_name(rterr));
        } else {
            /* No speech captured: a clean idle, exactly like Phase 2. */
            ESP_LOGW(TAG, "RT captured no utterance");
            robodog_ui_set_agent_state("idle");
            s_turn_active = false;
            return;
        }
    }

    /* When the realtime session already transcribed the utterance, the
     * Phase 2 capture + Whisper legs must not run: capturing again would open
     * a second listen window (and end the turn idle with no speech). */
    size_t samples = 0;
    bool cap_fault = false;
    int16_t *pcm = NULL;
    if (s_rt_transcript == NULL) {
        pcm = voice_capture(&samples, &cap_fault);
    }
    if (s_rt_transcript == NULL && (pcm == NULL || samples < VOICE_FRAME_SAMPLES * 2)) {
        if (pcm != NULL) {
            heap_caps_free(pcm);
        }
        if (cap_fault) {
            /* The board could not buffer the audio at all (SPIRAM fault):
             * a real fault, never a clean "you said nothing" idle. */
            ESP_LOGE(TAG, "capture fault; ending turn in error");
            robodog_ui_set_agent_state("error");
        } else {
            ESP_LOGW(TAG, "no usable utterance captured (%u samples)",
                     (unsigned)samples);
            robodog_ui_set_agent_state("idle");
        }
        s_turn_active = false;
        return;
    }

    robodog_asr_meta_t meta;
    char *text;
    if (s_rt_transcript != NULL) {
        /* The omni session already transcribed this utterance. */
        memset(&meta, 0, sizeof(meta));
        text = s_rt_transcript;
        s_rt_transcript = NULL;
        ESP_LOGI(TAG, "ASR text=\"%s\" (from the omni session)", text);
        robodog_ui_show_user(text);
    } else {
        text = robodog_voice_asr(pcm, samples, &meta);
    }
    if (pcm != NULL) {
        heap_caps_free(pcm);
    }
    if (text == NULL) {
        /* ASR round-trip failed (or OOM): a real fault, not "user silent" —
         * surface it instead of collapsing to the success idle state. */
        ESP_LOGE(TAG, "ASR failed; ending turn");
        robodog_ui_set_agent_state("error");
        s_turn_active = false;
        return;
    }
    if (text[0] == '\0') {
        ESP_LOGW(TAG, "ASR returned no text; ending turn");
        free(text);
        robodog_ui_set_agent_state("idle");
        s_turn_active = false;
        return;
    }

    /* Render the user transcript (same role/timing as a user message
     * finalizing), then mark the agent as reasoning while the LLM is in
     * flight; the reply is appended when it finalizes (stale-caption safe). */
    robodog_ui_show_user(text);

    /* Auto-title the conversation session from the FIRST question (friendly
     * name for the top-middle RLCD label); a no-op once a title exists. */
    robodog_session_note_question(text);

    uint32_t request_id = 0;
    /* Voice-scoped directive at the ONE seam we control.  The shared base
     * prompt is upstream-owned and IM-oriented (it nudges the model toward
     * structured, scannable markdown), so rather than rewrite that global
     * prompt (claw_agent_mgr_update_core_config would require re-embedding the
     * whole upstream string — the fragile kind of hardcode we avoid), ask for
     * plain prose THIS turn: fewer `**`/bullets generated at the source =
     * cleaner, shorter TTS audio even before the robodog_voice_md_strip()
     * guarantee below.  The language rule rides the same directive: a Chinese
     * utterance gets a Traditional-Chinese answer (the RLCD font + household
     * convention), everything else English.  Applied AFTER the transcript
     * render so it never appears on the RLCD. */
    const bool chinese = voice_turn_is_chinese(&meta, text);
    const char *const directive = chinese ? VOICE_STYLE_ZH : VOICE_STYLE_EN;
    s_tts_lang = chinese ? TTS_LANG_ZH : TTS_LANG_EN;
    ESP_LOGI(TAG, "voice reply language=%s (asr=%s)",
             chinese ? "zh-TW" : "en",
             meta.language[0] ? meta.language : "?");
    /* Console evidence of the exact TTS request policy this turn applies —
     * the host gates assert the per-language timbre + fixed seed. */
    ESP_LOGI(TAG, "TTS request language=%s voice=%s seed=%d",
             tts_language_for(s_tts_lang), tts_voice_for(s_tts_lang),
             CONFIG_ROBODOG_TTS_SEED);
    const size_t vlen = strlen(directive);
    const size_t tlen = strlen(text);
    char *agent_input = malloc(vlen + tlen + 1);
    if (agent_input == NULL) {
        ESP_LOGE(TAG, "no memory for voice-style agent input");
        free(text);
        robodog_ui_set_agent_state("error");
        s_turn_active = false;
        return;
    }
    memcpy(agent_input, directive, vlen);
    memcpy(agent_input + vlen, text, tlen + 1);
    free(text);
    esp_err_t err = claw_agent_mgr_start_root_run_text(
        agent_input,
        robodog_session_id()[0] ? robodog_session_id() : NULL,
        0, CONFIG_ROBODOG_LLM_TIMEOUT_MS, &request_id);
    free(agent_input);
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
        /* Line-safe reply log: a raw "%s" here truncates at the first
         * newline (the agent replies in multi-line markdown), so the host
         * voice-verify gate's `agent reply: (.+)` capture would only ever see
         * the reply's FIRST line and its FULL_READOUT anti-truncation target
         * would be the head, not the true tail (R1).  JSON-escape instead:
         * every newline/quote/control char becomes a backslash escape, the
         * log is one console line, and the gate recovers the full text. */
        char *reply_esc = robodog_json_escape(resp.text);
        ESP_LOGI(TAG, "agent reply: %s",
                 reply_esc != NULL ? reply_esc : resp.text);
        free(reply_esc);
        /* Markdown -> speech-friendly text BEFORE the TTS queue.  The agent
         * replies in multi-line markdown (headers `**`, bullets, inline-code
         * spans, tables); read raw, Qwen3-TTS voices the syntax literally
         * ("asterisk asterisk bold") and the text-only panel shows the same
         * glyph noise.  Strip it once here so BOTH the speaker and the RLCD
         * get clean prose from the same chunks (the full raw reply is still
         * in the `agent reply:` log above for the host gates). */
        const size_t reply_len = strlen(resp.text);
        char *speech = heap_caps_malloc(reply_len + 1,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        esp_err_t rc = ESP_OK;
        bool truncated = false;
        if (speech == NULL) {
            ESP_LOGE(TAG, "no memory for markdown-cleaned reply (%u B)",
                     (unsigned)(reply_len + 1));
            rc = ESP_ERR_NO_MEM;
            robodog_ui_set_agent_state("error");
        } else {
            (void)robodog_voice_md_strip(resp.text, speech, reply_len + 1);
            /* Speak the WHOLE reply, not a truncated script.  The esp-claw root
             * agent is verbose by default (a full "what can you do" dump is ~60 s
             * of speech), so the reply is split at sentence/line breaks into
             * sentence-aligned chunks (<= TTS_MAX_CHARS preferred, up to the
             * UI field when a sentence runs long) and each
             * chunk is rendered on the RLCD (progressively growing the full reply
             * via robodog_ui_append_assistant) then spoken, in order.  The
             * read-out voice finishes every word of the reply — the adversarial
             * "voice cut off" truncation is gone (unless the queue exceeds
             * CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS: that tail-drop is then
             * reported honestly, see `voice truncated` below — never a
             * false-complete idle). */
            const size_t max_chunks = CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS;
            const size_t cap = CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS;
            /* The FIRST chunk gets a tighter budget: its synthesis is the
             * whole time-to-first-audio, so a shorter opening sentence makes
             * the reply start speaking seconds sooner while the later chunks
             * stay large enough to carry natural prosody. */
            const size_t first_cap = CONFIG_ROBODOG_VOICE_TTS_FIRST_CHARS;
            /* Chunks are sentence-aligned: a chunk may exceed `cap` to reach
             * a sentence ender, bounded by OVERFLOW_CHARS so its WAV still
             * fits one pipeline slot (384 KB = ~8.2 s).  The arena is sized
             * from that ceiling, not from `cap`. */
            const size_t overflow = CONFIG_ROBODOG_VOICE_TTS_OVERFLOW_CHARS;
            const size_t chunk_ceiling = cap + overflow;
            const size_t arena_cap = max_chunks * (chunk_ceiling + 1) + 1;
            char *arena = heap_caps_malloc(arena_cap,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            size_t *offs = NULL;
            if (arena != NULL) {
                offs = heap_caps_malloc(max_chunks * sizeof(size_t),
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            }
            if (arena == NULL || offs == NULL) {
                ESP_LOGE(TAG, "no memory for %u-chunk TTS queue",
                         (unsigned)max_chunks);
                if (arena != NULL) {
                    free(arena);
                }
                if (offs != NULL) {
                    free(offs);
                }
                /* R4: keep the explicit error terminal — rc MUST be non-OK or the
                 * terminal-state line below would clobber this "error" with
                 * "idle" and the OOM turn would read as a clean success. */
                rc = ESP_ERR_NO_MEM;
                robodog_ui_set_agent_state("error");
            } else {
                const size_t n = robodog_voice_split_reply(
                    speech, cap, chunk_ceiling, first_cap, arena, arena_cap,
                    offs, max_chunks, &truncated);
                if (n == 0) {
                    /* Blank reply: nothing speakable; render it, stay idle. */
                    robodog_ui_show_assistant(speech);
                } else {
                    if (truncated) {
                        /* R3: the reply needed more than max_chunks chunks, so
                         * the tail was dropped — the read-out voice did NOT
                         * finish every word.  Log a distinct marker the host
                         * voice-verify gate FAILs on (it must never pass a
                         * half-read reply), and end the turn in a visibly
                         * "truncated" state below, never a false-complete idle. */
                        ESP_LOGW(TAG,
                                 "voice truncated: reply exceeds %u chunks; the "
                                 "tail was NOT read out",
                                 (unsigned)max_chunks);
                    }
                    robodog_ui_set_agent_state("responding");

                    /* ── N-slot TTS pipeline (seamless multi-sentence readout)
                     * Qwen3-TTS synthesizes at roughly real time and returns
                     * each WAV whole (no streaming: time-to-first-byte ==
                     * total), so a chunk cannot start before its synthesis
                     * finished.  Playing the queue strictly sequentially
                     * therefore leaves a multi-second silence at every
                     * sentence boundary whenever one fetch is slower than the
                     * previous chunk's playback — the audible "long delay
                     * between response segments".
                     *
                     * The fix is a real pipeline, not a one-step lookahead:
                     * CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD fetches are kept in
                     * flight by ONE background task (strictly in order) into
                     * CONFIG_ROBODOG_VOICE_TTS_SLOTS persistent buffers, so
                     * synthesis runs ahead of the speaker and consecutive
                     * sentences join without a gap.  The slots are allocated
                     * ONCE for the firmware's lifetime and reset in place —
                     * no per-turn/per-chunk alloc/free churn (DESIGN.md R7
                     * tlsf panic site) and no free under the fetch task's
                     * writer.
                     *
                     * The APP mixer track is opened ONCE for the turn and the
                     * chunks are written back-to-back into it: the old
                     * per-chunk flush+close drained the ring and re-opened the
                     * codec at every boundary, adding a silence of its own.
                     * Boundary silence in the WAV itself is trimmed
                     * (voice_pcm.c) so the sentences butt together.
                     *
                     * Degradation: no pipeline (fewer than two slots could be
                     * allocated) -> the sequential single-buffer path below;
                     * a failed/wedged fetch -> a synchronous fetch of EXACTLY
                     * that chunk (never skipped, never reordered, never
                     * double-fetched). */
                    if (!s_pf_ready || !tts_slots_ensure()) {
                        ESP_LOGW(TAG, "TTS: pipeline unavailable; sequential "
                                 "single-buffer path");
                        for (size_t i = 0; i < n && rc == ESP_OK; i++) {
                            const char *chunk = arena + offs[i];
                            ESP_LOGI(TAG, "voice chunk %u/%u: %.160s",
                                     (unsigned)(i + 1), (unsigned)n, chunk);
                            if (robodog_voice_speak(chunk) != ESP_OK) {
                                rc = ESP_ERR_INVALID_STATE;
                            }
                        }
                    } else {
                        const size_t slots = s_slot_count;
                        /* One slot is always reserved for the synchronous
                         * fallback fetch (K <= slots-2 keeps it free even
                         * while one slot is playing). */
                        size_t lookahead = CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD;
                        if (lookahead > s_pf_workers) {
                            lookahead = s_pf_workers; /* concurrency bound */
                        }
                        if (slots >= 3) {
                            if (lookahead > slots - 2) {
                                lookahead = slots - 2;
                            }
                        } else {
                            lookahead = 1;
                        }
                        const EventBits_t slot_mask =
                            (EventBits_t)((1u << slots) - 1u);
                        robodog_voice_pipe_t pipe;
                        robodog_voice_pipe_start(&pipe, n, slots, lookahead);
                        ESP_LOGI(TAG, "TTS pipeline: %u chunks, %u slots, %u "
                                 "fetch(es) ahead",
                                 (unsigned)n, (unsigned)slots,
                                 (unsigned)lookahead);

                        /* A fresh turn must not inherit a previous turn's
                         * completion bits. */
                        xEventGroupClearBits(s_pf_evts,
                                             slot_mask | EVT_PF_IDLE_ALL);
                        s_pf_abort = false;

                        audio_mixer_track_handle_t track = NULL;
                        uint32_t play_rate = VOICE_RATE;
                        esp_err_t terr = voice_track_open(&track, &play_rate);
                        if (terr != ESP_OK) {
                            rc = terr;
                            robodog_ui_set_agent_state("error");
                        } else {
                            (void)robodog_speaker_assert_output();
                            for (size_t i = 1; i <= n && rc == ESP_OK; i++) {
                                const char *chunk = arena + offs[i - 1];
                                /* Arm every fetch the pipeline allows BEFORE
                                 * blocking on this chunk's audio: the worker
                                 * then synthesizes the following sentences
                                 * while the speaker reads this one. */
                                voice_pf_arm(&pipe, arena, offs);
                                /* Host gate hook: one `voice chunk i/n` per
                                 * chunk, logged at PLAY time (the fetch task
                                 * never logs these). */
                                ESP_LOGI(TAG, "voice chunk %u/%u: %.160s",
                                         (unsigned)i, (unsigned)n, chunk);

                                const int slot = voice_pf_take(&pipe, i);
                                if (slot == -2) {
                                    /* Fetch wedged past the HTTP timeout: the
                                     * endpoint is dead, stop honestly. */
                                    rc = ESP_ERR_TIMEOUT;
                                    break;
                                }
                                int play_slot = slot;
                                if (play_slot < 0) {
                                    /* Not buffered and not armed (a failed
                                     * lookahead): fetch EXACTLY this chunk
                                     * synchronously into the reserved slot. */
                                    play_slot = robodog_voice_pipe_free_slot(
                                        &pipe);
                                    if (play_slot < 0) {
                                        ESP_LOGE(TAG, "TTS: no free slot for "
                                                 "the sync fallback of chunk "
                                                 "%u/%u", (unsigned)i,
                                                 (unsigned)n);
                                        rc = ESP_ERR_NO_MEM;
                                        break;
                                    }
                                    ESP_LOGW(TAG, "prefetch chunk %u/%u "
                                             "failed; sync fallback",
                                             (unsigned)i, (unsigned)n);
                                    const esp_err_t f = voice_speak_fetch_sync(
                                        chunk, &s_tts_slot[play_slot]);
                                    if (f != ESP_OK) {
                                        rc = ESP_ERR_INVALID_STATE;
                                        break;
                                    }
                                    robodog_voice_pipe_mark_ready(&pipe, i,
                                                                  play_slot);
                                } else {
                                    ESP_LOGI(TAG, "prefetch chunk %u/%u ready "
                                             "(%u B)", (unsigned)i,
                                             (unsigned)n,
                                             (unsigned)s_tts_slot[play_slot].len);
                                }

                                /* Deterministic console evidence that the
                                 * pipeline (not a synchronous fetch) is what
                                 * feeds the speaker: `buffered=yes` means this
                                 * chunk was already synthesized when its turn
                                 * came, `ahead=k` how many later chunks are
                                 * buffered behind it.  The host voice-verify
                                 * gate asserts every boundary is buffered. */
                                size_t ahead = 0;
                                for (size_t k = i + 1; k <= n; k++) {
                                    if (robodog_voice_pipe_ready_slot(&pipe,
                                                                      k) >= 0) {
                                        ahead++;
                                    }
                                }
                                ESP_LOGI(TAG, "pipeline chunk %u/%u buffered=%s "
                                         "ahead=%u", (unsigned)i, (unsigned)n,
                                         slot >= 0 ? "yes" : "no",
                                         (unsigned)ahead);

                                robodog_voice_pipe_begin_play(&pipe, i,
                                                              play_slot);
                                /* Re-arm now that this chunk owns its slot:
                                 * the worker keeps synthesizing ahead while
                                 * the speaker reads. */
                                voice_pf_arm(&pipe, arena, offs);
                                const esp_err_t perr = voice_play_chunk(
                                    track, play_rate, chunk,
                                    &s_tts_slot[play_slot]);
                                robodog_voice_pipe_end_play(&pipe, i,
                                                            play_slot);
                                if (perr != ESP_OK) {
                                    rc = ESP_ERR_INVALID_STATE;
                                    break;
                                }
                            }
                            /* Drain the last ring frames, then release the
                             * track (one open/close per turn, not per chunk). */
                            (void)audio_mixer_track_flush(track);
                            audio_mixer_close_track(track);
                        }

                        /* Teardown: stop the worker and wait until it is no
                         * longer touching a slot before the turn returns (the
                         * slots themselves persist, so nothing is freed under
                         * the writer).  A mid-flight HTTP request finishes on
                         * its own bounded timeout first. */
                        s_pf_abort = true;
                        xQueueReset(s_pf_q);
                        robodog_voice_pipe_abort(&pipe);
                        const EventBits_t idle = xEventGroupWaitBits(
                            s_pf_evts, EVT_PF_IDLE_ALL, pdTRUE, pdTRUE,
                            pdMS_TO_TICKS(PF_WAIT_MS));
                        if ((idle & EVT_PF_IDLE_ALL) != EVT_PF_IDLE_ALL) {
                            /* The worker is STILL inside a fetch after the
                             * HTTP timeout window: it may keep writing a slot,
                             * so the pipeline must never hand that slot out
                             * again.  Degrade permanently to the sequential
                             * path (which owns its own buffer) instead of
                             * risking a write under the reader. */
                            ESP_LOGE(TAG, "TTS fetch worker(s) did not go "
                                     "idle; disabling the pipeline for this "
                                     "boot");
                            s_pf_ready = false;
                        }
                        s_pf_abort = false;
                        xEventGroupClearBits(s_pf_evts,
                                             slot_mask | EVT_PF_IDLE_ALL);
                    }
                }
                free(offs);
                free(arena);
            }
            free(speech);
        }
        /* Terminal state: idle on a complete readout, error on a real fault
         * (OOM / TTS stall), and — R3 — a distinct "truncated" when the chunk
         * queue cap dropped the reply tail, so the panel never lies about
         * read-out completion. */
        const char *terminal = "idle";
        if (rc != ESP_OK) {
            terminal = "error";
        } else if (truncated) {
            terminal = "truncated";
        }
        robodog_ui_set_agent_state(terminal);
    } else {
        ESP_LOGE(TAG, "agent run failed (%s): %s", esp_err_to_name(err),
                 resp.error_message != NULL ? resp.error_message : "");
        robodog_ui_set_agent_state("error");
    }
    /* Release the response's owned strings (text / error_message / target
     * channel+chat): `claw_core_response_free` is the upstream contract (see
     * app_claw_cli.c) — skipping it leaked the reply text on every turn. */
    claw_core_response_free(&resp);
    s_turn_active = false;
}

/* ── Task + trigger ─────────────────────────────────────────────────────── */

/* BOOT button (GPIO0): physical push-to-talk.  A press/release pair sets the
 * same EVT_LISTEN bit the `rd_listen` command sets, so the one voice task
 * wakes for both.
 *
 * Debounce is applied to the PRESS edge only, and a release is honoured only
 * when a press is armed:
 *   - A quick tap used to be swallowed entirely: the release edge arrived
 *     inside BOOT_BUTTON_DEBOUNCE_US of the press edge and was dropped, so
 *     the button did nothing unless held > 300 ms.
 *   - A release with no armed press (contact bounce, or an edge while the
 *     button was already up) used to fall through to the duration branch with
 *     a STALE press timestamp — held >= 1.5 s by construction — and fire a
 *     session reset instead of a listen.  Now a stray release is ignored. */
#define BOOT_BUTTON_GPIO GPIO_NUM_0
#define BOOT_BUTTON_DEBOUNCE_US 300000UL    /* min spacing between PRESSES */
#define BOOT_BUTTON_BOUNCE_US 20000UL       /* contact bounce on release */
#define BOOT_BUTTON_LONG_PRESS_US 1500000UL /* hold >= 1.5 s -> clear session */

static volatile uint64_t s_press_start_us; /* armed on press, read on release */
static volatile uint64_t s_last_press_us;  /* last accepted press edge */
static volatile bool s_press_armed;

/* ANYEDGE ISR: a short tap (release < LONG_PRESS) starts a voice turn
 * (EVT_LISTEN, same as the `rd_listen` console command); a hold >= 1.5 s
 * clears/resets the conversation session (EVT_RESET, fresh id). */
static void IRAM_ATTR boot_button_isr(void *arg)
{
    (void)arg;
    const uint64_t now = esp_timer_get_time();
    if (gpio_get_level(BOOT_BUTTON_GPIO) == 0) {
        /* Press: ignore bounce (already armed) and rapid re-presses. */
        if (s_press_armed || now - s_last_press_us < BOOT_BUTTON_DEBOUNCE_US) {
            return;
        }
        s_last_press_us = now;
        s_press_start_us = now;
        s_press_armed = true;
        return;
    }
    /* Release: only meaningful for an armed press; a release inside the
     * contact-bounce window keeps the press armed (the real release follows). */
    if (!s_press_armed || s_voice_evts == NULL) {
        return;
    }
    const uint64_t held = now - s_press_start_us;
    if (held < BOOT_BUTTON_BOUNCE_US) {
        return;
    }
    s_press_armed = false;
    const bool long_press = held >= BOOT_BUTTON_LONG_PRESS_US;
    BaseType_t hpw = pdFALSE;
    xEventGroupSetBitsFromISR(s_voice_evts,
                              long_press ? EVT_RESET : EVT_LISTEN, &hpw);
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
        .intr_type = GPIO_INTR_ANYEDGE,
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
    ESP_LOGI(TAG, "BOOT button armed on GPIO%d (tap = talk, 1.5 s hold = "
                  "clear session)", BOOT_BUTTON_GPIO);
#else
    ESP_LOGI(TAG, "BOOT button disabled (CONFIG_ROBODOG_VOICE_BOOT_BUTTON=n)");
#endif
    return ESP_OK;
}

static void voice_task(void *arg)
{
    (void)arg;
    for (;;) {
        const EventBits_t bits = xEventGroupWaitBits(
            s_voice_evts, EVT_LISTEN | EVT_RESET, pdTRUE /* clear */, pdFALSE,
            portMAX_DELAY);
        if (bits & EVT_RESET) {
            /* Long-press: clear the conversation session (fresh id).  Does
             * not start a listen. */
            ESP_LOGI(TAG, "RD_SESSION_RESET -> new session");
            robodog_session_reset();
            continue;
        }
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

/* `rd_voice_mode [classic|auto|omni]` — which path a turn takes.  `auto` and
 * `omni` drive the Phase 3 realtime session; `classic` keeps the Phase 2
 * chain (capture -> Whisper -> claw_core -> Qwen3-TTS).  Runtime switchable so
 * the two paths can be measured back to back on the same board. */
static int cmd_rd_voice_mode(int argc, char **argv)
{
    if (argc >= 2) {
        if (strcmp(argv[1], "classic") == 0) {
            s_voice_path = VOICE_PATH_CLASSIC;
        } else if (strcmp(argv[1], "omni") == 0) {
            s_voice_path = VOICE_PATH_OMNI;
        } else if (strcmp(argv[1], "auto") == 0) {
            s_voice_path = VOICE_PATH_AUTO;
        } else {
            ESP_LOGW(TAG, "unknown mode '%s' (classic|auto|omni)", argv[1]);
            return 1;
        }
    }
    ESP_LOGI(TAG, "voice mode=%s",
             s_voice_path == VOICE_PATH_CLASSIC ? "classic"
             : s_voice_path == VOICE_PATH_OMNI ? "omni" : "auto");
    return 0;
}

static int cmd_rd_session(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "reset") == 0) {
        robodog_session_reset();
        ESP_LOGI(TAG, "rd_session reset -> %s", robodog_session_id());
        return 0;
    }
    ESP_LOGI(TAG, "session id=%s title=\"%s\"",
             robodog_session_id(), robodog_session_title());
    return 0;
}

esp_err_t robodog_voice_init(void)
{
    s_voice_evts = xEventGroupCreate();
    if (s_voice_evts == NULL) {
        return ESP_ERR_NO_MEM;
    }
    /* Persistent conversation session: load the NVS id+title (or mint a fresh
     * id) and show it top-middle on the RLCD.  Failure (NVS down) just means
     * a RAM-only id this boot. */
    (void)robodog_session_init();
    if (xTaskCreate(voice_task, "robodog_voice", VOICE_STACK, NULL, 5,
                    &s_voice_task) != pdPASS) {
        vEventGroupDelete(s_voice_evts);
        s_voice_evts = NULL;
        return ESP_ERR_NO_MEM;
    }
    /* One-step TTS lookahead prefetch task + its DEDICATED event group (the
     * turn loop ABORT-acks it before freeing buffers).  On failure the
     * double-buffer path is simply disabled: the turn then always runs the
     * synchronous single-buffer path (graceful, ordered, no regression). */
    /* TTS fetch worker + its dedicated event group and job queue.  The worker
     * (priority 3, below the voice task's 5) only fetches; the turn owns the
     * pipeline bookkeeping and the slot buffers.  On failure the pipeline is
     * simply unavailable and every turn runs the sequential path. */
    s_pf_ready = false;
    s_pf_evts = xEventGroupCreate();
    s_pf_q = xQueueCreate(PF_QUEUE_DEPTH, sizeof(voice_pf_job_t));
    if (s_pf_evts == NULL || s_pf_q == NULL) {
        ESP_LOGW(TAG, "TTS pipeline primitives failed; sequential path only");
        if (s_pf_evts != NULL) {
            vEventGroupDelete(s_pf_evts);
            s_pf_evts = NULL;
        }
        if (s_pf_q != NULL) {
            vQueueDelete(s_pf_q);
            s_pf_q = NULL;
        }
    } else {
        s_pf_workers = 0;
        for (size_t i = 0; i < PF_WORKERS; i++) {
            TaskHandle_t t = NULL;
            if (xTaskCreate(voice_fetch_task, "rd_voice_pf", PF_STACK,
                            (void *)(intptr_t)i, 3, &t) != pdPASS) {
                break;
            }
            s_pf_workers++;
        }
        if (s_pf_workers == 0) {
            ESP_LOGW(TAG, "TTS fetch workers failed; sequential path only");
            vEventGroupDelete(s_pf_evts);
            vQueueDelete(s_pf_q);
            s_pf_evts = NULL;
            s_pf_q = NULL;
        } else {
            s_pf_ready = true;
            ESP_LOGI(TAG, "TTS fetch workers ready (%u concurrent, %u-slot "
                     "pipeline)", (unsigned)s_pf_workers,
                     (unsigned)TTS_SLOTS);
        }
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
    const esp_console_cmd_t diag = {
        .command = "rd_audio_diag",
        .help = "dump ES8311 regs, unmute/vol, play 1kHz tone, re-dump",
        .func = &cmd_audio_diag,
    };
    if (esp_console_cmd_register(&diag) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_audio_diag' console command");
    }
    const esp_console_cmd_t mode = {
        .command = "rd_voice_mode",
        .help = "voice path: rd_voice_mode [classic|auto|omni]",
        .func = &cmd_rd_voice_mode,
    };
    if (esp_console_cmd_register(&mode) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_voice_mode' console command");
    }
    /* Phase 3: the omni realtime session (playback slots + WS client).  A
     * failure is not fatal — the turn then falls through to the Phase 2
     * chain, which is what the board shipped before. */
    if (robodog_rt_init() == ESP_OK) {
        (void)robodog_rt_register_commands();
    } else {
        ESP_LOGW(TAG, "omni realtime unavailable; Phase 2 chain only");
        s_voice_path = VOICE_PATH_CLASSIC;
    }
    const esp_console_cmd_t sess = {
        .command = "rd_session",
        .help = "show the conversation session; 'rd_session reset' clears it",
        .func = &cmd_rd_session,
    };
    if (esp_console_cmd_register(&sess) != ESP_OK) {
        ESP_LOGW(TAG, "failed to register 'rd_session' console command");
    }
    ESP_LOGI(TAG, "RoboDog Phase 2 voice loop ready: rd_listen");
    return ESP_OK;
}
