// Host tests for cardputer chart (DATA-line parser + geometry + draw against a
// fake display) — the C++ twin of //tests:test_chart (cardputer_client/chart.py).
// Pure C++: no ESP-IDF.  Exercises the same code the device firmware compiles.
#include <array>
#include <cassert>
#include <cstdio>
#include <string>
#include <tuple>
#include <vector>

#include "chart.h"

using namespace cardputer::chart;

namespace {

    class FakeChartDisplay : public ChartDisplay {
    public:
        void fill(uint32_t c) override { fills++; pixels.clear(); lines.clear(); }
        void pixel(int x, int y, uint32_t c) override { pixels.push_back({x, y, c}); }
        void line(int x0, int y0, int x1, int y1, uint32_t c) override { lines.push_back({x0, y0, x1, y1, c}); }
        void text(const char* s, int x, int y, uint32_t c) override { texts.emplace_back(s, x, y, c); }

        int  fills = 0;
        std::vector<std::array<int,3>> pixels;
        std::vector<std::array<int,5>> lines;
        std::vector<std::tuple<std::string,int,int,uint32_t>> texts;

        bool has_color(uint32_t c) const {
            for (auto& p : pixels) if ((uint32_t)p[2] == c) return true;
            for (auto& l : lines)  if ((uint32_t)l[4] == c) return true;
            return false;
        }
        int rows_with(uint32_t c) const {
            for (auto& p : pixels) if ((uint32_t)p[2] == c) return p[1];
            for (auto& l : lines)  if ((uint32_t)l[4] == c && l[1] == l[3]) return l[1];
            return -1;
        }
        std::vector<std::string> texts_with(uint32_t c) const {
            std::vector<std::string> v;
            for (auto& t : texts) if (std::get<3>(t) == c) v.push_back(std::get<0>(t));
            return v;
        }
        std::string all_texts() const {
            std::string s;
            for (auto& t : texts) { s += std::get<0>(t); s += " | "; }
            return s;
        }
    };

    ChartData data(int dry, int wet, std::vector<int> samples,
                   std::vector<int> humidity = {},
                   std::vector<float> temp = {}, std::string node = "e824ad2d") {
        ChartData d;
        d.node = node; d.dry = dry; d.wet = wet;
        d.samples = samples; d.humidity = humidity; d.temp = temp;
        return d;
    }

    void test_parses_record_sharing_message_with_ack() {
        std::string msg =
            "ACK from LMAO Server — received your message (89 bytes)\n"
            "DATA e824ad2d 37 53 2 25 26 2 54 55 3 46 45 47";
        ChartData d;
        assert(parse_data_line(msg, d));
        assert(d.node == "e824ad2d");
        assert(d.dry == 37 && d.wet == 53);
        assert(d.samples == std::vector<int>({46, 45, 47}));
    }

    void test_parses_all_three_series() {
        ChartData d;
        assert(parse_data_line("DATA e824ad2d 37 53 3 25 26 27 2 54 55 3 40 41 42", d));
        assert(d.temp == std::vector<float>({25, 26, 27}));
        assert(d.humidity == std::vector<int>({54, 55}));
        assert(d.samples == std::vector<int>({40, 41, 42}));
    }

    void test_parses_empty_series_counts() {
        ChartData d;
        assert(parse_data_line("DATA e824ad2d -1 -1 0 0 1 42", d));
        assert(d.dry == -1 && d.wet == -1);
        assert(d.temp.empty() && d.humidity.empty());
        assert(d.samples == std::vector<int>({42}));
    }

    void test_old_moisture_only_format_ignored() {
        ChartData d;
        assert(!parse_data_line("DATA e824ad2d 37 53 40 41 42", d));
    }

    void test_temp_only_line_rejected() {
        ChartData d;
        assert(!parse_data_line("DATA e824ad2d 37 53 2 25 26 0 0", d));
    }

    void test_rounds_fractional_values() {
        ChartData d;
        assert(parse_data_line("DATA e824ad2d 36.5 53.4 1 26.4 1 60.5 1 46.6", d));
        assert(d.dry == 36 && d.wet == 53);
        assert(d.temp == std::vector<float>({26.4f}));
        assert(d.humidity == std::vector<int>({60}));
        assert(d.samples == std::vector<int>({47}));
    }

    void test_ignores_non_data_traffic() {
        ChartData d;
        assert(!parse_data_line("ACK from LMAO Server", d));
        assert(!parse_data_line("", d));
    }

    void test_ignores_malformed_records() {
        ChartData d;
        assert(!parse_data_line("DATA e824ad2d 37", d));
        assert(!parse_data_line("DATA e824ad2d 37 53 1 notanumber", d));
        assert(!parse_data_line("DATA e824ad2d 37 53 0", d));
        assert(!parse_data_line("DATA e824ad2d 37 53 1 26 2 55", d));
        assert(!parse_data_line("DATA e824ad2d 37 53 1 26 1 55 1 40 999", d));
    }

