#pragma once

// Sprout node build configuration (mode + plant), baked at flashing time.
//
// One client binary serves two device classes, selected when the firmware is
// built/flashed (see build.sh / flash.sh, which regenerate THIS header in the
// staged ESP-IDF tree from their --mode / --plant arguments):
//
//   * sprout (default): Atom Lite + DTU LoRaWAN base + ENV III.  Full mesh
//     client: reads soil moisture + air T/RH, runs the control engine, and
//     uploads a SensorReport bundle to the LMAO server over LoRa every 5 min.
//   * sprout-lite: Atom Lite + Watering Unit only.  No LoRa module (no DTU),
//     no ENV III: it only reads soil moisture and water-drives the pump when
//     the soil is too dry.  No temp/humidity sensing, no data upload.
//
// The header in the repo is the sprout build; build.sh overwrites the staged
// copy for any other configuration, so flash-time arguments are authoritative.

#ifndef SPROUT_LITE
#define SPROUT_LITE 0          // 0 = full sprout (radio + ENV III), 1 = sprout lite
#endif

#ifndef SPROUT_PLANT
// The plant profile used on this device.  Can be any name in the control
// engine's profile table (control.cpp) — the flash-time plant becomes the
// device's profile unless a full-sprout node carries a newer profile in NVS.
#define SPROUT_PLANT "kale"
#endif
