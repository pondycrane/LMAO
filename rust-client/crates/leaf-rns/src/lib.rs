//! LMAO leaf RNS: transport / Link wiring + duty-cycle **link-window
//! scheduling** (design §5 `leaf-rns`, ticket T4).
//!
//! ## Open decision §11 #1 — resolved for T4
//!
//! *How does a link-capable leaf serve under duty-cycle?* A half-duplex,
//! duty-cycled LoRa leaf holding an RNS `Link` must stay awake to sustain it —
//! unlike the old sleep-when-idle leaf. Chosen design (the design doc's
//! recommended option, for power):
//!
//! **Scheduled gateway→leaf link windows with a resource resume queue.**
//! The leaf's radio RX is awake only during periodic link windows; an
//! established `Link` is **held across the dormant gap** (not torn down —
//! sustaining a link is cheaper in protocol state than re-establishing every
//! window), and any frame produced while dormant is **queued and flushed at the
//! next window open** — the seed of the T6 resource resume queue for largely
//! uninterrupted transfers across duty-cycled gaps.
//!
//! This module is the pure, host-testable core: the window gate, the hold
//! (sustain) rule, the resume queue, and the keepalive pacing inside windows.
//! Driving real RNS traffic through it needs rns-core's transport + Link
//! (on-device, pairs with T6) — the RF/framing leg is T3/T4-hardware.

#![no_std]
#![cfg_attr(not(test), forbid(unsafe_code))]

extern crate alloc;

use alloc::{collections::VecDeque, vec, vec::Vec};

/// The leaf's scheduled link-window schedule: radio RX awake for `window_ms`,
/// dormant for `gap_ms`, repeating.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LinkWindowConfig {
    /// ms the radio RX is open (link window) per period.
    pub window_ms: u32,
    /// ms the radio is dormant per period (no RX).
    pub gap_ms: u32,
}

impl LinkWindowConfig {
    pub fn new(window_ms: u32, gap_ms: u32) -> Self {
        Self {
            window_ms,
            gap_ms,
        }
    }

    /// Period = window + gap.
    pub fn period(&self) -> u32 {
        self.window_ms.saturating_add(self.gap_ms)
    }

    /// RX duty cycle (power budget of being link-capable) — the honest cost of
    /// holding a link vs. the old sleep-when-idle leaf.
    pub fn rx_duty_cycle(&self) -> f32 {
        let p = self.period();
        if p == 0 {
            return 0.0;
        }
        self.window_ms as f32 / p as f32
    }
}

/// Duty-cycle link manager for the leaf.
#[derive(Debug)]
pub struct LinkWindowScheduler {
    cfg: LinkWindowConfig,
    /// An established gateway↔leaf link is held across dormant gaps (sustain).
    link_established: bool,
    /// last ms a link frame was sent or received (for keepalive pacing).
    last_link_activity_ms: u32,
    /// Frames produced while dormant, flushed at the next window open.
    resume_queue: VecDeque<Vec<u8>>,
}

impl LinkWindowScheduler {
    pub fn new(cfg: LinkWindowConfig) -> Self {
        Self {
            cfg,
            link_established: false,
            last_link_activity_ms: 0,
            resume_queue: VecDeque::new(),
        }
    }

    /// Is the radio RX open at `now_ms` (within a link window)?
    pub fn is_rx_awake(&self, now_ms: u32) -> bool {
        let p = self.cfg.period();
        if p == 0 {
            return false;
        }
        now_ms % p < self.cfg.window_ms
    }

    /// Mark a link as established (inbound request served or outbound established).
    pub fn mark_link_established(&mut self, now_ms: u32) {
        self.link_established = true;
        self.last_link_activity_ms = now_ms;
    }

    /// A link is held across dormant gaps (never auto-torn-down by duty-cycling).
    pub fn link_established(&self) -> bool {
        self.link_established
    }

    /// Whether the device is currently linked (regardless of RX gate).
    pub fn is_linked(&self) -> bool {
        self.link_established
    }

    /// Record link traffic (keeps the link from going stale for keepalive purposes).
    pub fn note_link_activity(&mut self, now_ms: u32) {
        if self.link_established {
            self.last_link_activity_ms = now_ms;
        }
    }

    /// A keepalive is due when the link is established, the radio is in a (new)
    /// window, and no link traffic has flowed for at least one full period —
    /// so the gateway peer sees the leaf still alive each window.
    pub fn keepalive_due(&self, now_ms: u32) -> bool {
        self.link_established
            && self.is_rx_awake(now_ms)
            && now_ms.wrapping_sub(self.last_link_activity_ms) >= self.cfg.period()
    }

