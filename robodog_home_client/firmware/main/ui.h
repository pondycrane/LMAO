#pragma once

#include "lma_chart.h"

// Panel rendering for the RoboDog home client: device name + latest air
// reading + the temperature/humidity history chart.
//
// Also owns the RD_SHOT capture dump (the same protocol as robodog_client):
// after every render the exact u8g2 frame buffer is streamed over USB-JTAG, so
// `robodog_client/capture.py` can reconstruct what the panel shows.
namespace ui {

    bool init(void);

    // Draw the panel.  `bundle` may be null (no reply yet); `stale` marks a
    // failed sync (the chart then shows the last good series), `status` is a
    // short line for the header right side (e.g. "wifi", "no reply").
    void render(const char* device_name, const lma_chart::ChartBundle* bundle,
                bool have_data, bool stale, const char* status);

}
