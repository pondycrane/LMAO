//! Sprout chart history — Rust port of `lma_core/sprout_history.py`.
//!
//! Ring of recent moisture/temperature/humidity samples for one Sprout node.
//! Folds SensorReport readings (by sensor_id) and renders the ACK-piggyback
//! `DATA` line the Cardputer charts: `DATA <node8> <dry> <wet> [n v…] (temp
//! air + humidity + soil series) <pump-mask>`.

use parking_lot::Mutex;

const DEFAULT_MAXLEN: usize = 30;
const DATA_AIR_MAX_SAMPLES: usize = 10;

const SENSOR_AIR_HUMIDITY: u32 = 2;
const SENSOR_AIR_TEMP: u32 = 3;
const SENSOR_SOIL_MOISTURE: u32 = 4;
const SENSOR_PUMP_ACTIVE: u32 = 7;
const SENSOR_PROFILE_DRY: u32 = 10;
const SENSOR_PROFILE_WET: u32 = 11;

#[derive(Default)]
pub struct SproutHistory {
    maxlen: usize,
    moisture: Vec<f32>,
    temp: Vec<f32>,
    humidity: Vec<f32>,
    /// One bool per moisture sample (index-aligned): pump active at that report.
    pump: Vec<bool>,
    dry: Option<f32>,
    wet: Option<f32>,
    node: Option<String>,
}

impl SproutHistory {
    pub fn new(maxlen: usize) -> Self {
        Self {
            maxlen,
            ..Default::default()
        }
    }

    fn push(ring: &mut Vec<f32>, maxlen: usize, value: f32) {
        if ring.len() >= maxlen {
            ring.remove(0);
        }
        ring.push(value);
    }

    /// Fold `(sensor_id, value)` samples into the history. Only a report that
    /// carries a soil-moisture reading owns the history (the ownership rule in
    /// the Python original — otherwise the Cardputer's air-only reports would
    /// adopt the Sprout's chart). Returns true if this report had moisture.
    pub fn fold_samples(&mut self, node: &str, samples: &[(u32, f32)]) -> bool {
        let mut moisture_seen = false;
        let mut moisture_pushes = 0usize;
        let mut air_temp: Option<f32> = None;
        let mut air_humidity: Option<f32> = None;
        let mut pump_active: Option<f32> = None;
        let mut dry: Option<f32> = None;
        let mut wet: Option<f32> = None;

        for (id, value) in samples {
            match id {
                &SENSOR_SOIL_MOISTURE => {
                    Self::push(&mut self.moisture, self.maxlen, *value);
                    moisture_seen = true;
                    moisture_pushes += 1;
                }
                &SENSOR_AIR_TEMP => air_temp = Some(*value),
                &SENSOR_AIR_HUMIDITY => air_humidity = Some(*value),
                &SENSOR_PUMP_ACTIVE => pump_active = Some(*value),
                &SENSOR_PROFILE_DRY => dry = Some(*value),
                &SENSOR_PROFILE_WET => wet = Some(*value),
                _ => {}
            }
        }

        if moisture_seen {
            if let Some(t) = air_temp {
                Self::push(&mut self.temp, self.maxlen, t);
            }
            if let Some(h) = air_humidity {
                Self::push(&mut self.humidity, self.maxlen, h);
            }
            if !node.is_empty() {
                self.node = Some(node.to_owned());
            }
            if let Some(d) = dry {
                self.dry = Some(d);
            }
            if let Some(w) = wet {
                self.wet = Some(w);
            }
            let pump_flag = pump_active.map(|v| v != 0.0).unwrap_or(false);
            for _ in 0..moisture_pushes {
                if self.pump.len() >= self.maxlen {
                    self.pump.remove(0);
                }
                self.pump.push(pump_flag);
            }
        }
        moisture_seen
    }