    // ── geometry ─────────────────────────────────────────────────────────
    void test_window_contains_band() {
        int lo, hi;
        y_window(37, 53, {46, 46, 46}, {}, lo, hi);
        assert(lo <= 37 && hi >= 53);
        int lo2, hi2;
        y_window(37, 53, {46, 46}, {}, lo2, hi2);
        assert(hi2 - lo2 >= 20);
    }

    void test_window_clamped() {
        int lo, hi;
        y_window(0, 0, {0, 1}, {}, lo, hi);
        assert(lo >= 0);
        y_window(100, 100, {99, 100}, {}, lo, hi);
        assert(hi <= 100);
    }

    void test_y_px_orientation() {
        assert(y_px(60, 0, 100) < y_px(40, 0, 100));
        assert(y_px(100, 0, 100) == PLOT_T);
        assert(y_px(0, 0, 100) == PLOT_B);
    }

    void test_x_spans_plot_box() {
        assert(x_px(0, 10) == PLOT_L);
        assert(x_px(9, 10) == PLOT_R);
        assert(x_px(0, 1) == PLOT_L);
    }

    void test_temp_window() {
        float lo, hi;
        temp_window({20, 21, 22}, lo, hi);
        assert(lo <= 20 && hi >= 22);
        assert(hi - lo >= 4);
        temp_window({21.5f}, lo, hi);
        assert(lo < 21.5f && hi > 21.5f);
    }

    // ── draw ─────────────────────────────────────────────────────────────
    void test_draws_frame_thresholds_and_trace() {
        FakeChartDisplay tft;
        DrawResult r = draw(tft, data(37, 53, {46, 45, 47, 46}));
        assert(!r.error);
        assert(tft.fills == 1);
        assert(tft.has_color(DRY_COLOR));
        assert(tft.has_color(WET_COLOR));
        assert(tft.has_color(GREY));
        assert(r.points == 3);   // three trace segments for four samples
    }

    void test_newest_sample_labeled() {
        FakeChartDisplay tft;
        draw(tft, data(37, 53, {46, 45, 47, 46}));
        auto white = tft.texts_with(WHITE);
        bool found = false;
        for (auto& t : white) if (t.find("46") != std::string::npos) found = true;
        assert(found);
    }

    void test_missing_band_no_thresholds() {
        FakeChartDisplay tft;
        draw(tft, data(-1, -1, {46, 45, 47, 46}));
        assert(!tft.has_color(DRY_COLOR));
        assert(!tft.has_color(WET_COLOR));
        assert(tft.all_texts().find("dry --") != std::string::npos);
    }

    void test_draws_humidity_and_temperature_traces() {
        FakeChartDisplay tft;
        DrawResult r = draw(tft, data(37, 53, {46, 45, 47, 46},
                                      {50, 60, 70, 55}, {20, 21, 22, 23}));
        assert(!r.error);
        assert(tft.has_color(HUM_COLOR));
        assert(tft.has_color(TEMP_COLOR));
        assert(r.points == 9);   // three per trace × 3 traces
    }

    void test_header_shows_latest_air() {
        FakeChartDisplay tft;
        draw(tft, data(37, 53, {46, 45, 47, 46}, {50, 60, 70, 55}, {20, 21, 22, 23}));
        std::string t = tft.all_texts();
        assert(t.find("HUM 55%") != std::string::npos);
        assert(t.find("AIR 23C") != std::string::npos);
        assert(t.find("n=4") != std::string::npos);
    }

    void test_flat_temperature_visible_thick() {
        FakeChartDisplay tft;
        draw(tft, data(37, 53, {60, 60, 60, 60, 60}, {60, 60, 60, 60, 60}, {24, 24, 24, 24, 24}));
        int orange_segs = 0;
        for (auto& l : tft.lines) if ((uint32_t)l[4] == TEMP_COLOR) orange_segs++;
        assert(orange_segs >= 2 * 4);   // thick: y and y+1 per segment
        auto orange = tft.texts_with(TEMP_COLOR);
        bool found = false;
        for (auto& t : orange) if (t.find("24C") != std::string::npos) found = true;
        assert(found);
    }

    void test_single_sample_waits() {
        FakeChartDisplay tft;
        DrawResult r = draw(tft, data(37, 53, {46}));
        assert(!r.error);
        assert(tft.all_texts().find("waiting") != std::string::npos);
    }

}

int main() {
    test_parses_record_sharing_message_with_ack();
    test_parses_all_three_series();
    test_parses_empty_series_counts();
    test_old_moisture_only_format_ignored();
    test_temp_only_line_rejected();
    test_rounds_fractional_values();
    test_ignores_non_data_traffic();
    test_ignores_malformed_records();
    test_window_contains_band();
    test_window_clamped();
    test_y_px_orientation();
    test_x_spans_plot_box();
    test_temp_window();
    test_draws_frame_thresholds_and_trace();
    test_newest_sample_labeled();
    test_missing_band_no_thresholds();
    test_draws_humidity_and_temperature_traces();
    test_header_shows_latest_air();
    test_flat_temperature_visible_thick();
    test_single_sample_waits();
    std::puts("chart host tests passed");
    return 0;
}
