#include "chart.h"

#include <algorithm>
#include <cstdio>
#include <cmath>
#include <cstdlib>

namespace cardputer::chart {

    namespace {

        const char* PREFIX = "DATA ";

        bool parse_float(const std::string& tok, double& out) {
            if (tok.empty()) return false;
            char* end = nullptr;
            double v = strtod(tok.c_str(), &end);
            if (!end || *end != '\0') return false;
            if (!std::isfinite(v) || v > 1e9 || v < -1e9) return false;
            out = v;
            return true;
        }

        /* Python round() — round-half-to-even (matches chart.py exactly). */
        int py_round(double v) {
            double fl = std::floor(v);
            double diff = v - fl;
            if (diff < 0.5) return (int)fl;
            if (diff > 0.5) return (int)(fl + 1.0);
            // exactly .5 → round to even
            return ((long long)fl % 2 == 0) ? (int)fl : (int)(fl + 1.0);
        }

        void line_pixels(ChartDisplay& tft, int x0, int y0, int x1, int y1, uint32_t color) {
            // ChartDisplay always has a line() primitive (mirrors chart.py,
            // which probes for one and falls back to pixel Bresenham).
            tft.line(x0, y0, x1, y1, color);
        }

        // One trace routine for all three series (like chart.py): temp holds
        // floats °C, humidity/moisture hold ints; values are read as double.
        int trace(ChartDisplay& tft, const std::vector<double>& values,
                  double lo, double hi, uint32_t color, bool label, bool thick) {
            int count = (int)values.size();
            if (count < 2) {
                if (count == 1)
                    tft.pixel(x_px(0, count), y_px(values[0], lo, hi), color);
                return 0;
            }
            int px = x_px(0, count);
            int py = y_px(values[0], lo, hi);
            tft.pixel(px, py, color);
            int points = 0;
            for (int i = 1; i < count; i++) {
                int x = x_px(i, count);
                int y = y_px(values[i], lo, hi);
                line_pixels(tft, px, py, x, y, color);
                if (thick) line_pixels(tft, px, py + 1, x, y + 1, color);
                px = x; py = y;
                points++;
            }
            tft.pixel(px, py, color);
            tft.pixel(px - 1, py, color);
            tft.pixel(px, py - 1, color);
            tft.pixel(px - 1, py - 1, color);
            if (thick) {
                for (int oy = 1; oy <= 2; oy++)
                    for (int ox = 0; ox >= -1; ox--)
                        tft.pixel(px + ox, py + oy, color);
            }
            if (label) {
                char buf[16];
                snprintf(buf, sizeof buf, "%d", py_round(values[count - 1]));
                tft.text(buf, std::max(PLOT_L, px - 16), std::max(PLOT_T, py - 16), WHITE);
            }
            return points;
        }

    }

