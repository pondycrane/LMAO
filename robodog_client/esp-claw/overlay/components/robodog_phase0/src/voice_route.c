/*
 * RoboDog voice-turn capability router — see voice_route.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_route.h"

#include <string.h>

/* Capability intents that MUST reach claw_core: web/search, the household's
 * calendar + sensors, the board's own control surfaces, and anything that
 * writes code or files (self-programming).  Matched case-insensitively as
 * substrings; Chinese terms cover the household's Mandarin turns.
 *
 * Keep entries specific enough that small talk does not trip them: "list "
 * carries its trailing space so "listen" is not a capability request, and
 * "call " likewise. */
static const char *const ROUTE_AGENT_KEYWORDS[] = {
    /* the assistant's own capabilities: only claw_core knows the skills /
     * MCP / tools it actually has, so a capability question must not be
     * answered from the omni model's generic memory. */
    "capabilit", "can you do", "what can you do",
    /* web / knowledge retrieval */
    "search", "google", "look up", "lookup", "web", "browse", "fetch",
    /* household data */
    "weather", "forecast", "calendar", "schedule", "remind", "timer", "alarm",
    /* files / code / the board itself */
    "write", "file", "save", "code", "program", "script", "build", "compile",
    "run ", "execute", "terminal", "shell", "install", "deploy", "flash",
    /* device control */
    "turn on", "turn off", "switch", "light", "led", "volume", "brightness",
    /* sensor / history queries */
    "sensor", "temperature", "humidity", "chart", "history", "reading",
    /* memory / notes */
    "remember", "memory", "note", "list ", "todo",
    /* messaging */
    "email", "message", "send", "call ",
    /* Mandarin equivalents (the household speaks Mandarin too) */
    "搜尋", "查", "天氣", "行事曆", "提醒", "寫", "程式", "開", "關", "燈",
    NULL,
};

size_t robodog_route_keyword_count(void)
{
    size_t n = 0;
    while (ROUTE_AGENT_KEYWORDS[n] != NULL) {
        n++;
    }
    return n;
}

const char *robodog_route_keyword(size_t i)
{
    const size_t n = robodog_route_keyword_count();
    return (i < n) ? ROUTE_AGENT_KEYWORDS[i] : NULL;
}

static char lower(char c)
{
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

/* Case-insensitive substring test (ASCII case folding; CJK bytes pass
 * through unchanged, which is what the Mandarin entries need). */
static bool contains_ci(const char *haystack, const char *needle)
{
    const size_t nlen = strlen(needle);
    if (nlen == 0) {
        return true;
    }
    for (const char *p = haystack; *p != '\0'; p++) {
        size_t j = 0;
        while (j < nlen && p[j] != '\0' && lower(p[j]) == lower(needle[j])) {
            j++;
        }
        if (j == nlen) {
            return true;
        }
    }
    return false;
}

robodog_route_t robodog_route_turn(const char *text, const char **out_reason)
{
    if (out_reason != NULL) {
        *out_reason = "default";
    }
    if (text == NULL || text[0] == '\0') {
        if (out_reason != NULL) {
            *out_reason = "empty";
        }
        return ROBODOG_ROUTE_OMNI;
    }
    for (size_t i = 0; ROUTE_AGENT_KEYWORDS[i] != NULL; i++) {
        if (contains_ci(text, ROUTE_AGENT_KEYWORDS[i])) {
            if (out_reason != NULL) {
                *out_reason = ROUTE_AGENT_KEYWORDS[i];
            }
            return ROBODOG_ROUTE_AGENT;
        }
    }
    return ROBODOG_ROUTE_OMNI;
}
