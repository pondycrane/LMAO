/*
 * RoboDog explicit ASR response parser (LMAO-owned, PURE C).
 *
 * The deployed Qwen3-Omni realtime lane never emits
 * `conversation.item.input_audio_transcription.completed`, so the realtime
 * module POSTs the captured utterance to `/v1/chat/completions` as
 * `input_audio` (same model, no new service) and reads the transcript from
 * `choices[0].message.content`.  That content is either
 *   - the plain transcript, or
 *   - a `{"language":"<code>","text":"<transcript>"}` object (the prompt asks
 *     for it so the console can carry an honest detected language), sometimes
 *     wrapped in a ```json fence.
 *
 * This parser owns that shape: it extracts the content string, unescapes the
 * JSON, strips the optional fence, then parses the inner object (or falls back
 * to the whole content as the transcript).  Pure C — no cJSON, no allocation —
 * so the exact same source is compiled by the firmware and by the Bazel host
 * unit test (the DRY rule for device/host-testable helpers).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Parse a chat/completions ASR body into transcript + language.
 *
 * @param[in]  body      NUL-terminated JSON response body
 * @param[out] lang      detected ISO-639-1 code (empty when the model did not
 *                       report one; the caller may script-detect)
 * @param[in]  lang_cap  capacity of `lang`
 * @param[out] text      the verbatim transcript (empty when none)
 * @param[in]  text_cap  capacity of `text`
 * @return true when a non-empty transcript was found.
 *
 * Truncation is always on a UTF-8 boundary for `text`; `lang` is ASCII.
 */
bool robodog_asr_parse(const char *body, char *lang, size_t lang_cap,
                       char *text, size_t text_cap);

#ifdef __cplusplus
}
#endif