    bool parse_data_line(const std::string& text, ChartData& out) {
        if (text.empty()) return false;
        size_t line_start = 0;
        while (line_start <= text.size()) {
            size_t line_end = text.find('\n', line_start);
            if (line_end == std::string::npos) line_end = text.size();
            std::string line = text.substr(line_start, line_end - line_start);
            // trim
            size_t b = line.find_first_not_of(" \t\r");
            if (b == std::string::npos) { line_start = line_end + 1; continue; }
            size_t e = line.find_last_not_of(" \t\r");
            line = line.substr(b, e - b + 1);

            if (line.rfind(PREFIX, 0) == 0) {
                std::vector<std::string> parts;
                size_t p = 5;
                while (p <= line.size()) {
                    size_t sp = line.find(' ', p);
                    if (sp == std::string::npos) sp = line.size();
                    if (sp > p) parts.push_back(line.substr(p, sp - p));
                    if (sp == line.size()) break;
                    p = sp + 1;
                }

                // Mirrors chart.py's parse: node, dry, wet, then three
                // count-prefixed series (temp °C floats, humidity %, moisture %).
                try {
                    if (parts.size() < 4) { line_start = line_end + 1; continue; }
                    ChartData d;
                    d.node = parts[0];
                    double v;
                    if (!parse_float(parts[1], v)) { line_start = line_end + 1; continue; }
                    d.dry = py_round(v);
                    if (!parse_float(parts[2], v)) { line_start = line_end + 1; continue; }
                    d.wet = py_round(v);

                    size_t idx = 3;
                    // Reader guard shared by the three count-prefixed series.
                    auto read_next = [&](double& out) -> bool {
                        if (idx >= parts.size()) return false;
                        if (!parse_float(parts[idx], out)) return false;
                        idx++;
                        return true;
                    };
                    // ct
                    {
                        if (!read_next(v)) { line_start = line_end + 1; continue; }
                        int ct = py_round(v);
                        if (ct < 0 || ct > 4096) { line_start = line_end + 1; continue; }
                        for (int i = 0; i < ct; i++) {
                            double tv;
                            if (!read_next(tv)) return false;
                            d.temp.push_back(tv);
                        }
                    }
                    // ch
                    {
                        if (!read_next(v)) return false;
                        int ch = py_round(v);
                        if (ch < 0 || ch > 4096) return false;
                        for (int i = 0; i < ch; i++) {
                            double hv;
                            if (!read_next(hv)) return false;
                            d.humidity.push_back(py_round(hv));
                        }
                    }
                    // cm
                    {
                        if (!read_next(v)) return false;
                        int cm = py_round(v);
                        if (cm < 0 || cm > 4096) return false;
                        for (int i = 0; i < cm; i++) {
                            double mv;
                            if (!read_next(mv)) return false;
                            d.samples.push_back(py_round(mv));
                        }
                    }
                    if (idx != parts.size()) { line_start = line_end + 1; continue; }
                    if (d.humidity.empty() && d.samples.empty()) {
                        line_start = line_end + 1;   // percent-less → not a record
                        continue;
                    }
                    out = d;
                    return true;
                } catch (...) {
                    return false;
                }
            }
            line_start = line_end + 1;
        }
        return false;
    }

    void y_window(int dry, int wet, const std::vector<int>& soil,
                  const std::vector<int>& hum, int& lo, int& hi) {
        std::vector<int> values;
        values.insert(values.end(), soil.begin(), soil.end());
        values.insert(values.end(), hum.begin(), hum.end());
        if (dry >= 0) values.push_back(dry);
        if (wet >= 0) values.push_back(wet);
        if (values.empty()) { lo = 0; hi = 100; return; }
        int mn = *std::min_element(values.begin(), values.end());
        int mx = *std::max_element(values.begin(), values.end());
        lo = mn - 4;
        hi = mx + 4;
        if (hi - lo < 20) {
            int mid = (hi + lo) / 2;
            lo = mid - 10;
            hi = mid + 10;
        }
        if (lo < 0) lo = 0;
        if (hi > 100) hi = 100;
        if (hi - lo < 4) hi = lo + 4;
        if (hi > 100) { hi = 100; lo = hi - 4; }
    }

    void temp_window(const std::vector<float>& temps, float& lo, float& hi) {
        float mn = *std::min_element(temps.begin(), temps.end());
        float mx = *std::max_element(temps.begin(), temps.end());
        float pad = std::max(1.5f, (mx - mn) * 0.2f);
        lo = mn - pad;
        hi = mx + pad;
        if (hi - lo < 4) {
            float mid = (hi + lo) / 2.0f;
            lo = mid - 2;
            hi = mid + 2;
        }
    }

    int y_px(double value, double lo, double hi) {
        double span = hi - lo;
        if (span <= 0) return PLOT_B;
        double v = value - lo;
        if (v < 0) v = 0;
        if (v > span) v = span;
        // Python: bottom - (v * (bottom - top)) // span  (float floor-div)
        return (int)(PLOT_B - std::floor(v * (PLOT_B - PLOT_T) / span));
    }

    int x_px(int index, int count) {
        if (count <= 1) return PLOT_L;
        return PLOT_L + (long long)index * (PLOT_R - PLOT_L) / (count - 1);
    }

