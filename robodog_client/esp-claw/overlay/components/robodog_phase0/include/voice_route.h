/*
 * RoboDog voice-turn capability router (LMAO-owned, PURE C).
 *
 * The Phase 3 voice path has two shapes, and the board picks one PER TURN:
 *
 *   OMNI  — the omni model answers the turn itself and speaks it.  Fastest
 *           (one streaming session, first audio after a fraction of a second
 *           of generation) but esp-claw's skills/MCP/web tools are NOT in
 *           that path.
 *   AGENT — the omni session transcribes only; claw_core answers with its
 *           skills, MCP servers, web search and self-programming, and
 *           Qwen3-TTS speaks.  Full capability, slower.
 *
 * Getting this wrong in the OMNI direction silently loses capability ("search
 * the web for X" answered from the model's memory instead of searching), so
 * the rule is a small EXPLICIT keyword table rather than an inferred
 * classifier: the decision is auditable from the console
 * (`RT route=agent reason=search`) and a household can tune it.  Anything not
 * matching a capability intent stays on the fast path.
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

typedef enum {
    ROBODOG_ROUTE_OMNI = 0,   /**< omni answers + speaks the turn */
    ROBODOG_ROUTE_AGENT = 1,  /**< claw_core answers (skills/MCP/web) */
} robodog_route_t;

/**
 * @brief Decide which shape a turn takes, from the transcript.
 *
 * @param[in]  text        transcript (may be NULL/empty)
 * @param[out] out_reason  optional; the matched keyword (static string), or
 *                         "default"/"empty" when nothing matched.  Never
 *                         allocated — the caller must not free it.
 * @return ROBODOG_ROUTE_AGENT when the transcript asks for a capability the
 *         omni model cannot perform, else ROBODOG_ROUTE_OMNI.
 */
robodog_route_t robodog_route_turn(const char *text, const char **out_reason);

/** @brief Number of capability keywords in the table (for tests/diagnostics). */
size_t robodog_route_keyword_count(void);

/** @brief The i-th capability keyword (static string), or NULL past the end. */
const char *robodog_route_keyword(size_t i);

#ifdef __cplusplus
}
#endif
