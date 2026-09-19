// ============================================================================
// Sprout — indoor prototype HOUSING ASSEMBLY (box + lid + positioned internals)
// ----------------------------------------------------------------------------
// include <sprout_enclosure.scad>   (defines box_body(), lid(), PARAMS)
//
// This file models the INTERNALS from their PUBLISHED DIMENSIONS (exact, known):
//   - ATOM DTU LoRaWAN base (A152-EU868)  65.3 x 28.7 x 24.0   <- NOT in M5_Hardware,
//     so we model it precisely here. This is the structural spine of the stack.
//   - ATOM Lite 24.0 x 24.0 x 9.5   (sits on top of the DTU base, 9-pin socket)
//   - ENV III   ~32 x 24 x 15        (air T/humidity + pressure, on base Port A)
//
// Design note on the vendor STL files (vendor_ms5/): M5Stack's structure STLs do
// NOT match the published product dimensions (measured ~4x too large vs spec; see
// scale_check.scad). They are schematic renderings, not dimensionally-usable
// meshes. So the ACCURATE internals here are simple placeholder boxes keyed to
// real dims; the vendor STLs are available only behind use_vendor_stl=true if you
// decide they're representative enough for you (they are NOT fit-critical).
//
// The Watering Unit U101 physically lives OUTSIDE/adjacent for the indoor rig:
// only its Grove cable + pump leads enter the box through the bottom gland. The
// pump (5 W / ~1 A @ 5 V) must NOT share the node's rail — see README.
// ============================================================================

include <sprout_enclosure.scad>

/* [Internals — published dims; measure & adjust to your calipers] */
dtu_len = 65.3;   dtu_wid = 28.7;   dtu_h   = 24.0;   // Atom DTU LoRaWAN base
atom_x  = 24.0;   atom_y  = 24.0;   atom_z  = 9.5;    // Atom Lite (on top of base)
env_len = 32.0;   env_wid = 24.0;   env_z   = 15.0;   // ENV III (near base corner)
use_vendor_stl = false;   // true = import vendor_ms5/*.stl instead of the boxes

// --- helpers: place parts centered within the inner cavity, resting on floor --
// inner cavity lives in [wall, wall, floor] .. +[stack_len, stack_wid, ...]
function cx(c=0) = wall + (stack_len)/2 + c;   // X center of cavity
function cy(c=0) = wall + (stack_wid)/2 + c;   // Y center of cavity

module dtu_base(){          // ATOM DTU LoRaWAN base — the modeled spine
    tx = cx() - dtu_len/2;
    ty = cy() - dtu_wid/2;
    translate([tx, ty, floor]) color("DarkSlateGray") cube([dtu_len, dtu_wid, dtu_h]);
}

module atom_lite(){         // Atom Lite on top of the DTU base
    tx = cx() - atom_x/2;
    ty = cy() - atom_y/2;
    translate([tx, ty, floor + dtu_h]) color("DodgerBlue") cube([atom_x, atom_y, atom_z]);
}

module env_iii(){           // ENV III on the DTU base, near one end (Port A side)
    tx = wall + (stack_len - dtu_len)/2 + 2;          // offset to the -X end
    ty = cy() - env_wid/2;
    translate([tx, ty, floor + dtu_h]) color("MediumSeaGreen") cube([env_len, env_wid, env_z]);
}

// Vendor STL proxies (unreliable scale — off by default)
module atom_stl(){
    translate([cx()-12, cy()-12, floor+dtu_h])
        color("DodgerBlue",0.6) import("vendor_ms5/normalized/Atom-Lite.stl");
}
module env_stl(){
    translate([wall + (stack_len-dtu_len)/2 + 2, cy()-45, floor+dtu_h])
        color("MediumSeaGreen",0.6) import("vendor_ms5/normalized/Unit_ENV-III.stl");
}

module internals(){
    if (use_vendor_stl) { atom_stl(); env_stl(); }
    else {                              // accurate dims-based placeholders
        dtu_base(); atom_lite(); env_iii();
    }
}

// ===========================================================================
// Render
// ===========================================================================
if (debug_split) {
    translate([0,0,0]) box_body();
    translate([outer_len + 2*lid_overlap + 10, 0, 0]) internals();   // exploded next to box
    translate([2*(outer_len+2*lid_overlap)+10, 0, 0]) lid();
} else {
    color("SteelBlue", 0.55) box_body();
    internals();
    // lid shown lifted so you can see inside
    translate([0,0, outer_h - 0.4]) color("Orange", 0.5) lid();
}
