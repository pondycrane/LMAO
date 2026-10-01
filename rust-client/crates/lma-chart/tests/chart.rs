//! Host tests for `lma-chart` — the Rust recreation of the Cardputer's chart
//! (port of `cardputer_client/chart.py`; assertions mirror `tests/test_chart.py`).
//!
//! A fake display records pixels — no hardware, no panel driver.

use lma_chart::{self as chart};
use lma_chart::{
    y_px, x_px, y_window, temp_window, parse_data_line, draw, Display,
    ChartRecord, W, H,
};
use lma_chart::{
    PLOT_L, PLOT_R, PLOT_T, PLOT_B, WHITE, GREY, DRY_COLOR, WET_COLOR,
    SOIL_COLOR, HUM_COLOR, TEMP_COLOR, WATER_COLOR,
};
use std::collections::{HashMap, HashSet};

/// Records what the chart drew; mimics the two-primitive `Display` contract.
#[derive(Default)]
struct FakeTft {
    pixels: HashMap<(i32, i32), u32>,
    fills: usize,
}

impl FakeTft {
    fn colors(&self) -> HashSet<u32> {
        self.pixels.values().copied().collect()
    }
    /// Columns holding a WATER_COLOR tick top (`y == PLOT_T`).
    fn water_marker_xs(&self) -> Vec<i32> {
        self.pixels
            .iter()
            .filter(|((_, y), c)| *y == PLOT_T && **c == WATER_COLOR)
            .map(|((x, _), _)| *x)
            .collect()
    }
}

impl Display for FakeTft {
    fn fill(&mut self, rgb: u32) {
        self.fills += 1;
        self.pixels.clear();
        // Fill the whole panel so out-of-plot pixels are observable too.
        for y in 0..H {
            for x in 0..W {
                self.pixels.insert((x, y), rgb);
            }
        }
    }
    fn pixel(&mut self, x: i32, y: i32, rgb: u32) {
        self.pixels.insert((x, y), rgb);
    }
}

fn data(dry: i64, wet: i64, samples: &[i64]) -> ChartRecord {
    ChartRecord {
        node: "e824ad2d".into(),
        dry,
        wet,
        temp: vec![],
        humidity: vec![],
        samples: samples.to_vec(),
        water_mask: 0,
    }
}

// ---------------- parse_data_line ----------------

#[test]
fn parses_a_record_sharing_the_message_with_the_ack() {
    let msg = format!(
        "ACK from LMAO Server — received your message (89 bytes)\nDATA e824ad2d 37 53 2 25 26 2 54 55 3 46 45 47"
    );
    let d = parse_data_line(&msg).unwrap();
    assert_eq!(d.node, "e824ad2d");
    assert_eq!(d.dry, 37);
    assert_eq!(d.wet, 53);
    assert_eq!(d.samples, vec![46, 45, 47], "oldest first, order preserved");
}

#[test]
fn parses_all_three_series_with_their_counts() {
    let d = parse_data_line("DATA e824ad2d 37 53 3 25 26 27 2 54 55 3 40 41 42").unwrap();
    assert_eq!(d.temp, vec![25.0, 26.0, 27.0], "temperature kept as °C floats");
    assert_eq!(d.humidity, vec![54, 55], "humidity as integer percent");
    assert_eq!(d.samples, vec![40, 41, 42]);
}

#[test]
fn parses_empty_series_counts() {
    let d = parse_data_line("DATA e824ad2d -1 -1 0 0 1 42").unwrap();
    assert_eq!(d.dry, -1);
    assert_eq!(d.wet, -1);
    assert!(d.temp.is_empty());
    assert!(d.humidity.is_empty());
    assert_eq!(d.samples, vec![42]);
}

#[test]
fn old_moisture_only_format_is_ignored() {
    // Would misparse the first count field — ignored rather than drawn as
    // garbage (the server and cardputer are flashed together).
    assert!(parse_data_line("DATA e824ad2d 37 53 40 41 42").is_none());
}

#[test]
fn temp_only_line_is_rejected() {
    // Air is only ever emitted alongside moisture; a percent-less line would
    // blank the chart, so it is not a valid record.
    assert!(parse_data_line("DATA e824ad2d 37 53 2 25 26 0 0").is_none());
}

#[test]
fn reports_unknown_band_as_minus_one() {
    let d = parse_data_line("DATA e824ad2d -1 -1 0 0 2 40 41").unwrap();
    assert_eq!(d.dry, -1);
    assert_eq!(d.wet, -1);
    assert_eq!(d.samples, vec![40, 41]);
}

