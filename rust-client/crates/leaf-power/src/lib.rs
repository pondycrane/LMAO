//! T9 — leaf power / duty-cycle polish (design §5 `leaf-power`, stretch).
//!
//! Three `no_std` + `alloc`-free health/power guards for a duty-cycled LoRa
//! leaf:
//!
//! 1. **`SleepPolicy`** — deep sleep **between link windows**: given the
//!    gateway↔leaf rendezvous schedule (the `leaf-rns` `LinkWindowConfig`:
//!    `window_ms` awake, `gap_ms` dormant), compute how long the leaf may
//!    deep-sleep before the next window opens — bounded by `max_sleep_ms` so a
//!    very sparse schedule cannot lose the link (T4 sustain), and `0` while a
//!    window is open.
//! 2. **`Watchdog`** — detect a wedged live-lock (no link/radio activity for
//!    `threshold_ms`, cf. `cardputer_client` #74's stuck-beacon device) and
//!    signal recovery.
//! 3. **`HeapGuard`** — heap-fragmentation guard (cf. #74 robustness): given a
//!    heap report `(total_free, largest_free_block)` compute the fragmentation
//!    ratio and flag when framing/transfers are at risk.

#![no_std]

/// Deep-sleep scheduling between two link rendezvous windows.
///
/// Windows are `[k·T, k·T + window_ms)` for integer `k`, T = window+gap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SleepPolicy {
    window_ms: u64,
    period_ms: u64,
    max_sleep_ms: u64,
}

impl SleepPolicy {
    /// `window_ms`/`gap_ms` mirror `leaf_rns::LinkWindowConfig`;
    /// `max_sleep_ms` caps a single deep-sleep so the link is sustained even
    /// across a sparse schedule.
    pub const fn new(window_ms: u64, gap_ms: u64, max_sleep_ms: u64) -> Self {
        SleepPolicy {
            window_ms,
            period_ms: window_ms + gap_ms,
            max_sleep_ms,
        }
    }

    /// Milliseconds the leaf may deep-sleep from `now_ms` before the next
    /// window opens. Returns `0` when a window is currently open (stay up).
    /// Capped at `max_sleep_ms` so keepalive/link-sustain can't be starved.
    pub fn sleep_until_next_window(&self, now_ms: u64) -> u64 {
        let offset = now_ms % self.period_ms;
        let sleep = if offset < self.window_ms {
            0
        } else {
            self.period_ms - offset
        };
        sleep.min(self.max_sleep_ms)
    }

    /// Duty cycle of the awake window (fraction of the period spent up).
    pub fn duty_cycle(&self) -> f32 {
        self.window_ms as f32 / self.period_ms.max(1) as f32
    }
}

/// Soft watchdog: a wedged device (no progress for a threshold) is flagged for
/// recovery, e.g. an unresponsive link that has stopped advancing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Watchdog {
    threshold_ms: u64,
    last_activity_ms: u64,
}

impl Watchdog {
    pub const fn new(threshold_ms: u64) -> Self {
        Watchdog {
            threshold_ms,
            last_activity_ms: 0,
        }
    }

    /// Note that the device made forward progress at `now_ms`.
    pub fn kick(&mut self, now_ms: u64) {
        self.last_activity_ms = now_ms;
    }

    /// True when no progress has been observed for longer than the threshold.
    pub fn wedged(&self, now_ms: u64) -> bool {
        now_ms.saturating_sub(self.last_activity_ms) > self.threshold_ms
    }

    /// Seconds the device may sleep without the watchdog firing (keepalive cadence).
    pub fn threshold_ms(&self) -> u64 {
        self.threshold_ms
    }
}

/// Heap-fragmentation guard (robustness against a RAM-starved framing stage).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HeapGuard {
    warn_fragmentation: f32,
}

impl HeapGuard {
    /// `warn_fragmentation` in [0,1]: the fragmentation ratio above which the
    /// guard flags risk.
    pub const fn new(warn_fragmentation: f32) -> Self {
        HeapGuard { warn_fragmentation }
    }

    /// Fragmentation = 1 − (largest_free_block / total_free). 0 = one
    /// contiguous block (healthy); → 1 = fully fragmented.
    pub fn fragmentation(total_free: usize, largest_free_block: usize) -> f32 {
        if total_free == 0 {
            return 1.0;
        }
        let largest = largest_free_block.min(total_free) as f32;
        1.0 - largest / total_free as f32
    }

    /// True when measured fragmentation is at/above the warning threshold.
    pub fn at_risk(&self, total_free: usize, largest_free_block: usize) -> bool {
        Self::fragmentation(total_free, largest_free_block) >= self.warn_fragmentation
    }
}
