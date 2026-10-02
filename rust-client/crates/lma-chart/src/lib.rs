//! `lma-chart` — the Cardputer Sprout chart, in Rust (no_std).
//!
//! A faithful port of the MicroPython `cardputer_client/chart.py` renderer
//! that the Cardputer's screen used to show — soil moisture, air humidity and
//! air temperature traces plus the pump's watering markers.  It exists so the
//! chart rides the Rust stack end to end:
//!
//! 1. the Rust server's `SproutHistory` ([`crate` counterpart in
//!    `lmao-server-rs/src/sprout.rs`]) emits the same `DATA …` line the
//!    parser here consumes (one format, shared with the firmware);
//! 2. the Rust firmware's LXMF reply handler parses that line into a
//!    [`ChartRecord`] ([`parse_data_line`]) and paints it with [`draw`];
//! 3. this crate is no_std and draws through the two-primitive [`Display`]
//!    trait (`fill` + `pixel`), so a driver only provides a framebuffer —
//!    lines, traces and the embedded 5x7 text are rendered here over
//!    `pixel`, which keeps every pixel decision host-testable without any
//!    panel.
//!
//! Geometry and colours are byte-for-byte the Python original: same 240×135
//! plot box, same auto-scaled % and °C windows, same red/cyan band
//! thresholds, same blue watering ticks.

#![no_std]
extern crate alloc;

use alloc::format;
use alloc::string::String;
use alloc::string::ToString;
use alloc::vec::Vec;

mod font;
use font::FONT_8X8;

/// Whichever RGB888 colour constants the reference renderer used.
pub const WHITE: u32 = 0xFF_FF_FF;
pub const GREY: u32 = 0x80_80_80;
/// Red   — the soil DRY threshold the pump works to stay above.
pub const DRY_COLOR: u32 = 0xFF_00_00;
/// Cyan  — the soil WET threshold that ends a watering session.
pub const WET_COLOR: u32 = 0x00_FF_FF;
pub const SOIL_COLOR: u32 = 0xFF_FF_FF;
pub const HUM_COLOR: u32 = 0x00_FF_00;
/// Orange — air temperature (its own right-hand °C axis).
pub const TEMP_COLOR: u32 = 0xFF_A5_00;
/// Blue  — watering-active tick above the soil trace.
pub const WATER_COLOR: u32 = 0x00_00_FF;

/// Panel size and plot box (the right gutter holds the temp °C axis).
pub const W: i32 = 240;
pub const H: i32 = 135;
pub const PLOT_L: i32 = 28;
pub const PLOT_R: i32 = 210;
pub const PLOT_T: i32 = 18;
pub const PLOT_B: i32 = 116;
pub const TEMP_AXIS_X: i32 = PLOT_R + 6;

const PREFIX: &str = "DATA ";

/// Pixel canvas a chart renders onto.  `fill` clears the whole panel to an
/// RGB888 colour; `pixel` sets one pixel.  Everything else the chart needs
/// (lines, traces, text) is this crate's own logic over `pixel`, so a driver
/// (an LCD framebuffer in the firmware, a fake in the host tests) only has to
/// implement these two.
pub trait Display {
    /// Clear the entire panel to `rgb`.
    fn fill(&mut self, rgb: u32);
    /// Set one pixel; an out-of-bounds coordinate may be ignored.
    fn pixel(&mut self, x: i32, y: i32, rgb: u32);
}

/// A parsed `DATA …` chart record.  Mirrors the field set of the Python
/// `chart.parse_data_line` and of the server's `SproutHistory` (soil counts
/// are the `DATA_AIR_MAX_SAMPLES`-capped trailing series).
#[derive(Debug, Clone, PartialEq)]
pub struct ChartRecord {
    /// First 8 hex chars of the reporting node id (informational).
    pub node: String,
    /// Soil dry threshold in %, `-1` = the node has not reported a band.
    pub dry: i64,
    /// Soil wet threshold in %, `-1` = unknown.
    pub wet: i64,
    /// Air temperature series, oldest first, °C.
    pub temp: Vec<f32>,
    /// Air humidity series, oldest first, integer %.
    pub humidity: Vec<i64>,
    /// Soil-moisture series, oldest first, integer %.
    pub samples: Vec<i64>,
    /// Watering mask: bit `i` = the pump was physically active at sample `i`.
    pub water_mask: u64,
}

impl ChartRecord {
    /// `true` at each moisture sample where the pump was active (bit `i`).
    pub fn water(&self) -> Vec<bool> {
        (0..self.samples.len().min(64))
            .map(|i| (self.water_mask >> i) & 1 == 1)
            .collect()
    }
}

