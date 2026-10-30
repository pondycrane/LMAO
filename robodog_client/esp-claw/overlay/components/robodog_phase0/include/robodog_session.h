/*
 * RoboDog Phase 0 overlay — persistent conversation session (LMAO-owned).
 *
 * Gives the RoboDog voice loop a persistent session identity so the
 * agent keeps context across turns (and across reboots), until the user
 * clears it with a long-press of the middle (BOOT) button.  The session id
 * is passed to claw_agent_mgr's root-run seam each turn; a NEW id (minted on
 * first boot or after a clear) starts a clean, context-free session.
 *
 * Also carries a friendly display NAME auto-titled from the conversation's
 * first question (the top-middle RLCD label shows it).  Both id and title are
 * persisted in NVS so they survive re-flash-free reboots; a clear erases them
 * and mints a fresh id.
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Idempotent: load the persisted session id+title, or mint a fresh id when
 * none is stored.  Safe to call once at voice-init. */
esp_err_t robodog_session_init(void);

/* Current session id ("" if not initialized).  Stable across reboots until a
 * clear.  Pass to the agent root-run seam to keep conversation context. */
const char *robodog_session_id(void);

/* Current friendly display name ("" until the first question titles it). */
const char *robodog_session_title(void);

/* Auto-title the session from the first question if it is still unnamed, then
 * refresh the top-middle RLCD label.  Pass the raw user transcript (NOT the
 * voice-style directive prefix). */
esp_err_t robodog_session_note_question(const char *question);

/* Clear the session: best-effort delete in the agent manager, erase the NVS
 * id+title, mint a fresh id, refresh the UI, log `rd_session: reset -> <id>`.
 * The fresh id is what guarantees the next turn starts a clean session even if
 * the upstream catalog delete is a no-op for our id. */
esp_err_t robodog_session_reset(void);

#ifdef __cplusplus
}
#endif
