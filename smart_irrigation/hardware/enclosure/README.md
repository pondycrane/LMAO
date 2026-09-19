# Sprout enclosure (indoor prototype)

Parametric 3D-printed housing for the LMAO smart-irrigation node `sprout`
(ATOM Lite + ATOM DTU LoRaWAN base A152-EU868 + ENV III + Watering Unit U101).

## Open

- `sprout_enclosure.scad` — open in **OpenSCAD** (free, open-source, Linux/Win/Mac).
  All dims are named parameters at the top; change a number, re-render.

## Workflow (the whole point of parametric)

1. **Measure the real stack** with calipers (the published dims in the file are
   M5Stack figures — your assembled stack + cable routing will differ).
2. Update `stack_len / stack_wid / stack_h` (+ `clearance`) to YOUR numbers.
3. Render, export STL (F6 then F6/Export), slice at **0.2 mm / 2–3 perimeters /** 20% infill, print in **PLA** (indoor).
4. Fit-test: pop the stack in, check USB access + cable runs, **measure the delta**, edit the param, reprint. Iterate.
5. `debug_split = true` renders box and lid side-by-side (useful for a BOM shot or per-part export).

Slicer sanity: print the **box on its back/side or with `wall` floor oriented
so the floor glands print cleanly** — or enable a brim. The bottom glands are
holes in the floor; a 0.4 mm nozzle cleanly prints the 12 mm bore.

## What to lock before you finalize / go outdoor

- **Indoor now**: no sealing needed; keep the lid screw-mounted (here) not
  glued, because you'll be opening it weekly for `mpremote`.
- **Pump (U101)**: 5 W / ~1 A @ 5 V — run it from a **separate supply**, not the
  node's 5 V rail. Fit the 10 kΩ pull-down on PUMP_EN *physically at the unit*,
  inside this box where the hose exits (see `docs/hardware-verification.md` §5).
  Never connect the pump until the default-OFF `boot.py` + pull-down exist.
- **ENV III Grove plug is contact-sensitive** — the USB slot only gets you to one
  side; consider a strain relief on the Grove/PORT-A cable so tug doesn't reseat it.
- **Antenna clear**: keep the 868 MHz duck away from the pump DC run and metal.
- **Future field unit**: white/UV filament, an outer shade canopy with an air gap,
  drip lip, and IP-scale sealing around the glands — out of scope for this prototype.

## M5Stack official 3D models — reality check

Yes, M5Stack publishes structure files in
[`github.com/m5stack/M5_Hardware`](https://github.com/m5stack/M5_Hardware),
`Products/<SKU>/Structures/*.stl`. I downloaded the three that exist for Sprout:

- `C008_Atom-Lite` ✅, `U101_Unit_Watering` ✅, `U001-C_Unit_ENV-III` ✅
- **`A152` Atom DTU LoRaWAN base is NOT published** — so the structural spine of
  the stack is modeled from its published dims (65.3×28.7×24) in `sprout_assembly.scad`.

**Warning — they are schematic, not dimensional.** Measured STL bounds vs spec:
Atom-Lite scans ≈95.6×27.6×12 mm but its spec is **24×24×9.5**; ENV-III ≈92×91×13;
Watering ≈137×113×38. `scale_check.scad` renders each next to a true 24 mm cube so
you can see the mismatch. **Do not design tight cavities from these STLs** — use the
published dimension numbers (as `sprout_assembly.scad` does) + your calipers. The
vendor STLs are retained in `vendor_ms5/` behind `use_vendor_stl=true` (off
by default) if you later decide they're representative enough for visual reference.
STEP files (precise solids) would be the gold standard — no central set exists; ask
M5Stack for a STEP of the A152 base if you need true board geometry.

## Files

```
sprout_enclosure.scad   # parametric housing (box + lid), print-ready STL exported
sprout_assembly.scad    # box + lid + INTERNALS modeled from published dims (DTU base included)
sprout_assembly.stl     # assembled STL (shows cavity vs parts) — reference, not for slicing the box
scale_check.scad        # proves vendor STLs ≠ published dims (see below)
vendor_ms5/             # M5Stack structure STLs (Atom-Lite, ENV-III, Watering) + normalized/
```

