"""
beam_wizard.py
================
Interactive, step-by-step command-line wizard for beam_multi_span.py.

    1. Project information
    2. Number of spans, lengths, and end support conditions (Pin / Cantilever)
    3. Slab panels & design criteria (finishes, partitions, live loads)
    4. Slab loading calculation (automatic)
    5. Wall loading (floor height - beam depth)
    6. Load distribution to spans (critical panel identified automatically)
    7. Point loads (with optional element self-weight) & self-weight per span
    8. Results (reactions + tabulated output)
    9. Graphical output (SVG diagram)
    10. PDF calculation sheet

Run:
    python beam_wizard.py
"""

from beam_multi_span import (
    ProjectInfo, DesignCriteria, SlabPanel, WallLoad, PanelContribution, PointLoad,
    Span, BeamSystem, FINISHES_OPTIONS, LIVE_LOAD_OPTIONS,
)


# ---------------------------------------------------------------- helpers
def ask_float(prompt, default=None):
    while True:
        raw = input(f"{prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
        if raw == "" and default is not None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            print("  Please enter a number.")


def ask_int(prompt, default=None):
    while True:
        raw = input(f"{prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
        if raw == "" and default is not None:
            return int(default)
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def ask_yesno(prompt, default="n"):
    raw = input(f"{prompt} (y/n) [{default}]: ").strip().lower()
    if raw == "":
        raw = default
    return raw.startswith("y")


def ask_choice(prompt, options: dict, default_key=None):
    print(prompt)
    for k, (label, val) in options.items():
        print(f"    {k}. {label}  ({val})")
    while True:
        raw = input(f"  Choose" + (f" [{default_key}]" if default_key else "") + ": ").strip()
        if raw == "" and default_key:
            raw = default_key
        if raw in options:
            return options[raw]
        print("  Invalid option, try again.")


def ask_condition(prompt, default="Pin"):
    while True:
        raw = input(f"{prompt} (Pin/Cantilever) [{default}]: ").strip().title()
        if raw == "":
            return default
        if raw in ("Pin", "Cantilever"):
            return raw
        print("  Please enter 'Pin' or 'Cantilever'.")


def ask_str(prompt, default=None):
    raw = input(f"{prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    return raw if raw else (default or "")


# ---------------------------------------------------------------- wizard
def run_wizard():
    print("=" * 72)
    print("BEAM LOAD ANALYSIS — INTERACTIVE WIZARD")
    print("=" * 72)

    # ---------------- STEP 1: project information ----------------
    print("\nSTEP 1 — Project information")
    project = ProjectInfo(
        firm_name=ask_str("  Firm name", default=ProjectInfo().firm_name),
        address=ask_str("  Address", default=ProjectInfo().address),
        tel=ask_str("  Tel", default=ProjectInfo().tel),
        email=ask_str("  Email", default=ProjectInfo().email),
        project_title=ask_str("  Project title"),
        checked_by=ask_str("  Checked by"),
        job_no=ask_str("  Job No."),
        calc_sheet_no=ask_str("  Calculation Sheet No."),
        designer=ask_str("  Designer/Engineer"),
        date=ask_str("  Date"),
        revision=ask_str("  Revision", default="A"),
        element=ask_str("  Element (e.g. 'Second Floor Beam SF9')"),
        beam_type=ask_str("  Beam type (blank = automatic)", default=""),
        location=ask_str("  Location (e.g. 'Along Grid 3/A-C')"),
        material=ask_str("  Material / beam ID"),
    )

    dc = DesignCriteria()

    # ---------------- STEP 2: spans, lengths, end conditions ----------------
    print("\nSTEP 2 — Span configuration")
    n_spans = ask_int("Number of spans", default=1)
    span_lengths = [ask_float(f"  Span {i+1} length (m)") for i in range(n_spans)]

    print("\nEnd support conditions (interior/shared supports are always Pin & continuous)")
    start_cond = ask_condition("  Condition at the FIRST support (start of Span 1)")
    end_cond = ask_condition("  Condition at the LAST support (end of last span)")

    # ---------------- STEP 3: slab panels ----------------
    print("\nSTEP 3 — Slab panels & design criteria")
    n_panels = ask_int("How many slab panels are there in total?", default=1)
    panels = {}
    used_names = set()
    for idx in range(n_panels):
        default_name = f"P{idx+1}"
        while True:
            name = ask_str(f"\n  Panel {idx+1} — name/ID (e.g. 'Kitchen', 'RoofSlab', 'P{idx+1}')",
                            default=default_name)
            if name not in used_names:
                used_names.add(name)
                break
            print(f"  '{name}' is already used for another panel — choose a different name.")
        thickness = ask_float("    Thickness (mm)", default=150)
        ly = ask_float("    ly — long dimension (m)", default=5)
        lx = ask_float("    lx — short dimension / load width feeding the beam (m)", default=2)
        if lx > ly:
            ly, lx = lx, ly
            print(f"    (ly must be the longer dimension — swapped: ly={ly} m, lx={lx} m)")
        ratio = ly / lx if lx > 0 else float("inf")
        spanning = "Two-way" if ratio <= 2.0 else "One-way"
        print(f"    ly/lx = {ratio:.2f}  ->  {'<= 2.0' if spanning=='Two-way' else '> 2.0'}"
              f"  ->  {spanning} spanning slab")

        # ---------------- STEP 4: design criteria & load selection ----------------
        finishes_label, finishes = ask_choice("    Finishes:", FINISHES_OPTIONS, default_key="2")
        live_label, live = ask_choice("    Live load:", LIVE_LOAD_OPTIONS, default_key="2")
        has_partition = ask_yesno("    Partition wall on this panel?", "n")
        plen = pthk = pht = pfl = pdp = 0.0
        if has_partition:
            plen = ask_float("      Partition length (m)")
            pthk = ask_float("      Partition thickness (m)", default=0.2)
            pfl = ask_float("      Floor height (m)", default=3.6)
            pdp = ask_float("      Slab depth (m)", default=thickness / 1000.0)
            pht = max(0.0, pfl - pdp)
            print(f"      Partition height = floor height - slab depth = {pfl:g} - {pdp:g} = {pht:.2f} m")

        print("    Panel type:")
        print("      1. Solid slab (uses BS 8110-1:1997 Table 3.15 — two-way as normal,")
        print("         one-way takes beta_vx at ly/lx = 2.0)")
        print("      2. Ribbed slab (factor 0.5; double arrow = rib span direction)")
        print("      3. Cantilever slab (single arrow = loading direction; factor 1.0, or 0.5 if the")
        print("         beam runs along the loading direction)")
        slab_type_choice = ask_str("    Choose", default="1")
        slab_type = {"1": "solid", "2": "ribbed", "3": "cantilever"}.get(slab_type_choice, "solid")

        edge_continuous = {"top": True, "bottom": True, "left": True, "right": True}

        # Orientation for ALL panel types: the reference edge is the edge of the panel that
        # sits on the beam. It is applied automatically to the panel contributions in Step 7.
        print(f"\n    Panel edge on this beam — orientation")
        print(f"      Pick the edge of panel '{name}' that sits on the beam as the reference edge,")
        print(f"      and tell me whether THAT edge (and its opposite) is the ly side or the lx side.")
        while True:
            primary_edge = ask_str("      Reference edge (top/bottom/left/right)", default="left").strip().lower()
            if primary_edge in ("top", "bottom", "left", "right"):
                break
            print("      Please enter one of: top, bottom, left, right")
        primary_edge_is_ly = ask_yesno(f"      Is the '{primary_edge}' edge the ly (long, {ly} m) side?"
                                        f" ('n' means it's the lx ({lx} m) side)", "n")

        opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}[primary_edge]
        ly_pair = {primary_edge, opposite} if primary_edge_is_ly else \
                  ({"top", "bottom", "left", "right"} - {primary_edge, opposite})

        arrow_perpendicular, arrow_flip, rib_lx = True, False, 0.525
        if slab_type in ("ribbed", "cantilever"):
            what = "ribs span (double arrow <-->)" if slab_type == "ribbed" else "cantilever loads (single arrow -->)"
            arrow_perpendicular = ask_yesno(
                f"      Does the direction the {what} run PERPENDICULAR to the '{primary_edge}' reference edge?", "y")
            if not arrow_perpendicular:
                if slab_type == "ribbed":
                    rib_lx = ask_float("      lx — rib load width (m)", default=0.525)
                else:
                    arrow_flip = ask_yesno("      Flip the arrow direction (sketch only — no effect on load)?", "n")

        if slab_type == "solid":
            print(f"\n    Mark each edge of panel '{name}' as CONTINUOUS (built in / carries over an "
                  f"adjacent support) or DISCONTINUOUS (simply supported / a free edge).")
            print(f"      ly = {ly} m  -> {' and '.join(sorted(ly_pair))} edge(s)")
            print(f"      lx = {lx} m  -> {' and '.join(sorted({'top','bottom','left','right'} - ly_pair))} edge(s)")
            for edge_key in ("top", "bottom", "left", "right"):
                dim_label = "ly" if edge_key in ly_pair else "lx"
                dim_value = ly if dim_label == "ly" else lx
                edge_continuous[edge_key] = ask_yesno(
                    f"      {edge_key.upper():<6} ({dim_label} edge, length = {dim_value} m) continuous?", "y")

            # oriented sketch: draw ly on whichever pair the user actually specified
            def mark(v):
                return "====" if v else "----"
            top_lbl = "ly" if "top" in ly_pair else "lx"
            left_is_ly = "left" in ly_pair
            print(f"\n      [{top_lbl}] {mark(edge_continuous['top'])}====")
            print(f"      {'|' if edge_continuous['left'] else '.'}"
                  f"{'(ly)' if left_is_ly else '(lx)':<8}"
                  f"{'|' if edge_continuous['right'] else '.'}")
            print(f"      {mark(edge_continuous['bottom'])}====")

            panels_tmp = SlabPanel(name, thickness, ly, lx, finishes, live, has_partition, plen, pthk, pht,
                                    slab_type, edge_continuous, primary_edge, primary_edge_is_ly)
            print(f"      -> BS 8110 panel type: {panels_tmp.panel_type_bs8110()}\n")

        panels[name] = SlabPanel(name, thickness, ly, lx, finishes, live,
                                  has_partition, plen, pthk, pht, slab_type, edge_continuous,
                                  primary_edge, primary_edge_is_ly,
                                  arrow_perpendicular=arrow_perpendicular, arrow_flip=arrow_flip,
                                  rib_lx_m=rib_lx, partition_floor_ht_m=pfl, partition_depth_m=pdp,
                                  finishes_label=finishes_label, live_label=live_label)
        if slab_type != "solid":
            pn = panels[name]
            print(f"      -> factor = {pn.distribution_factor(primary_edge):.2f}, "
                  f"load width lx = {pn.load_width_m():.3f} m\n")

    # ---------------- STEP 5: slab loading (auto) ----------------
    print("\nSTEP 4 — Slab loading calculation")
    for i, p in panels.items():
        print(f"  Panel {i}: Gk = {p.dead_kNm2(dc):.3f} kN/m²   Qk = {p.live_kNm2_factored(dc):.3f} kN/m²")

    # ---------------- STEP 6: wall loading ----------------
    print("\nSTEP 5 — Wall loading")
    wall_defined = ask_yesno("Is there direct wall loading on the beam anywhere?", "n")
    if wall_defined:
        wt = ask_float("  Wall thickness (m)", default=0.2)
        wfl = ask_float("  Floor height (m)", default=3.6)
        wdp = ask_float("  Beam depth (m)", default=0.45)
        wall = WallLoad(wt, wfl, wdp)
        print(f"  Wall height = floor height - beam depth = {wfl:g} - {wdp:g} = {wall.height_m:.2f} m")
        print(f"  Factored wall dead load = {wall.factored_dead_kNm(dc):.3f} kN/m (applied where present)")
    else:
        wall = WallLoad(0, 0)
    n_partitioned = sum(1 for p in panels.values() if p.has_partition)
    if n_partitioned:
        print(f"  Note: {n_partitioned} panel(s) also carry partition loads (set in Step 3/4) "
              f"— these are already included in the panel Gk above.")

    beam = BeamSystem(dc, panels, wall, project)

    # ---------------- STEP 7/8: build each span ----------------
    for i in range(n_spans):
        print(f"\n--- Span {i+1} (length {span_lengths[i]} m) ---")

        print("STEP 6 — Load distribution: which panels touch this span?")
        print(f"  Available panels: {', '.join(panels.keys())}")
        n_contrib = ask_int("  How many panel contributions on this span?", default=0)
        contributions = []
        for j in range(n_contrib):
            while True:
                pid = ask_str(f"    Contribution {j+1}: panel name", default=next(iter(panels)))
                if pid in panels:
                    break
                print(f"    '{pid}' isn't a defined panel — choose from: {', '.join(panels.keys())}")
            panel = panels[pid]
            edge = panel.primary_edge
            print(f"    Panel edge on this beam: '{edge}' (from the panel's reference edge — not editable here)")
            contributions.append(PanelContribution(pid, edge))
            factor = panel.distribution_factor(edge)
            print(f"    -> distribution factor = {factor:.3f}"
                  + f"   Lx = {panel.load_width_m():.3f} m")

        wall_here = ask_yesno("  Is the wall present on THIS span?", "n") if wall_defined else False

        print("STEP 7 — Point loads and self-weight on this span")
        self_weight = ask_float("  Element self-weight (kN/m) — added automatically to Gk", default=0)
        n_pts = ask_int("  How many point loads on this span?", default=0)
        point_loads = []
        for j in range(n_pts):
            label = ask_str(f"    Point load {j+1} label", default=f"P{j+1}")
            d = ask_float(f"    {label}: dead load Ngk (kN)", default=0)
            l = ask_float(f"    {label}: live load Nqk (kN)", default=0)
            x = ask_float(f"    {label}: position from left support (m)", default=0)
            eb = eh = el = 0.0
            if ask_yesno(f"    Add self-weight of the element (beam/column) to {label}?", "n"):
                eb = ask_float("      Element width b (mm)", default=200) / 1000.0
                eh = ask_float("      Element depth h (mm)", default=450) / 1000.0
                el = ask_float("      Element length (m)", default=3)
            pl = PointLoad(label, d, l, x, eb, eh, el, dc.concrete_density, dc.factor_selfweight_partition)
            if pl.self_weight_kN:
                print(f"      Self-weight = {dc.factor_selfweight_partition:g} x {eb:g} x {eh:g} x {el:g} x "
                      f"{dc.concrete_density:g} = {pl.self_weight_kN:.2f} kN  ->  total Ngk = {pl.total_dead_kN:.2f} kN")
            point_loads.append(pl)

        left_cond = start_cond if i == 0 else "Pin"
        right_cond = end_cond if i == n_spans - 1 else "Pin"

        labels = [chr(65 + k) for k in range(n_spans + 1)]
        span = Span(i + 1, span_lengths[i], labels[i], labels[i + 1],
                    contributions=contributions, wall_present=wall_here,
                    point_loads=point_loads, self_weight_kNm=self_weight,
                    left_condition=left_cond, right_condition=right_cond)
        beam.add_span(span)

        # report the critical-panel selection immediately, as requested
        beam.compute_all()
        for pos, g in span.governing.items():
            others = [c for c in g['all_candidates'] if c[0] != g['panel_id']]
            if others:
                print(f"    -> {pos}: Panel {g['panel_id']} governs over "
                      f"panel(s) {', '.join(str(o[0]) for o in others)} "
                      f"(Gk={g['dead']:.3f} kN/m)")

    # ---------------- STEP 9/10: compute + tabulate ----------------
    beam.compute_all()
    print("\nSTEP 8 — Results")
    print(beam.tabulate())

    # ---------------- STEP 11: diagram ----------------
    diagram_path = beam.plot("beam_diagram.svg")
    print(f"\nSTEP 9 — Diagram saved to: {diagram_path} (vector SVG — open in a browser or vector editor)")

    # ---------------- STEP 12: PDF report ----------------
    make_pdf = ask_yesno("\nSTEP 10 — Generate a PDF report?", "y")
    if make_pdf:
        pdf_path = beam.generate_pdf("beam_report.pdf")
        print(f"  PDF report saved to: {pdf_path}")

    return beam


if __name__ == "__main__":
    run_wizard()
