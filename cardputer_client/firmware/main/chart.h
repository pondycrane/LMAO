#pragma once

/* Sprout chart for the Cardputer's 240x135 ST7789 display: soil moisture,
 * air humidity and air temperature.
 *
 * The server appends one machine-readable line to its LXMF reply to every
 * allow-listed client (see lma_core/sprout_history.py):
 *
 *     DATA <node8> <dry> <wet> <ct> <t0> ... <tt> <ch> <h0> ... <hh> <cm> <m0> ... <mm>
 *
 * * ``node8`` — first 8 hex chars of the reporting node id (informational)
 * * ``dry``/``wet`` — the node's active plant-profile band, integer percent;
 *   -1 means the node has not reported a band yet
 * * three count-prefixed series — air temperature in °C, air humidity in %,
 *   then soil moisture in % — each OLDEST FIRST.  Lengths may differ.
 *
 * Ported from cardputer_client/chart.py (the MicroPython reference); the DATA
 * parser and the geometry are pure C++ and host-tested via Bazel (the same
 * code the device firmware compiles).  draw() talks to a ChartDisplay adapter
 * exposing four primitives — fill(rgb888), pixel(x,y,rgb888),
 * line(x0,y0,x1,y1,rgb888) and text(s,x,y,rgb888).  The ST7789 driver in
 * st7789.{h,cpp} implements it; a host test uses a fake. */

#include <cstdint>
#include <string>
#include <vector>

namespace cardputer::chart {

    // ChartData mirrors cardputer_client/chart.py's parse_data_line result.
    struct ChartData {
        std::string node;
        int  dry = 0;                 /* -1 = no band reported yet */
        int  wet = 0;
        std::vector<float> temp;      /* °C floats */
        std::vector<int>   humidity;  /* % */
        std::vector<int>   samples;   /* soil moisture % */
    };

    /* Display adapter abstracting the real ST7789 panel (device) from the
     * chart renderer (host-testable).  Colours are RGB888; the device converts
     * to the panel's RGB565. */
    class ChartDisplay {
    public:
        virtual ~ChartDisplay() = default;
        virtual void fill(uint32_t rgb888) = 0;
        virtual void pixel(int x, int y, uint32_t rgb888) = 0;
        virtual void line(int x0, int y0, int x1, int y1, uint32_t rgb888) = 0;
        virtual void text(const char* s, int x, int y, uint32_t rgb888) = 0;
    };

    struct DrawResult {
        int  points = 0;
        bool error  = false;
        std::string error_message;
    };

    /* Extract the first DATA record from *text*, or false.
     * Tolerates the ACK text sharing the message (the server sends its ACK
     * line first, then this one) and ignores malformed records. */
    bool parse_data_line(const std::string& text, ChartData& out);

    /* Percent range [lo, hi] to draw, always including the band. */
    void y_window(int dry, int wet, const std::vector<int>& soil,
                  const std::vector<int>& hum, int& lo, int& hi);
    /* Auto-scaled °C range [lo, hi] for the temperature trace. */
    void temp_window(const std::vector<float>& temps, float& lo, float& hi);
    /* Map a value to a screen row (y grows downward).  Value and range are
     * doubles so the °C temperature trace shares the mapper with the percent
     * series (mirrors chart.py's float-friendly y_px). */
    int  y_px(double value, double lo, double hi);
    /* Map a sample index to a screen column. */
    int  x_px(int index, int count);

    /* Render the chart.  Returns a diagnostics result; never throws for
     * drawing problems — on failure error is set so the caller can fall back
     * to the text screen. */
    DrawResult draw(ChartDisplay& tft, const ChartData& data);

    // Screen geometry / colours — kept in sync with cardputer_client/chart.py.
    inline constexpr int W = 240;
    inline constexpr int H = 135;
    inline constexpr int PLOT_L = 28, PLOT_R = 210, PLOT_T = 18, PLOT_B = 116;
    inline constexpr int TEMP_AXIS_X = PLOT_R + 6;

    inline constexpr uint32_t WHITE = 0xFFFFFF;
    inline constexpr uint32_t GREY = 0x808080;
    inline constexpr uint32_t DRY_COLOR = 0xFF0000;   /* red    */
    inline constexpr uint32_t WET_COLOR = 0x00FFFF;   /* cyan   */
    inline constexpr uint32_t SOIL_COLOR = 0xFFFFFF;  /* white  */
    inline constexpr uint32_t HUM_COLOR = 0x00FF00;   /* green  */
    inline constexpr uint32_t TEMP_COLOR = 0xFFA500;  /* orange */

}
