/*
 * RoboDog reply sanitiser: strip MiniCPM-o reasoning from a reply
 * (LMAO-owned, PURE C).
 *
 * The omni model emits ` thinking ...  response` blocks.  Two things make them
 * unacceptable on this board:
 *
 *   1. The Talker VOICES them — measured by ASR-ing a turn's audio: the
 *      transcript read "Gate, rot, zoo, rot, okay, 21, 22, ..." i.e. the model
 *      pronouncing the tag before the answer.
 *   2. They leak into the RLCD subtitle and the `agent reply:` log.
 *
 * There is NO server-side switch that removes them: the OpenAI-compatible
 * realtime handler accepts `chat_template_kwargs.enable_thinking=false` and
 * ignores it (measured: identical audio and text with and without it, and the
 * block still appears on a repeated question in one session).  So the device
 * strips it — and because the Talker has already voiced the block, the turn's
 * server audio is discarded and the CLEAN text is re-spoken.
 *
 * The scan runs over the WHOLE accumulated text rather than per streaming
 * delta: the deltas split the tag across events (`<`, `think`, `>` arrive
 * separately), so a per-delta match misses it — measured on hardware.
 *
 * Pure C (no ESP-IDF, no allocation) so the same source is compiled by the
 * firmware and by the Bazel host unit test.
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
 * @brief Copy `raw` to `out`, dropping every ` thinking ... </think>` block
 *        (and the closing tag), then trimming leading blank lines.
 *
 * A block that is still open at the end of `raw` is dropped to the end of the
 * text, which is what the caller wants mid-stream: nothing inside a reasoning
 * block is ever shown or spoken.  A malformed tag (no closing tag) simply
 * drops the tail.
 *
 * @param[in]  raw  NUL-terminated accumulated reply text
 * @param[out] out  destination (may alias `raw` only if `out == raw` is not
 *                  required; pass a separate buffer)
 * @param[in]  cap  capacity of `out` in bytes
 * @return length of the clean text written to `out` (always NUL-terminated
 *         when cap > 0)
 */
size_t robodog_think_strip(const char *raw, char *out, size_t cap);

/**
 * @brief True when `raw` contains a reasoning block (open or closed).
 *        Used to decide whether a turn's server audio must be discarded.
 */
bool robodog_think_present(const char *raw);

#ifdef __cplusplus
}
#endif