#[test]
fn rounds_fractional_values() {
    let d = parse_data_line("DATA e824ad2d 36.5 53.4 1 26.4 1 60.5 1 46.6").unwrap();
    assert_eq!((d.dry, d.wet), (36, 53));
    assert_eq!(d.temp, vec![26.4]);
    assert_eq!(d.humidity, vec![60]);
    assert_eq!(d.samples, vec![47]);
}

#[test]
fn ignores_non_data_traffic() {
    assert!(parse_data_line("ACK from LMAO Server").is_none());
    assert!(parse_data_line("").is_none());
}

#[test]
fn ignores_malformed_records() {
    assert!(parse_data_line("DATA e824ad2d 37").is_none());
    assert!(parse_data_line("DATA e824ad2d 37 53 1 notanumber").is_none());
    assert!(parse_data_line("DATA e824ad2d 37 53 1 26").is_none());
    assert!(parse_data_line("DATA e824ad2d 37 53 1 26 2 55").is_none());
    // More than one trailing token is garbage, not a watering mask.
    assert!(parse_data_line("DATA e824ad2d 37 53 1 26 1 55 1 40 0 5").is_none());
}

#[test]
fn parses_the_trailing_watering_mask() {
    let d = parse_data_line("DATA e824ad2d 37 53 1 26 1 55 3 40 41 42 4").unwrap();
    assert_eq!(d.samples, vec![40, 41, 42]);
    assert_eq!(d.water(), vec![false, false, true], "bit 2 (sample index 2) set");
}

#[test]
fn missing_watering_mask_defaults_to_all_off() {
    let d = parse_data_line("DATA e824ad2d 37 53 3 25 26 27 2 54 55 3 40 41 42").unwrap();
    assert_eq!(d.water(), vec![false, false, false]);
}

#[test]
fn watering_mask_all_bits_set() {
    let d = parse_data_line("DATA e824ad2d 37 53 0 0 3 40 41 42 7").unwrap();
    assert_eq!(d.water(), vec![true, true, true]);
}

// ---------------- geometry ----------------

#[test]
fn window_always_contains_the_band() {
    let (lo, hi) = y_window(37, 53, &[&[46, 46, 46]]);
    assert!(lo <= 37 && hi >= 53);
}

#[test]
fn window_keeps_a_minimum_span_for_a_flat_series() {
    let (lo, hi) = y_window(37, 53, &[&[46, 46]]);
    assert!(hi - lo >= 20);
}

#[test]
fn window_is_clamped_to_the_sensor_range() {
    let (lo, hi) = y_window(0, 0, &[&[0, 1]]);
    assert!(lo >= 0);
    let (lo, hi) = y_window(100, 100, &[&[99, 100]]);
    assert!(hi <= 100);
}

#[test]
fn empty_window_returns_the_default_range() {
    assert_eq!(y_window(-1, -1, &[]), (0, 100));
}

#[test]
fn temp_window_pads_and_wraps_the_values() {
    let (lo, hi) = temp_window(&[20.0, 21.0, 22.0]);
    assert!(lo <= 20.0 && hi >= 22.0, "window covers every temperature");
    assert!(hi - lo >= 4.0, "flat series keeps a readable span");
}

#[test]
fn temp_window_handles_a_flat_or_single_value() {
    let (lo, hi) = temp_window(&[21.5]);
    assert!(lo < 21.5 && 21.5 < hi);
    assert!(hi - lo >= 4.0);
}

#[test]
fn temp_window_handles_negative_values() {
    let (lo, hi) = temp_window(&[-3.0, -1.0]);
    assert!(lo <= -3.0 && hi >= -1.0);
}

#[test]
fn higher_moisture_draws_higher_on_screen() {
    assert!(y_px(60.0, 0.0, 100.0, PLOT_T, PLOT_B) < y_px(40.0, 0.0, 100.0, PLOT_T, PLOT_B));
    assert_eq!(y_px(100.0, 0.0, 100.0, PLOT_T, PLOT_B), PLOT_T);
    assert_eq!(y_px(0.0, 0.0, 100.0, PLOT_T, PLOT_B), PLOT_B);
}

