"""
Streamlit web app for beam_multi_span.py
------------------------------------------
Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Opens at http://localhost:8501 in your browser.
Put this file in the SAME folder as beam_multi_span.py.
"""

import streamlit as st
from beam_multi_span import (
    ProjectInfo, DesignCriteria, SlabPanel, WallLoad, PanelContribution, PointLoad,
    Span, BeamSystem, FINISHES_OPTIONS, LIVE_LOAD_OPTIONS,
)

st.set_page_config(page_title="Multi-Span Beam Load Analysis", layout="centered")
st.title("Multi-span beam — load analysis")

dc = DesignCriteria()

# ---------------- Step 1: project information ----------------
st.header("1. Project information")
c1, c2 = st.columns(2)
project = ProjectInfo(
    firm_name=c1.text_input("Firm name", value="Horicon Engineering Solutions"),
    job_no=c2.text_input("Job No."),
    designer=c1.text_input("Designer/Engineer"),
    date=c2.text_input("Date"),
    element=c1.text_input("Element (e.g. 'Second Floor Beam SF9')"),
    revision=c2.text_input("Revision", value="A"),
    location=c1.text_input("Location"),
    material=c2.text_input("Material / beam ID"),
)

# ---------------- Step 2: spans ----------------
st.header("2. Spans")
n_spans = st.number_input("Number of spans", min_value=1, value=2, step=1)
lengths = []
ROW_SIZE = 6  # wrap span-length inputs into rows so large counts stay usable
for row_start in range(0, int(n_spans), ROW_SIZE):
    row_indices = range(row_start, min(row_start + ROW_SIZE, int(n_spans)))
    cols = st.columns(len(row_indices))
    for col, i in zip(cols, row_indices):
        with col:
            lengths.append(st.number_input(f"Span {i+1} length (m)", min_value=0.1, value=3.0 + i, step=0.1, key=f"L{i}"))

c1, c2 = st.columns(2)
start_cond = c1.selectbox("Condition at FIRST support (start of Span 1)", ["Pin", "Cantilever"])
end_cond = c2.selectbox("Condition at LAST support (end of last span)", ["Pin", "Cantilever"])
st.caption("Interior/shared supports are always pinned and continuous.")

# ---------------- Step 3 & 4: slab panels ----------------
st.header("3-4. Slab panels & design criteria")
n_panels = st.number_input("Number of slab panels", min_value=1, value=3, step=1)
panels = {}
fin_labels = {k: v[0] for k, v in FINISHES_OPTIONS.items()}
live_labels = {k: v[0] for k, v in LIVE_LOAD_OPTIONS.items()}
used_names = set()
for i in range(1, int(n_panels) + 1):
    default_name = f"P{i}"
    name = st.text_input(f"Panel {i} — name/ID", value=default_name, key=f"pname{i}")
    if name in used_names:
        st.warning(f"'{name}' is already used by another panel — please make it unique.")
    used_names.add(name)
    with st.expander(f"Panel: {name}", expanded=(i <= 2)):
        c1, c2, c3 = st.columns(3)
        t = c1.number_input("Thickness (mm)", value=200, key=f"pt{i}")
        ly = c2.number_input("ly (m)", value=5.0, key=f"ply{i}")
        lx = c3.number_input("lx (m)", value=2.0, key=f"plx{i}")
        c4, c5 = st.columns(2)
        fin_key = c4.selectbox("Finishes", options=list(FINISHES_OPTIONS.keys()),
                                format_func=lambda k: fin_labels[k], key=f"pfin{i}")
        live_key = c5.selectbox("Live load", options=list(LIVE_LOAD_OPTIONS.keys()),
                                 format_func=lambda k: live_labels[k], key=f"pliv{i}")
        has_partition = st.checkbox("Partition wall on this panel?", key=f"ppart{i}")
        plen = pthk = pht = 0.0
        if has_partition:
            c6, c7, c8 = st.columns(3)
            plen = c6.number_input("Partition length (m)", value=5.0, key=f"pplen{i}")
            pthk = c7.number_input("Partition thickness (m)", value=0.2, key=f"ppthk{i}")
            pht = c8.number_input("Partition height (m)", value=2.7, key=f"ppht{i}")
        panels[name] = SlabPanel(name, t, ly, lx, FINISHES_OPTIONS[fin_key][1], LIVE_LOAD_OPTIONS[live_key][1],
                               has_partition, plen, pthk, pht)
        st.caption(f"-> Gk = {panels[name].dead_kNm2(dc):.3f} kN/m²   Qk = {panels[name].live_kNm2_factored(dc):.3f} kN/m²"
                   f"  (Step 5: calculated automatically)")