/// Round-half-even like Python's `round(float())`, so the firmware agrees with
/// the Python-era tests (`round(36.5)==36`, `round(60.5)==60`).  Implemented
/// from truncation (`f32::floor` is std-only and this crate is no_std).
fn roundi(x: f32) -> i64 {
    let t = x as i64; // truncates toward zero
    let frac = (x - t as f32).abs();
    if frac < 0.5 {
        t
    } else if frac > 0.5 {
        if x >= 0.0 { t + 1 } else { t - 1 }
    } else if t % 2 == 0 {
        t
    } else if x >= 0.0 {
        t + 1
    } else {
        t - 1
    }
}

/// Extract the first `DATA ` record from *text*, or None.
///
/// Tolerates the ACK text sharing the message (the server sends its ACK line
/// first, then the DATA line) and ignores malformed records rather than
/// raising — later valid records in the same text still parse.
pub fn parse_data_line(text: &str) -> Option<ChartRecord> {
    for raw in text.lines() {
        let line = raw.trim();
        if !line.starts_with(PREFIX) {
            continue;
        }
        let all: Vec<&str> = line.split_whitespace().collect();
        if all.len() < 6 || all[0] != "DATA" {
            continue;
        }
        if let Some(rec) = parse_record(&all) {
            return Some(rec);
        }
    }
    None
}

/// Parse one whitespace-split DATA line; None on any malformation.
fn parse_record(all: &[&str]) -> Option<ChartRecord> {
    let node = String::from(all[1]);
    let dry = all[2].parse::<f32>().ok().map(roundi)?;
    let wet = all[3].parse::<f32>().ok().map(roundi)?;
    let ct: usize = all.get(4)?.parse().ok()?;
    let mut idx = 5;
    let mut temp = Vec::new();
    for _ in 0..ct {
        temp.push(all.get(idx)?.parse::<f32>().ok()?);
        idx += 1;
    }
    let ch: usize = all.get(idx)?.parse().ok()?;
    idx += 1;
    let mut humidity = Vec::new();
    for _ in 0..ch {
        humidity.push(all.get(idx)?.parse::<f32>().ok().map(roundi)?);
        idx += 1;
    }
    let cm: usize = all.get(idx)?.parse().ok()?;
    idx += 1;
    let mut samples = Vec::new();
    for _ in 0..cm {
        samples.push(all.get(idx)?.parse::<f32>().ok().map(roundi)?);
        idx += 1;
    }
    // Optional trailing watering mask (absent on old lines).
    let water_mask = match all.len() - idx {
        0 => 0u64,
        1 => all[idx].parse::<u64>().ok()?,
        _ => return None,
    };
    // A percent-less line can never come from the server — reaching draw()
    // would blank the chart.  Reject (mirrors the Python guard).
    if humidity.is_empty() && samples.is_empty() {
        return None;
    }
    Some(ChartRecord { node, dry, wet, temp, humidity, samples, water_mask })
}

/// Percent range `[lo, hi]` to draw, always including the band.  A minimum
/// span keeps a nearly-flat series readable instead of amplifying one-point
/// jitter across the whole screen height.
pub fn y_window(dry: i64, wet: i64, series: &[&[i64]]) -> (i64, i64) {
    let mut values: Vec<i64> = Vec::new();
    for s in series {
        values.extend_from_slice(s);
    }
    if dry >= 0 {
        values.push(dry);
    }
    if wet >= 0 {
        values.push(wet);
    }
    if values.is_empty() {
        // Defense in depth: draw() always passes at least one percent series.
        return (0, 100);
    }
    let mut lo = *values.iter().min().unwrap() - 4;
    let mut hi = *values.iter().max().unwrap() + 4;
    if hi - lo < 20 {
        let mid = (hi + lo) / 2;
        lo = mid - 10;
        hi = mid + 10;
    }
    if lo < 0 {
        lo = 0;
    }
    if hi > 100 {
        hi = 100;
    }
    if hi - lo < 4 {
        hi = lo + 4;
    }
    if hi > 100 {
        hi = 100;
        lo = hi - 4;
    }
    (lo, hi)
}

/// Auto-scaled °C range for the temperature trace.
pub fn temp_window(temps: &[f32]) -> (f32, f32) {
    let mut lo = f32::MAX;
    let mut hi = f32::MIN;
    for t in temps {
        lo = lo.min(*t);
        hi = hi.max(*t);
    }
    if lo == f32::MAX {
        return (0.0, 0.0);
    }
    let pad = 1.5f32.max((hi - lo) * 0.2);
    lo -= pad;
    hi += pad;
    if hi - lo < 4.0 {
        let mid = (hi + lo) / 2.0;
        lo = mid - 2.0;
        hi = mid + 2.0;
    }
    (lo, hi)
}