    /// The `DATA` line for the next ACK reply, or "" while nothing is known.
    pub fn data_line(&self) -> String {
        if self.moisture.is_empty() && self.temp.is_empty() && self.humidity.is_empty() {
            return String::new();
        }
        let node: String = self.node.as_deref().unwrap_or("").chars().take(8).collect();
        let dry = self.dry.map(round_i).unwrap_or(-1);
        let wet = self.wet.map(round_i).unwrap_or(-1);
        let mut tokens = vec![format!("DATA {node} {dry} {wet}")];
        tokens.extend(series_tokens(&self.temp, Some(DATA_AIR_MAX_SAMPLES)));
        tokens.extend(series_tokens(&self.humidity, Some(DATA_AIR_MAX_SAMPLES)));
        tokens.extend(series_tokens(&self.moisture, None));
        let mut mask = 0u64;
        for (i, active) in self.pump.iter().enumerate() {
            if *active {
                mask |= 1u64 << i;
            }
        }
        tokens.push(mask.to_string());
        tokens.join(" ")
    }
}

fn round_i(v: f32) -> i64 {
    v.round() as i64
}

/// `[count, v0, ...]` tokens for a series, newest samples last, capped by limit.
fn series_tokens(values: &[f32], limit: Option<usize>) -> Vec<String> {
    let mut out = Vec::new();
    let slice = match limit {
        Some(limit) => &values[values.len().saturating_sub(limit)..],
        None => values,
    };
    out.push(slice.len().to_string());
    out.extend(slice.iter().map(|v| round_i(*v).to_string()));
    out
}

/// A thread-safe wrapper for shared state behind `Arc<AppState>`.
pub struct SharedSproutHistory(pub Mutex<SproutHistory>);

impl Default for SharedSproutHistory {
    fn default() -> Self {
        Self(Mutex::new(SproutHistory::new(DEFAULT_MAXLEN)))
    }
}

impl SharedSproutHistory {
    pub fn fold_sensor(&self, node_id: &str, readings: impl IntoIterator<Item = (u32, f32)>) {
        let samples: Vec<(u32, f32)> = readings.into_iter().collect();
        self.0.lock().fold_samples(node_id, &samples);
    }

    pub fn data_line(&self) -> String {
        self.0.lock().data_line()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn air_only_report_does_not_own_history() {
        let mut h = SproutHistory::new(DEFAULT_MAXLEN);
        // Cardputer-style air report (temp/humidity, no soil).
        let owned = h.fold_samples("cardputer", &[(3, 24.5), (2, 55.0)]);
        assert!(!owned);
        assert_eq!(h.data_line(), "");
    }

    #[test]
    fn sprout_report_renders_data_line() {
        let mut h = SproutHistory::new(DEFAULT_MAXLEN);
        for v in [30.0, 28.0, 25.0] {
            let owned = h.fold_samples(
                "sprout01",
                &[(4, v), (3, 22.5), (2, 60.0), (7, 1.0), (10, 20.0), (11, 60.0)],
            );
            assert!(owned);
        }
        let line = h.data_line();
        let mut it = line.split_whitespace();
        assert_eq!(it.next(), Some("DATA"));
        assert_eq!(it.next(), Some("sprout01"));
        assert_eq!(it.next(), Some("20")); // dry
        assert_eq!(it.next(), Some("60")); // wet
        // temp series: count=3 + 3 samples (22.5*3 -> rounds to 23)
        let tcount: usize = it.next().unwrap().parse().unwrap();
        assert_eq!(tcount, 3);
        assert_eq!(it.next(), Some("23"));
        // pump mask: all active (pump=1 per sample) -> all bits set in low 3
        assert_eq!(line.split_whitespace().next_back(), Some("7"));
    }

    #[test]
    fn pump_mask_tracks_off_flags() {
        let mut h = SproutHistory::new(DEFAULT_MAXLEN);
        h.fold_samples("n", &[(4, 30.0), (7, 0.0)]);
        h.fold_samples("n", &[(4, 25.0), (7, 1.0)]);
        h.fold_samples("n", &[(4, 20.0)]);
        // bits: sample0 off, sample1 on, sample2 off -> mask 0b010 = 2
        assert_eq!(h.data_line().split_whitespace().next_back(), Some("2"));
    }

    #[test]
    fn ring_caps_at_maxlen() {
        let mut h = SproutHistory::new(2);
        for v in [1.0, 2.0, 3.0] {
            h.fold_samples("n", &[(4, v)]);
        }
        assert_eq!(h.moisture, vec![2.0, 3.0]);
        assert_eq!(h.pump.len(), 2);
    }
}