    /// Queue a frame for TX. While dormant it is held in the resume queue and
    /// flushed at the next window open (uninterrupted transfer across gaps).
    pub fn queue_tx(&mut self, frame: Vec<u8>) {
        self.resume_queue.push_back(frame);
    }

    /// Best-effort immediate flush **only if the radio is awake**; while
    /// dormant nothing is emitted (the resume queue holds it until the next
    /// window). Returns the frames to transmit now; clears them from the queue.
    pub fn drain_tx(&mut self, now_ms: u32) -> Vec<Vec<u8>> {
        if !self.is_rx_awake(now_ms) {
            // Dormant: keep the resume queue — do not transmit.
            return Vec::new();
        }
        if !self.link_established {
            // No link — nothing to send on it (leaf is announced-only).
            self.resume_queue.clear();
            return Vec::new();
        }
        let mut out = Vec::with_capacity(self.resume_queue.len());
        while let Some(f) = self.resume_queue.pop_front() {
            out.push(f);
        }
        // Sending traffic keeps the link alive.
        if !out.is_empty() {
            self.last_link_activity_ms = now_ms;
        }
        out
    }

    pub fn queued_len(&self) -> usize {
        self.resume_queue.len()
    }

    pub fn config(&self) -> LinkWindowConfig {
        self.cfg
    }

    /// Close the link (torn down for real — not by duty cycling).
    pub fn close_link(&mut self) {
        self.link_established = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rx_awake_only_inside_window() {
        // 200 ms window, 800 ms gap -> period 1000 ms.
        let s = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
        assert!(s.is_rx_awake(0)); // window open
        assert!(s.is_rx_awake(199)); // still window
        assert!(!s.is_rx_awake(200)); // gap begins
        assert!(!s.is_rx_awake(999)); // still gap
        assert!(s.is_rx_awake(1000)); // next window opens
        assert!(!s.is_rx_awake(1999)); // 1000+999: in the gap, wrapped
        assert!(s.is_rx_awake(2100)); // 2000+100: inside the next window
    }

    #[test]
    fn duty_cycle_reflects_power_cost() {
        let s = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
        assert!((s.config().rx_duty_cycle() - 0.2).abs() < 1e-6);
        // persistent-link alternative = duty cycle 1.0 (the cost we avoid)
        let persistent = LinkWindowScheduler::new(LinkWindowConfig::new(1000, 0));
        assert!((persistent.config().rx_duty_cycle() - 1.0).abs() < 1e-6);
    }

    #[test]
    fn established_link_is_held_across_dormant_gap() {
        let mut s = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
        s.mark_link_established(50);
        assert!(s.is_linked());
        assert!(s.is_rx_awake(50)); // in-window when established
        // advance into a dormant gap: still linked (sustain, not torn down)
        assert!(!s.is_rx_awake(300));
        assert!(s.is_linked(), "link must not be torn down by duty cycling");
        // and held across the boundary into the next window
        assert!(s.is_rx_awake(1000));
        assert!(s.is_linked());
    }

    #[test]
    fn resume_queue_flushes_at_window_open_only() {
        let mut s = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
        s.mark_link_established(0);
        // queue frames while in a window
        s.queue_tx(vec![1]);
        s.queue_tx(vec![2]);
        // still awake -> immediate flush
        let now = s.drain_tx(10);
        assert_eq!(now, vec![vec![1], vec![2]]);
        assert_eq!(s.queued_len(), 0);

        // queue while dormant -> NOT flushed until the window reopens
        s.queue_tx(vec![3]);
        assert!(!s.is_rx_awake(1000 + 500)); // dormant
        assert_eq!(s.drain_tx(1500), Vec::<Vec<u8>>::new(), "must hold in gap");
        assert_eq!(s.queued_len(), 1, "frame stays queued (resume across gap)");
        // next window open -> flushed
        assert!(s.is_rx_awake(2000));
        let now = s.drain_tx(2000);
        assert_eq!(now, vec![vec![3]]);
    }

    #[test]
    fn keepalive_due_only_in_window_after_stale_period() {
        let mut s = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
        s.mark_link_established(0);
        assert!(!s.keepalive_due(10)); // fresh (activity recorded at establish)
        // a full period later, in a window, no traffic -> keepalive due
        assert!(s.keepalive_due(2000));
        // record link activity -> no longer due
        s.note_link_activity(2100);
        assert!(!s.keepalive_due(2100));
        // while dormant it is never due (radio can't TX anyway)
        assert!(!s.keepalive_due(1000 + 500));
    }
}