    DrawResult draw(ChartDisplay& tft, const ChartData& data) {
        DrawResult result;
        try {
            const std::vector<int>& samples = data.samples;
            const std::vector<int>& humidity = data.humidity;
            const std::vector<float>& temps = data.temp;
            const int dry = data.dry;
            const int wet = data.wet;

            tft.fill(0x000000);

            char header[96];
            {
                std::string soil_text = samples.empty() ? "--" : std::to_string(samples.back()) + "%";
                std::string hum_text  = humidity.empty() ? "--" : std::to_string(humidity.back()) + "%";
                std::string air_text  = temps.empty() ? "--" : std::to_string(py_round(temps.back())) + "C";
                snprintf(header, sizeof header, "SOIL %s  HUM %s  AIR %s",
                         soil_text.c_str(), hum_text.c_str(), air_text.c_str());
            }
            tft.text(header, 4, 2, WHITE);

            char footer[64];
            {
                std::string dry_label = (dry < 0) ? "--" : std::to_string(dry);
                std::string wet_label = (wet < 0) ? "--" : std::to_string(wet);
                snprintf(footer, sizeof footer, "dry %s  wet %s  n=%d",
                         dry_label.c_str(), wet_label.c_str(), (int)samples.size());
            }
            tft.text(footer, 4, H - 11, GREY);

            if ((int)samples.size() < 2 && (int)humidity.size() < 2 && (int)temps.size() < 2) {
                tft.text("waiting for samples", 40, 62, GREY);
                return result;
            }

            int lo, hi;
            y_window(dry, wet, samples, humidity, lo, hi);
            double tlo = 0, thi = 0;
            if (!temps.empty()) {
                float flo, fhi;
                temp_window(temps, flo, fhi);
                tlo = flo; thi = fhi;
            }

            // Frame + percentage labels on the left.
            line_pixels(tft, PLOT_L, PLOT_T, PLOT_R, PLOT_T, GREY);
            line_pixels(tft, PLOT_L, PLOT_B, PLOT_R, PLOT_B, GREY);
            line_pixels(tft, PLOT_L, PLOT_T, PLOT_L, PLOT_B, GREY);
            line_pixels(tft, PLOT_R, PLOT_T, PLOT_R, PLOT_B, GREY);
            char buf[16];
            snprintf(buf, sizeof buf, "%d", (int)std::lround(hi));
            tft.text(buf, 2, PLOT_T, GREY);
            snprintf(buf, sizeof buf, "%d", (int)std::lround(lo));
            tft.text(buf, 2, PLOT_B - 8, GREY);

            if (!samples.empty()) {
                if (dry >= 0) {
                    int y_dry = y_px(dry, lo, hi);
                    line_pixels(tft, PLOT_L + 1, y_dry, PLOT_R - 1, y_dry, DRY_COLOR);
                    snprintf(buf, sizeof buf, "%d", dry);
                    tft.text(buf, PLOT_L + 3, std::max(PLOT_T, y_dry - 7), DRY_COLOR);
                }
                if (wet >= 0) {
                    int y_wet = y_px(wet, lo, hi);
                    line_pixels(tft, PLOT_L + 1, y_wet, PLOT_R - 1, y_wet, WET_COLOR);
                    snprintf(buf, sizeof buf, "%d", wet);
                    tft.text(buf, PLOT_L + 3, std::max(PLOT_T, y_wet - 7), WET_COLOR);
                }
            }

            {
                std::vector<double> soil_d(samples.begin(), samples.end());
                std::vector<double> hum_d(humidity.begin(), humidity.end());
                result.points += trace(tft, soil_d, (double)lo, (double)hi, SOIL_COLOR, true, false);
                result.points += trace(tft, hum_d, (double)lo, (double)hi, HUM_COLOR, false, false);
            }
            if (!temps.empty()) {
                std::vector<double> temp_d(temps.begin(), temps.end());
                result.points += trace(tft, temp_d, tlo, thi, TEMP_COLOR, false, true);

                snprintf(buf, sizeof buf, "%d", py_round(thi));
                tft.text(buf, TEMP_AXIS_X, PLOT_T, TEMP_COLOR);
                snprintf(buf, sizeof buf, "%d", py_round(tlo));
                tft.text(buf, TEMP_AXIS_X, PLOT_B - 8, TEMP_COLOR);
                int lx = x_px((int)temps.size() - 1, (int)temps.size());
                int ly = y_px(temps.back(), tlo, thi);
                snprintf(buf, sizeof buf, "%.0fC", (double)temps.back());
                tft.text(buf, std::max(PLOT_L, lx - 24), std::max(PLOT_T, ly - 7), TEMP_COLOR);
            }

            return result;
        } catch (const std::exception& exc) {
            result.error = true;
            result.error_message = exc.what();
            return result;
        } catch (...) {
            result.error = true;
            result.error_message = "unknown drawing error";
            return result;
        }
    }

}