/// Map a value to a screen row (y grows downward).  `bottom` is the lo end.
pub fn y_px(value: f32, lo: f32, hi: f32, top: i32, bottom: i32) -> i32 {
    let span = hi - lo;
    if span <= 0.0 {
        return bottom;
    }
    let v = (value - lo).clamp(0.0, span);
    bottom - (v * (bottom - top) as f32 / span) as i32
}

/// Map a sample index to a screen column.
pub fn x_px(index: i64, count: i64, left: i32, right: i32) -> i32 {
    if count <= 1 {
        return left;
    }
    left + (index * (right - left) as i64 / (count - 1)) as i32
}

/// Bresenham line (the Python `_line` fallback path — the chart here always
/// draws through `pixel`, so every driver only needs `fill` + `pixel`).
pub fn line<D: Display>(tft: &mut D, x0: i32, y0: i32, x1: i32, y1: i32, color: u32) {
    let mut x0 = x0;
    let mut y0 = y0;
    let dx = (x1 - x0).abs();
    let dy = -(y1 - y0).abs();
    let sx = if x0 < x1 { 1 } else { -1 };
    let sy = if y0 < y1 { 1 } else { -1 };
    let mut err = dx + dy;
    loop {
        tft.pixel(x0, y0, color);
        if x0 == x1 && y0 == y1 {
            break;
        }
        let e2 = 2 * err;
        if e2 >= dy {
            err += dy;
            x0 += sx;
        }
        if e2 <= dx {
            err += dx;
            y0 += sy;
        }
    }
}

/// 8x8 text, drawn on top of the chart (labels are part of the display; a
/// driver need not supply fonts).
pub const FONT_ADVANCE: i32 = 8;

fn glyph(c: u8) -> Option<&'static [u8; 8]> {
    if !(0x20..=0x7e).contains(&c) {
        return None;
    }
    FONT_8X8.get((c - 0x20) as usize)
}

/// Draw `s` starting at `(x, y)` (top-left of the first glyph).
pub fn text<D: Display>(tft: &mut D, s: &str, x: i32, y: i32, color: u32) {
    let mut ox = x;
    for b in s.bytes() {
        if let Some(g) = glyph(b) {
            for row in 0u8..8 {
                let byte = g[row as usize];
                for bit in 0u8..8 {
                    if (byte >> bit) & 1 == 1 {
                        tft.pixel(ox + (7 - bit) as i32, y + row as i32, color);
                    }
                }
            }
        }
        ox += FONT_ADVANCE;
    }
}

/// Draw one time series across the plot box.  Returns the number of line
/// segments drawn.  The newest sample gets a 2×2 marker; `label` additionally
/// prints its value (primary soil series); `thick` doubles each segment so a
/// flat series is still visibly a trace (air temperature).
fn trace<D: Display>(
    tft: &mut D,
    values: &[f32],
    lo: f32,
    hi: f32,
    color: u32,
    label: bool,
    thick: bool,
) -> usize {
    let count = values.len() as i64;
    if count < 2 {
        if count == 1 {
            tft.pixel(x_px(0, count, PLOT_L, PLOT_R), y_px(values[0], lo, hi, PLOT_T, PLOT_B), color);
        }
        return 0;
    }
    let mut px = x_px(0, count, PLOT_L, PLOT_R);
    let mut py = y_px(values[0], lo, hi, PLOT_T, PLOT_B);
    tft.pixel(px, py, color);
    let mut points = 0;
    for i in 1..values.len() {
        let x = x_px(i as i64, count, PLOT_L, PLOT_R);
        let y = y_px(values[i], lo, hi, PLOT_T, PLOT_B);
        line(tft, px, py, x, y, color);
        if thick {
            line(tft, px, py + 1, x, y + 1, color);
        }
        px = x;
        py = y;
        points += 1;
    }
    tft.pixel(px, py, color);
    tft.pixel(px - 1, py, color);
    tft.pixel(px, py - 1, color);
    tft.pixel(px - 1, py - 1, color);
    if thick {
        for oy in [1i32, 2] {
            for ox in [0i32, -1] {
                tft.pixel(px + ox, py + oy, color);
            }
        }
    }
    if label {
        text(tft, &format!("{:.0}", values[values.len() - 1]), PLOT_L.max(px - 16), PLOT_T.max(py - 16), WHITE);
    }
    points
}

