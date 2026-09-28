//! T9 power/duty-cycle polish: deep sleep between link windows, watchdog,
//! heap-fragmentation guard.

use leaf_power::{HeapGuard, SleepPolicy, Watchdog};
use leaf_rns::{LinkWindowConfig, LinkWindowScheduler};

// window 400 ms / gap 1000 ms → period 1400 ms, duty ~0.286
const POLICY: SleepPolicy = SleepPolicy::new(400, 1000, 6000);

#[test]
fn sleep_zero_inside_open_window() {
    // t=0..400 is the open window.
    assert_eq!(POLICY.sleep_until_next_window(0), 0);
    assert_eq!(POLICY.sleep_until_next_window(399), 0);
    assert_eq!(POLICY.sleep_until_next_window(1400), 0); // next window reopens
}

#[test]
fn sleep_until_next_window_in_gap() {
    // gap = [400, 1400); window reopens at 1400.
    assert_eq!(POLICY.sleep_until_next_window(400), 1000);
    assert_eq!(POLICY.sleep_until_next_window(700), 700);
    assert_eq!(POLICY.sleep_until_next_window(1399), 1);
}

#[test]
fn sleep_capped_by_max() {
    // With a tiny cap the leaf wakes early and often (keepalive bound).
    let capped = SleepPolicy::new(400, 1000, 300);
    for t in [400u64, 700, 1399, 1800, 3000] {
        assert!(capped.sleep_until_next_window(t) <= 300, "cap violated at t={t}");
    }
}

#[test]
fn composes_with_leaf_rns_scheduler() {
    let cfg = LinkWindowConfig::new(400, 1000);
    let mut sched = LinkWindowScheduler::new(cfg);
    sched.mark_link_established(0);

    // The policy agrees with the scheduler's period/duty.
    assert_eq!(POLICY.duty_cycle(), cfg.rx_duty_cycle());

    // For points inside the gap, sleeping until the policy's next window lands
    // the leaf in an open scheduler window.
    for t in [400u64, 700, 1399, 3200u64] {
        let sleep = POLICY.sleep_until_next_window(t);
        let wake = t + sleep;
        assert_eq!(sleep > 0, !sched.is_rx_awake(t as u32), "awake in gap? t={t}");
        assert!(sched.is_rx_awake(wake as u32), "wake must land in a window (t={t}, wake={wake})");
    }
}

#[test]
fn watchdog_fires_after_threshold() {
    let mut wd = Watchdog::new(5000);
    wd.kick(1000);
    assert!(!wd.wedged(5999)); // 4999ms idle — not yet
    assert!(wd.wedged(6001)); // 5001ms idle — wedged
    wd.kick(6001);
    assert!(!wd.wedged(6101)); // kicked — breathing again
}

#[test]
fn heap_guard_flags_fragmentation() {
    // Contiguous: fragmentation 0.
    assert_eq!(HeapGuard::fragmentation(100_000, 100_000), 0.0);
    // Fully fragmented: largest block is a sliver.
    let frag = HeapGuard::fragmentation(100_000, 10_000);
    assert!((frag - 0.9).abs() < 1e-4, "frag={frag}");
    // Empty heap → maximally at risk.
    assert_eq!(HeapGuard::fragmentation(0, 0), 1.0);

    let guard = HeapGuard::new(0.8);
    assert!(!guard.at_risk(100_000, 50_000), "frag 0.5 < 0.8");
    assert!(guard.at_risk(100_000, 5_000), "frag 0.95 >= 0.8");
}
