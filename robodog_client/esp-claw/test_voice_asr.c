/*
 * Host test for the explicit-ASR response parser (voice_asr.c).
 *
 * The deployed Qwen3-Omni lane does not emit the realtime transcription event,
 * so the device POSTs the utterance to /v1/chat/completions and reads the
 * transcript from `choices[0].message.content`.  A parser bug here is silent
 * and expensive: the console would carry `RT ASR text=""` and the turn would
 * route as if the user said nothing.  The exact bodies observed from the live
 * endpoint are pinned here (plain transcript, the requested JSON object, the
 * ```json fence the model adds half the time, and Whisper's `{"text":...}`),
 * against the same source the firmware compiles.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <string.h>

#include "voice_asr.h"

static int failures;

#define CHECK(cond, ...)                                                      \
    do {                                                                      \
        if (!(cond)) {                                                        \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);              \
            fprintf(stderr, __VA_ARGS__);                                     \
            fprintf(stderr, "\n");                                            \
            failures++;                                                       \
        }                                                                     \
    } while (0)

static void one(const char *body, const char *want_lang, const char *want_text,
                bool want_ok)
{
    char lang[16];
    char text[512];
    const bool ok = robodog_asr_parse(body, lang, sizeof(lang), text,
                                      sizeof(text));
    CHECK(ok == want_ok, "parse(%s) ok=%d want=%d", body, (int)ok,
          (int)want_ok);
    CHECK(strcmp(lang, want_lang) == 0, "parse(%s) lang=%s want=%s", body,
          lang, want_lang);
    CHECK(strcmp(text, want_text) == 0, "parse(%s) text=%s want=%s", body,
          text, want_text);
}

int main(void)
{
    /* Plain transcript (the prompt asks for JSON, but the model sometimes
     * answers with the bare sentence). */
    one("{\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
        "\"content\":\"What can you do?\"}}]}",
        "", "What can you do?", true);

    /* The requested object, JSON-escaped inside the content string. */
    one("{\"choices\":[{\"message\":{\"content\":"
        "\"{\\\"language\\\":\\\"en\\\",\\\"text\\\":\\\"What can you do?\\\"}\"}}]}",
        "en", "What can you do?", true);

    /* The ```json fence the model adds around the object. */
    one("{\"choices\":[{\"message\":{\"content\":"
        "\"```json\\n{\\\"language\\\":\\\"zh\\\",\\\"text\\\":"
        "\\\"\\u4f60\\u597d\\uff0c\\u4e16\\u754c\\u4eca\\u5929\\u5f88\\u597d\\u3002\\\"}\\n```\"}}]}",
        "zh", "你好，世界今天很好。", true);

    /* Whisper / transcription-endpoint shape (no choices wrapper). */
    one("{\"text\":\"Count slowly from twenty to thirty.\"}",
        "", "Count slowly from twenty to thirty.", true);

    /* Escaped quotes/newlines inside the transcript survive the unescape. */
    one("{\"choices\":[{\"message\":{\"content\":"
        "\"{\\\"language\\\":\\\"en\\\",\\\"text\\\":\\\"say \\\\\\\"hi\\\\\\\""
        "\\\\nthen wait\\\"}\"}}]}",
        "en", "say \"hi\"\nthen wait", true);

    /* No transcript: an empty content is not a usable ASR result. */
    one("{\"choices\":[{\"message\":{\"content\":\"\"}}]}", "", "", false);
    one("{\"choices\":[]}", "", "", false);
    one("", "", "", false);
    one("not json at all", "", "", false);

    /* Truncation stays on a UTF-8 boundary. */
    {
        char lang[8];
        char text[5]; /* 4 bytes + NUL: room for exactly one CJK codepoint */
        const char *body =
            "{\"choices\":[{\"message\":{\"content\":\"\xe4\xbd\xa0\xe5\xa5\xbd"
            "\xe4\xb8\x96\xe7\x95\x8c\"}}]}";
        CHECK(robodog_asr_parse(body, lang, sizeof(lang), text, sizeof(text)),
              "truncation parse");
        CHECK(strcmp(text, "\xe4\xbd\xa0") == 0,
              "truncated on a codepoint boundary: %s", text);
    }

    if (failures != 0) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    printf("voice_asr: all checks passed\n");
    return 0;
}
