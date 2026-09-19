// Scale-check contact sheet: do M5Stack structure STLs match published dims?
// Left: normalized vendor STL.  Right: a true 24.0 mm reference cube (the real
// Atom-Lite body width). If the STL isn't ~same size as the cube, it's not usable
// as a precise cavity reference.

$fn=48;
module ref_cube(){ color("Red") cube([24,24,24]); }

module readout(name, stl){
    translate([0,0,0]) { color("SteelBlue") import(stl); }
    echo(str(name," imported for visual scale check"));
}

// ==== Atom-Lite ====
translate([0,0,0]) readout("Atom-Lite", "vendor_ms5/normalized/Atom-Lite.stl");
translate([130,0,0]) ref_cube();

// ==== ENV-III ====
translate([0,60,0]) readout("ENV-III", "vendor_ms5/normalized/Unit_ENV-III.stl");
translate([130,60,0]) ref_cube();

// ==== Watering unit ====
translate([0,120,0]) { color("SteelBlue") import("vendor_ms5/normalized/Unit_Watering.stl"); }
translate([130,120,0]) ref_cube();