#[test]
fn x_spans_the_plot_box() {
    assert_eq!(x_px(0, 10, PLOT_L, PLOT_R), PLOT_L);
    assert_eq!(x_px(9, 10, PLOT_L, PLOT_R), PLOT_R);
    assert_eq!(x_px(0, 1, PLOT_L, PLOT_R), PLOT_L);
}

// ---------------- draw ----------------

fn base_record() -> ChartRecord {
    data(37, 53, &[46, 45, 47, 46])
}

#[test]
fn draws_frame_thresholds_and_trace() {
    let mut tft = FakeTft::default();
    let points = draw(&mut tft, &base_record());
    assert_eq!(tft.fills, 1, "clears the screen first");
    assert!(tft.colors().contains(&DRY_COLOR), "dry line drawn");
    assert!(tft.colors().contains(&WET_COLOR), "wet line drawn");
    assert!(tft.colors().contains(&GREY), "frame drawn");
    assert_eq!(points, 3, "three trace segments for four samples");
}

#[test]
fn draws_header_and_footer_labels() {
    let mut tft = FakeTft::default();
    draw(&mut tft, &base_record());
    // Header lives in the top band, footer in the bottom band; both white/grey
    // pixels must exist on the panel outside the plot box.
    let top_text = tft.pixels.iter().any(|((x, y), c)| *y < PLOT_T && *x < 60 && is_whiteish(*c));
    let bottom_text = tft.pixels
        .iter()
        .any(|((_, y), c)| *y > PLOT_B && *c == GREY);
    assert!(top_text, "legend text drawn at the top");
    assert!(bottom_text, "dry/wet footer drawn at the bottom");
}

fn is_whiteish(c: u32) -> bool {
    c == WHITE || c == SOIL_COLOR
}

#[test]
fn draws_watering_markers_at_the_active_columns() {
    let mut rec = base_record();
    rec.water_mask = 0b0101; // samples 0 and 2 watered
    let mut tft = FakeTft::default();
    draw(&mut tft, &rec);
    assert!(tft.colors().contains(&WATER_COLOR), "watering marker drawn in blue");
    let xs = tft.water_marker_xs();
    assert!(xs.contains(&x_px(0, 4, PLOT_L, PLOT_R)), "marker at the first watering sample");
    assert!(xs.contains(&x_px(2, 4, PLOT_L, PLOT_R)), "marker at the third watering sample");
    assert!(!xs.contains(&x_px(1, 4, PLOT_L, PLOT_R)), "no marker where watering is off");
    assert!(!xs.contains(&x_px(3, 4, PLOT_L, PLOT_R)), "no marker where watering is off");
}

#[test]
fn no_watering_mask_draws_no_markers() {
    let mut tft = FakeTft::default();
    draw(&mut tft, &base_record());
    assert!(!tft.colors().contains(&WATER_COLOR), "no watering markers without a mask");
}

#[test]
fn draws_all_three_series_and_the_temp_axis() {
    let rec = ChartRecord {
        node: "e824ad2d".into(),
        dry: 37,
        wet: 53,
        temp: vec![21.5, 22.0, 22.5, 23.0],
        humidity: vec![54, 55, 56, 57],
        samples: vec![46, 45, 47, 46],
        water_mask: 0b1001,
    };
    let mut tft = FakeTft::default();
    let points = draw(&mut tft, &rec);
    assert!(tft.colors().contains(&HUM_COLOR), "humidity trace drawn");
    assert!(tft.colors().contains(&TEMP_COLOR), "temperature trace drawn");
    assert!(tft.colors().contains(&WATER_COLOR), "water markers drawn");
    // 4 samples -> 3 soil segments; 4 humidity -> 3; 4 temps -> 3.
    assert_eq!(points, 9);
    // Temp axis labels live in the right gutter (PLOT_R..W).
    let gutter = tft.pixels.iter().any(|((x, _), c)| *x > PLOT_R && *c == TEMP_COLOR);
    assert!(gutter, "temperature axis drawn in the right gutter");
}

#[test]
fn waiting_for_samples_keeps_the_status_line() {
    // A single sample is not enough for a trace — the reference renderer
    // shows the header + "waiting" placeholder and draws no geometry.
    let rec = data(37, 53, &[46]);
    let mut tft = FakeTft::default();
    let points = draw(&mut tft, &rec);
    assert_eq!(points, 0, "no trace segments drawn yet");
    // The white legend header is still drawn.
    assert!(tft.pixels.iter().any(|((x, y), c)| *y < PLOT_T && *x < 80 && is_whiteish(*c)));
}
