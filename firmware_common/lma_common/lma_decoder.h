#pragma once

#include <string>

// Minimal LMAOEnvelope -> AudioMessage decoder, mirroring lma_encoder.h.
// Walks the protobuf wire format (varints + length-delimited fields) to pull
// the oneof payload field 21 (AudioMessage) and its audio_data (field 2).
namespace lma_decoder {

    // Walk `envelope`; if it carries LMAOEnvelope.audio (field 21), return the
    // AudioMessage.audio_data (field 2) bytes + set `codec_out` ("codec2",
    // ...) from field 3 when present.  Returns empty when there is no audio
    // payload (text/sensor envelopes parse fine and yield empty).
    std::string extract_envelope_audio(const std::string& envelope,
                                       std::string* codec_out = nullptr);

}