/// Render the chart.  Never panics: pure geometry + the [Display] contract.
/// Returns the number of trace segments drawn.
pub fn draw<D: Display>(tft: &mut D, data: &ChartRecord) -> usize {
    let samples = &data.samples;
    let humidity = &data.humidity;
    let temps = &data.temp;
    let dry = data.dry;
    let wet = data.wet;

    tft.fill(0x000000);
    let soil_text = match samples.last() {
        Some(v) => format!("{v}%"),
        None => "--".to_string(),
    };
    let hum_text = match humidity.last() {
        Some(v) => format!("{v}%"),
        None => "--".to_string(),
    };
    let air_text = match temps.last() {
        Some(v) => format!("{:.0}C", v),
        None => "--".to_string(),
    };
    text(tft, &format!("SOIL {soil_text}  HUM {hum_text}  AIR {air_text}"), 4, 2, WHITE);
    let dry_label = if dry < 0 { "--".to_string() } else { format!("{dry}") };
    let wet_label = if wet < 0 { "--".to_string() } else { format!("{wet}") };
    text(tft, &format!("dry {dry_label}  wet {wet_label}  n={}", samples.len()), 4, H - 11, GREY);

    if samples.len() < 2 && humidity.len() < 2 && temps.len() < 2 {
        text(tft, "waiting for samples", 40, 62, GREY);
        return 0;
    }

    // Percent axis (soil + humidity); temperature maps onto its own scale.
    let (lo, hi) = y_window(dry, wet, &[&samples[..], &humidity[..]]);
    let (tlo, thi) = if !temps.is_empty() { temp_window(temps) } else { (0.0, 0.0) };

    // Frame + percentage labels on the left.
    line(tft, PLOT_L, PLOT_T, PLOT_R, PLOT_T, GREY);
    line(tft, PLOT_L, PLOT_B, PLOT_R, PLOT_B, GREY);
    line(tft, PLOT_L, PLOT_T, PLOT_L, PLOT_B, GREY);
    line(tft, PLOT_R, PLOT_T, PLOT_R, PLOT_B, GREY);
    text(tft, &format!("{hi}"), 2, PLOT_T, GREY);
    text(tft, &format!("{lo}"), 2, PLOT_B - 8, GREY);

    // Soil thresholds (the node's active band), labelled in their own colour.
    // Only drawn with the soil series, since they are soil thresholds.
    if !samples.is_empty() {
        if dry >= 0 {
            let y_dry = y_px(dry as f32, lo as f32, hi as f32, PLOT_T, PLOT_B);
            line(tft, PLOT_L + 1, y_dry, PLOT_R - 1, y_dry, DRY_COLOR);
            text(tft, &format!("{dry}"), PLOT_L + 3, PLOT_T.max(y_dry - 7), DRY_COLOR);
        }
        if wet >= 0 {
            let y_wet = y_px(wet as f32, lo as f32, hi as f32, PLOT_T, PLOT_B);
            line(tft, PLOT_L + 1, y_wet, PLOT_R - 1, y_wet, WET_COLOR);
            text(tft, &format!("{wet}"), PLOT_L + 3, PLOT_T.max(y_wet - 7), WET_COLOR);
        }
    }

    // The three traces: soil (white, labelled), humidity (green, shares the
    // % axis), temperature (orange, own °C axis, thick + labelled).
    let samples_f: Vec<f32> = samples.iter().map(|&v| v as f32).collect();
    let humidity_f: Vec<f32> = humidity.iter().map(|&v| v as f32).collect();
    let mut points = 0;
    points += trace(tft, &samples_f, lo as f32, hi as f32, SOIL_COLOR, true, false);
    points += trace(tft, &humidity_f, lo as f32, hi as f32, HUM_COLOR, false, false);
    if !temps.is_empty() {
        points += trace(tft, temps, tlo, thi, TEMP_COLOR, false, true);
        text(tft, &format!("{:.0}", thi), TEMP_AXIS_X, PLOT_T, TEMP_COLOR);
        text(tft, &format!("{:.0}", tlo), TEMP_AXIS_X, PLOT_B - 8, TEMP_COLOR);
        let lx = x_px((temps.len() - 1) as i64, temps.len() as i64, PLOT_L, PLOT_R);
        let ly = y_px(*temps.last().unwrap(), tlo, thi, PLOT_T, PLOT_B);
        text(tft, &format!("{:.0}C", temps.last().unwrap()), PLOT_L.max(lx - 24), PLOT_T.max(ly - 7), TEMP_COLOR);
    }

    // Watering markers: a blue tick hanging from the top frame at each sample
    // column where the pump was active, aligned with the moisture series.
    if !samples.is_empty() {
        let n = samples.len() as i64;
        for (i, active) in data.water().into_iter().enumerate() {
            if active {
                let x = x_px(i as i64, n, PLOT_L, PLOT_R);
                line(tft, x, PLOT_T, x, PLOT_T + 6, WATER_COLOR);
                tft.pixel(x, PLOT_T + 7, WATER_COLOR);
            }
        }
    }

    points
}