# ---------------- Step 6: wall ----------------
st.header("6. Wall loading")
wall_defined = st.checkbox("Is there direct wall loading on the beam anywhere?")
if wall_defined:
    c1, c2 = st.columns(2)
    wt = c1.number_input("Wall thickness (m)", value=0.2, step=0.05)
    wh = c2.number_input("Wall height (m)", value=2.7, step=0.1)
    wall = WallLoad(wt, wh)
    st.caption(f"Factored wall dead load = {wall.factored_dead_kNm(dc):.3f} kN/m (applied where marked present)")
else:
    wall = WallLoad(0, 0)

beam = BeamSystem(dc, panels, wall, project)
labels = [chr(65 + i) for i in range(int(n_spans) + 1)]

# ---------------- Step 7 & 8: per-span distribution + point loads ----------------
st.header("7-8. Load distribution, self-weight and point loads per span")
for i in range(int(n_spans)):
    with st.expander(f"Span {i+1}  ({labels[i]} -> {labels[i+1]}, {lengths[i]} m)", expanded=(i == 0)):
        st.subheader("Panel contributions")
        n_c = st.number_input("Number of panel contributions", min_value=0, value=2, step=1, key=f"nc{i}")
        contributions = []
        for j in range(int(n_c)):
            c1, c2, c3 = st.columns(3)
            pid = c1.selectbox("Panel", options=list(panels.keys()), key=f"s{i}c{j}p")
            pos = c2.selectbox("Position", options=["Left/Top", "Right/Bottom"], key=f"s{i}c{j}pos")
            factor = c3.number_input("Distribution factor", value=0.5, step=0.1, key=f"s{i}c{j}f")
            contributions.append(PanelContribution(pid, pos, factor))

        wall_here = False
        if wall_defined:
            wall_here = st.checkbox("Wall present on this span", key=f"s{i}wall")

        self_weight = st.number_input("Element self-weight (kN/m) — added automatically to Gk",
                                       value=0.0, step=0.1, key=f"s{i}sw")

        st.subheader("Point loads")
        n_p = st.number_input("Number of point loads", min_value=0, value=0, step=1, key=f"np{i}")
        point_loads = []
        for j in range(int(n_p)):
            c1, c2, c3, c4 = st.columns(4)
            label = c1.text_input("Label", value=f"P{j+1}", key=f"s{i}p{j}lbl")
            d = c2.number_input("Ngk (kN)", value=0.0, step=0.5, key=f"s{i}p{j}d")
            l = c3.number_input("Nqk (kN)", value=0.0, step=0.5, key=f"s{i}p{j}l")
            x = c4.number_input("Position (m)", value=0.0, step=0.1, key=f"s{i}p{j}x")
            point_loads.append(PointLoad(label, d, l, x))

        left_cond = start_cond if i == 0 else "Pin"
        right_cond = end_cond if i == int(n_spans) - 1 else "Pin"
        beam.add_span(Span(i + 1, lengths[i], labels[i], labels[i + 1],
                            contributions=contributions, wall_present=wall_here,
                            point_loads=point_loads, self_weight_kNm=self_weight,
                            left_condition=left_cond, right_condition=right_cond))

# ---------------- Step 9 & 10: compute + tabulate ----------------
beam.compute_all()
st.header("9-10. Results")

for s in beam.spans:
    for pos, g in s.governing.items():
        others = [c for c in g['all_candidates'] if c[0] != g['panel_id']]
        if others:
            st.caption(f"Span {s.index}, {pos}: Panel {g['panel_id']} governs over "
                       f"panel(s) {', '.join(str(o[0]) for o in others)} "
                       f"(Gk={g['dead']:.3f} kN/m)")

for i, s in enumerate(beam.spans):
    RL, RR = s.reactions()
    st.subheader(f"Span {i+1}")
    c1, c2, c3 = st.columns(3)
    c1.metric("UDL (Gk / Qk)", f"{s.udl_dead:.2f} / {s.udl_live:.2f} kN/m")
    c2.metric(f"Reaction {labels[i]}", f"{RL['dead']:.2f} / {RL['live']:.2f} kN")
    c3.metric(f"Reaction {labels[i+1]}", f"{RR['dead']:.2f} / {RR['live']:.2f} kN")

st.subheader("Total support reactions")
totals = beam.support_reactions()
rows = [{"Support": lbl, "Gk (kN)": round(totals[lbl]['dead'], 3),
         "Qk (kN)": round(totals[lbl]['live'], 3),
         "Total (kN)": round(totals[lbl]['dead'] + totals[lbl]['live'], 3)} for lbl in labels]
st.table(rows)

# ---------------- Step 11: diagram ----------------
st.header("11. Diagram")
path = beam.plot("diagram.png")
st.image(path)

with st.expander("Full text summary"):
    st.code(beam.tabulate())

# ---------------- Step 12: PDF report ----------------
st.header("12. PDF report")
if st.button("Generate PDF report"):
    pdf_path = beam.generate_pdf("beam_report.pdf")
    with open(pdf_path, "rb") as f:
        st.download_button("Download PDF report", f, file_name="beam_report.pdf", mime="application/pdf")
