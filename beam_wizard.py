"""
beam_wizard.py
================
Interactive, step-by-step command-line wizard for beam_multi_span.py.

    1. Project information
    2. Number of spans, lengths, and end support conditions (Pin / Cantilever)
    3. Slab panel properties
    4. Design criteria & load selection (Finishes / Partitions / Live loads)
    5. Slab loading calculation (automatic)
    6. Wall loading (direct wall on beam) + partition toggle recap
    7. Load distribution to spans (which panels touch which span; critical
       panel identified automatically where more than one touches a side)
    8. Point loads & self-weight per span
    9. Support reactions (statics, honouring end conditions)
    10. Tabulated output
    11. Graphical output (PNG diagram)
    12. PDF report

Run:
    python beam_wizard.py
"""

from beam_multi_span import (
    ProjectInfo, DesignCriteria, SlabPanel, WallLoad, PanelContribution, PointLoad,
    Span, BeamSystem, FINISHES_OPTIONS, LIVE_LOAD_OPTIONS, bs8110_classify_panel,
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
            return options[raw][1]
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
        firm_name=ask_str("  Firm name", default="Horicon Engineering Solutions"),
        address=ask_str("  Address", default=""),
        job_no=ask_str("  Job No."),
        calc_sheet_no=ask_str("  Calculation Sheet No."),
        designer=ask_str("  Designer/Engineer"),
        date=ask_str("  Date"),
        revision=ask_str("  Revision", default="A"),
        element=ask_str("  Element (e.g. 'Second Floor Beam SF9')"),
        beam_type=ask_str("  Beam type", default="Simply Supported"),
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
    print("\nSTEP 3 — Slab panel properties")
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
        lx = ask_float("    lx — load width feeding the beam (m)", default=2)

        # ---------------- STEP 4: design criteria & load selection ----------------
        finishes = ask_choice("    Finishes:", FINISHES_OPTIONS, default_key="2")
        live = ask_choice("    Live load:", LIVE_LOAD_OPTIONS, default_key="2")
        has_partition = ask_yesno("    Partition wall on this panel?", "n")
        plen = pthk = pht = 0.0
        if has_partition:
            plen = ask_float("      Partition length (m)")
            pthk = ask_float("      Partition thickness (m)", default=0.2)
            pht = ask_float("      Partition height (m)", default=2.7)

        print("    Panel type:")
        print("      1. Solid slab, two-way spanning (uses BS 8110-1:1997 Table 3.15)")
        print("      2. Ribbed slab (fixed distribution factor = 0.5)")
        print("      3. Cantilever slab (fixed distribution factor = 1.0)")
        slab_type_choice = ask_str("    Choose", default="1")
        slab_type = {"1": "solid", "2": "ribbed", "3": "cantilever"}.get(slab_type_choice, "solid")

        edge_continuous = {"top": True, "bottom": True, "left": True, "right": True}
        if slab_type == "solid":
            print(f"\n    Mark each edge of panel '{name}' as CONTINUOUS (built in / carries over an "
                  f"adjacent support) or DISCONTINUOUS (simply supported / a free edge).")
            print(f"      ly = {ly} m  -> TOP and BOTTOM are the 'long' edges")
            print(f"      lx = {lx} m  -> LEFT and RIGHT are the 'short' edges")
            for edge_key, edge_label in [("top", "TOP    (long edge, length = ly)"),
                                          ("bottom", "BOTTOM (long edge, length = ly)"),
                                          ("left", "LEFT   (short edge, length = lx)"),
                                          ("right", "RIGHT  (short edge, length = lx)")]:
                edge_continuous[edge_key] = ask_yesno(f"      {edge_label} continuous?", "y")

            def mark(v):
                return "====" if v else "----"
            print(f"\n      {mark(edge_continuous['top'])}====")
            print(f"      {'|' if edge_continuous['left'] else '.'}        "
                  f"{'|' if edge_continuous['right'] else '.'}")
            print(f"      {mark(edge_continuous['bottom'])}====")
            panel_type_name = bs8110_classify_panel(edge_continuous)
            print(f"      -> BS 8110 panel type: {panel_type_name}\n")

        panels[name] = SlabPanel(name, thickness, ly, lx, finishes, live,
                                  has_partition, plen, pthk, pht, slab_type, edge_continuous)

    # ---------------- STEP 5: slab loading (auto) ----------------
    print("\nSTEP 5 — Slab loading calculation")
    for i, p in panels.items():
        print(f"  Panel {i}: Gk = {p.dead_kNm2(dc):.3f} kN/m²   Qk = {p.live_kNm2_factored(dc):.3f} kN/m²")

    # ---------------- STEP 6: wall loading ----------------
    print("\nSTEP 6 — Wall loading")
    wall_defined = ask_yesno("Is there direct wall loading on the beam anywhere?", "n")
    if wall_defined:
        wt = ask_float("  Wall thickness (m)", default=0.2)
        wh = ask_float("  Wall height (m)", default=2.7)
        wall = WallLoad(wt, wh)
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

        print("STEP 7 — Load distribution: which panels touch this span?")
        print(f"  Available panels: {', '.join(panels.keys())}")
        n_contrib = ask_int("  How many panel contributions on this span?", default=0)
        contributions = []
        for j in range(n_contrib):
            while True:
                pid = ask_str(f"    Contribution {j+1}: panel name", default=next(iter(panels)))
                if pid in panels:
                    break
                print(f"    '{pid}' isn't a defined panel — choose from: {', '.join(panels.keys())}")
            pos = ask_str(f"    Contribution {j+1}: position (e.g. Left/Top or Right/Bottom)",
                          default="Left/Top")
            panel = panels[pid]
            if panel.slab_type == "solid":
                while True:
                    edge = ask_str(f"    Contribution {j+1}: which EDGE of panel '{pid}' bears onto "
                                    f"this beam? (top/bottom/left/right)", default="left")
                    edge = edge.strip().lower()
                    if edge in ("top", "bottom", "left", "right"):
                        break
                    print("    Please enter one of: top, bottom, left, right")
            else:
                edge = "left"  # not used for ribbed/cantilever (fixed factor), but a value is required
            contributions.append(PanelContribution(pid, pos, edge))
            factor = panel.distribution_factor(edge)
            print(f"    -> distribution factor = {factor:.3f}"
                  + ("" if panel.slab_type == "solid" else f"  ({panel.slab_type} slab, fixed)"))

        wall_here = ask_yesno("  Is the wall present on THIS span?", "n") if wall_defined else False

        print("STEP 8 — Point loads and self-weight on this span")
        self_weight = ask_float("  Element self-weight (kN/m) — added automatically to Gk", default=0)
        n_pts = ask_int("  How many point loads on this span?", default=0)
        point_loads = []
        for j in range(n_pts):
            label = ask_str(f"    Point load {j+1} label", default=f"P{j+1}")
            d = ask_float(f"    {label}: dead load Ngk (kN)", default=0)
            l = ask_float(f"    {label}: live load Nqk (kN)", default=0)
            x = ask_float(f"    {label}: position from left support (m)", default=0)
            point_loads.append(PointLoad(label, d, l, x))

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
    print("\nSTEP 9/10 — Results")
    print(beam.tabulate())

    # ---------------- STEP 11: diagram ----------------
    diagram_path = beam.plot("beam_diagram.svg")
    print(f"\nSTEP 11 — Diagram saved to: {diagram_path} (vector SVG — open in a browser or vector editor)")

    # ---------------- STEP 12: PDF report ----------------
    make_pdf = ask_yesno("\nSTEP 12 — Generate a PDF report?", "y")
    if make_pdf:
        pdf_path = beam.generate_pdf("beam_report.pdf")
        print(f"  PDF report saved to: {pdf_path}")

    return beam


if __name__ == "__main__":
    run_wizard()
