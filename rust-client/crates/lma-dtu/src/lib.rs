//! LMAO DTU wire protocol (design ticket: "rust on sprout") — the Sprout's
//! RAK3172 DTU (LoRa P2P) UART-AT radio protocol and the no_std SensorReport
//! envelope bytes it sends to the Rust LMAO server.
//!
//! Pure no_std + alloc logic, host-tested; `firmware-sprout` drives
//! [`at_dtu`] over an esp-hal UART and [`envelope`] to encode each sample
//! bundle.

#![no_std]
#![cfg_attr(not(test), forbid(unsafe_code))]

extern crate alloc;

pub mod at_dtu;
pub mod envelope;
