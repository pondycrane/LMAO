// ============================================================================
// Sprout — Indoor prototype housing (LMAO smart-irrigation node)
// ----------------------------------------------------------------------------
// OpenSCAD parametric box. Edit the values in the PARAMETERS block to match
// YOUR calliper measurements, re-render, slice, print, measure the delta,
// update the param. That is the whole iteration loop for a prototype.
//
// Contains (published M5Stack dims; measure & adjust in PARAMS):
//   - ATOM Lite             24.0 x 24.0 x  9.5 mm
//   - ATOM DTU LoRaWAN base 65.3 x 28.7 x 24.0 mm   (A152-EU868, DIN-rail back)
//     (stacked via 9-pin socket => combined footprint ~65 x 29, ~34 tall)
//   - ENV III on base Port A (SHT30 + QMP6988)      (verify actual size)
//   - Watering Unit U101    192.8 x 26.5 x 33.0 mm  (driver end INSIDE, probe/hose
//     exit through the floor gland -> into soil)
//   - LoRa antenna          RP-SMA duck, 195 mm, exits the side/top
//
// Design choices for an INDOOR bench prototype (see README):
//   - generous clearance + cable room; trim down after you fit it
//   - one-handed service: lid with 4x M3, not glue
//   - USB access slot on the side for mpremote/flash without opening the lid
//   - probe/hose gland + antenna exit on the BOTTOM so water never runs into
//     the electronics (habit for the future outdoor field unit)
// ============================================================================

// ---------------------------------------------------------------------------
// PARAMETERS — tune these
// ---------------------------------------------------------------------------
/* [Fit — measure the real stack and adjust] */
stack_len    = 70;    // X inner length to clear the stack (base is 65.3 -> allow cable tuck)
stack_wid    = 42;    // Y inner width                           (base is 28.7 -> ENV III + cables)
stack_h      = 52;    // Z inner height  (base 24 + Atom 9.5 + headroom + cable bend)

/* [Enclosure structure] */
wall         = 2.4;   // outer wall thickness (2-3 perimeters of 0.4 nozzle)
floor        = 2.8;   // floor thickness (bit thicker so glands thread well)
clearance    = 0.5;   // general fit clearance inside the cavity
lid_thick    = 2.4;
lid_overlap  = 6.0;   // lid skirt that drops over the box walls

/* [Fasteners — 4x M3] */
m3_d         = 3.4;   // tap hole for M3 brass insert (drill driver: ~3.2-3.4)
boss_h       = 6.0;   // screw boss height in the lid
boss_d       = 7.0;   // screw boss outer diameter
screw_inset  = 7.0;   // screw position inset from each box corner

/* [Penetrations] */
usb_w        = 14.0;  // USB-C access slot width  (Atom USB Type-C is at one end)
usb_h        = 8.0;   // slot height (so a cable can plug while lid is on)
usb_offset_z = 8.0;   // slot center height above the floor
usb_on_len   = true;  // slot on the LENGTH (X) side; false = on the WIDTH (Y) side

gland_d      = 12.0;  // probe/hose cable gland bore (bottom) — size to your hose/gland
gland_xoff   = 20.0;  // gland position along X from center
antenna_d    = 10.0;  // antenna bullet/pass-through bore (bottom corner)

/* [Print] */
fn           = 64;    // circle resolution (leave high for clean holes)
debug_split  = false; // true = render box & lid side-by-side for BOM/screenshot

// ---------------------------------------------------------------------------
// Derived
// ---------------------------------------------------------------------------
outer_len = stack_len + 2 * wall;
outer_wid = stack_wid + 2 * wall;
outer_h   = stack_h  + floor;

// ---------------------------------------------------------------------------
// Box body
// ---------------------------------------------------------------------------
module box_body() {
    difference() {
        // solid outer shell (open top)
        difference() {
            cube([outer_len, outer_wid, outer_h], center = false);
            // inner cavity pocket
            translate([wall, wall, floor])
                cube([stack_len, stack_wid, outer_h], center = false);
        }
        // USB service slot (on a side)
        translate([usb_on_len ? (outer_len - wall - usb_w / 2)
                              : (outer_len / 2),
                   usb_on_len ? (outer_wid / 2)
                              : (outer_wid - wall - usb_h / 2),
                   usb_offset_z])
            cube([usb_on_len ? usb_w : usb_h,
                  usb_on_len ? usb_h : usb_w,
                  2 * wall + 1], center = true);
        // bottom gland for the probe/hose + the antenna pass-through
        translate([outer_len / 2 - gland_xoff, outer_wid / 2, -0.01])
            cylinder(d = gland_d,  h = floor + 1, $fn = fn);
        translate([outer_len / 2 + gland_xoff, outer_wid / 2, -0.01])
            cylinder(d = antenna_d, h = floor + 1, $fn = fn);
        // M2 mount point for the Atom (back has an M2 hole) — optional floor boss
        // translate([outer_len/2, outer_wid/2, -0.01])
        //     cylinder(d = 2.5, h = floor + 1, $fn = fn);
    }
}

// ---------------------------------------------------------------------------
// Lid (drops over the box top; 4x M3 screw bosses)
// ---------------------------------------------------------------------------
module lid() {
    difference() {
        union() {
            // main lid plate + drop-down skirt
            cube([outer_len + 2 * lid_overlap, outer_wid + 2 * lid_overlap,
                  lid_thick]);
            translate([lid_overlap, lid_overlap, 0])
                cube([outer_len, outer_wid, lid_thick + lid_overlap]);
            // screw bosses
            for (sx = [screw_inset, outer_len - screw_inset])
                for (sy = [screw_inset, outer_wid - screw_inset])
                    translate([sx + lid_overlap, sy + lid_overlap, 0])
                        cylinder(d = boss_d, h = lid_thick + boss_h, $fn = fn);
        }
        // M3 holes through the bosses
        for (sx = [screw_inset, outer_len - screw_inset])
            for (sy = [screw_inset, outer_wid - screw_inset])
                translate([sx + lid_overlap, sy + lid_overlap, -0.01])
                    cylinder(d = m3_d, h = lid_thick + boss_h + 1, $fn = fn);
    }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
if (debug_split) {
    translate([0, 0, 0]) box_body();
    translate([outer_len + 2 * lid_overlap + 10, 0, 0]) lid();
} else {
    // assembled view (lid on top, see-through by lowering it slightly)
    color("SteelBlue") box_body();
    translate([0, 0, outer_h - 0.4]) color("Orange") lid();
}
