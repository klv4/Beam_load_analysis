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
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from beam_multi_span import (
    ProjectInfo, DesignCriteria, SlabPanel, WallLoad, PanelContribution, PointLoad,
    Span, BeamSystem, FINISHES_OPTIONS, LIVE_LOAD_OPTIONS, bs8110_classify_panel,
    panel_sketch_figure,
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
        ly = c2.number_input("ly — long dimension (m)", value=5.0, key=f"ply{i}")
        lx = c3.number_input("lx — short dimension (m)", value=2.0, key=f"plx{i}")
        if lx > ly:
            ly, lx = lx, ly
            st.info(f"ly must be the longer dimension — swapped automatically: ly={ly:g} m, lx={lx:g} m")
        ratio = ly / lx if lx > 0 else float("inf")
        spanning = "Two-way" if ratio <= 2.0 else "One-way"
        st.caption(f"ly/lx = {ratio:.2f}  ->  {'\u2264 2.0' if spanning=='Two-way' else '> 2.0'}  ->  "
                   f"**{spanning} spanning slab**")
        c4, c5 = st.columns(2)
        fin_key = c4.selectbox("Finishes", options=list(FINISHES_OPTIONS.keys()),
                                format_func=lambda k: fin_labels[k], key=f"pfin{i}")
        live_key = c5.selectbox("Live load", options=list(LIVE_LOAD_OPTIONS.keys()),
                                 format_func=lambda k: live_labels[k], key=f"pliv{i}")
        has_partition = st.checkbox("Partition wall on this panel?", key=f"ppart{i}")
        plen = pthk = pht = pfl = pdp = 0.0
        if has_partition:
            c6, c7 = st.columns(2)
            plen = c6.number_input("Partition length (m)", value=5.0, key=f"pplen{i}")
            pthk = c7.number_input("Partition thickness (m)", value=0.2, key=f"ppthk{i}")
            c8, c9 = st.columns(2)
            pfl = c8.number_input("Floor height (m)", value=3.6, step=0.1, key=f"ppfl{i}")
            pdp = c9.number_input("Slab depth (m)", value=round(t / 1000.0, 3), step=0.05,
                                  format="%.3f", key=f"ppdp{i}_{t}")
            pht = max(0.0, pfl - pdp)
            st.caption(f"Partition height = floor height − slab depth = {pfl:g} − {pdp:g} = **{pht:.2f} m**")

        st.markdown("**Distribution factor** (auto — BS 8110-1:1997 Table 3.15 for solid slabs)")
        type_opts = {"Solid slab (Table 3.15 — one-way uses beta_vx at ly/lx = 2.0)": "solid",
                     "Ribbed slab (factor 0.5, double arrow = rib span)": "ribbed",
                     "Cantilever slab (single arrow = loading direction)": "cantilever"}
        slab_type = type_opts[st.selectbox("Panel type", list(type_opts.keys()), key=f"pstype{i}")]

        st.markdown("**Panel edge on this beam — orientation**")
        st.caption("Pick the edge of this panel that sits on the beam (reference edge). It is applied "
                   "automatically to 'Panel edge on this beam' under Panel contributions. Also say whether "
                   "that edge is your ly or lx side.")
        oc1, oc2 = st.columns(2)
        primary_edge = oc1.selectbox("Reference edge", ["top", "bottom", "left", "right"],
                                      index=2, key=f"pref{i}")
        primary_edge_is_ly = oc2.checkbox(f"'{primary_edge}' edge is the ly ({ly:g} m) side",
                                           value=False, key=f"prefly{i}")
        opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}[primary_edge]
        pair = {primary_edge, opposite}
        ly_edges = pair if primary_edge_is_ly else ({"top", "bottom", "left", "right"} - pair)
        lx_edges = {"top", "bottom", "left", "right"} - ly_edges

        edge_continuous = {"top": True, "bottom": True, "left": True, "right": True}
        arrow_perpendicular, arrow_flip, rib_lx = True, False, 0.525
        if slab_type in ("ribbed", "cantilever"):
            what = "Rib spanning direction (double arrow)" if slab_type == "ribbed" \
                else "Cantilever loading direction (single arrow)"
            rel = st.radio(f"{what} relative to the reference edge",
                           ["Perpendicular to the reference edge", "Parallel to the reference edge"],
                           key=f"parrow{i}")
            arrow_perpendicular = rel.startswith("Perpendicular")
            if not arrow_perpendicular:
                if slab_type == "ribbed":
                    rib_lx = st.number_input("lx — rib load width (m)", value=0.525, step=0.005,
                                              format="%.3f", key=f"prib{i}")
                else:
                    arrow_flip = st.checkbox("Flip arrow direction (sketch only — no effect on load)",
                                              key=f"pflip{i}")

        panel_obj = SlabPanel(name, t, ly, lx, FINISHES_OPTIONS[fin_key][1], LIVE_LOAD_OPTIONS[live_key][1],
                              has_partition, plen, pthk, pht, slab_type, edge_continuous,
                              primary_edge, primary_edge_is_ly,
                              partition_floor_ht_m=pfl, partition_depth_m=pdp,
                              arrow_perpendicular=arrow_perpendicular, arrow_flip=arrow_flip,
                              rib_lx_m=rib_lx)

        if slab_type == "solid":
            ec1, ec2 = st.columns([1, 1])
            with ec1:
                for edge_key in ("top", "bottom", "left", "right"):
                    dim = "ly" if edge_key in ly_edges else "lx"
                    dim_val = ly if dim == "ly" else lx
                    edge_continuous[edge_key] = st.checkbox(
                        f"{edge_key.capitalize()} continuous ({dim} edge, {dim_val:g} m)",
                        value=True, key=f"e{edge_key}{i}")
            with ec2:
                st.pyplot(panel_sketch_figure(panel_obj), width='content')
            st.caption(f"BS 8110 panel type: **{bs8110_classify_panel(edge_continuous, list(ly_edges), list(lx_edges))}**"
                       + (f"  — *one-way: beta_vx taken at ly/lx = 2.0*" if spanning == "One-way" else ""))
        else:
            sk1, sk2 = st.columns([1, 1])
            with sk2:
                st.pyplot(panel_sketch_figure(panel_obj), width='content')
            with sk1:
                f_ = panel_obj.distribution_factor(primary_edge)
                st.markdown(f"Factor = **{f_:.2f}**  \nLoad width lx = **{panel_obj.load_width_m():.3f} m**")
                if slab_type == "ribbed":
                    st.caption("Ribs perpendicular to the beam: lx = panel length along the ribs."
                               if arrow_perpendicular else
                               "Ribs parallel to the beam: lx = rib load width entered above.")
                else:
                    st.caption("Cantilever loading perpendicular to the beam (arrow points toward it): factor 1.0, lx = projection length."
                               if arrow_perpendicular else
                               "Beam runs along the cantilever direction: factor 0.5, lx = shortest panel dimension.")
        panels[name] = panel_obj
        st.caption(f"-> Gₖ = {panels[name].dead_kNm2(dc):.3f} kN/m²   Qₖ = {panels[name].live_kNm2_factored(dc):.3f} kN/m²"
                   f"  (Step 5: calculated automatically)")

# ---------------- Step 6: wall ----------------
st.header("6. Wall loading")
wall_defined = st.checkbox("Is there direct wall loading on the beam anywhere?")
if wall_defined:
    c1, c2, c3 = st.columns(3)
    wt = c1.number_input("Wall thickness (m)", value=0.2, step=0.05)
    wfl = c2.number_input("Floor height (m)", value=3.6, step=0.1)
    wdp = c3.number_input("Beam depth (m)", value=0.45, step=0.05)
    wall = WallLoad(wt, wfl, wdp)
    st.caption(f"Wall height = floor height − beam depth = {wfl:g} − {wdp:g} = **{wall.height_m:.2f} m**")
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
            c1, c2 = st.columns(2)
            pid = c1.selectbox("Panel", options=list(panels.keys()), key=f"s{i}c{j}p")
            panel = panels[pid]
            edge = panel.primary_edge
            c2.selectbox("Panel edge on this beam", options=[edge], index=0, disabled=True,
                         key=f"s{i}c{j}edge_{pid}_{edge}")
            st.caption("Set from the panel's reference edge (Step 3-4). Contributions sharing the same "
                       "edge are treated as being on the same side of the beam.")
            contributions.append(PanelContribution(pid, edge))
            factor = panel.distribution_factor(edge)
            st.caption(f"-> distribution factor = {factor:.3f}   |   Lx = {panel.load_width_m():.3f} m")

        wall_here = False
        if wall_defined:
            wall_here = st.checkbox("Wall present on this span", key=f"s{i}wall")

        self_weight = st.number_input("Element self-weight (kN/m) — added automatically to Gₖ",
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
                       f"(Gₖ={g['dead']:.3f} kN/m)")

for i, s in enumerate(beam.spans):
    RL, RR = s.reactions()
    st.subheader(f"Span {i+1}")
    c1, c2, c3 = st.columns(3)
    c1.metric("UDL (Gₖ / Qₖ)", f"{s.udl_dead:.2f} / {s.udl_live:.2f} kN/m")
    c2.metric(f"Reaction {labels[i]}", f"{RL['dead']:.2f} / {RL['live']:.2f} kN")
    c3.metric(f"Reaction {labels[i+1]}", f"{RR['dead']:.2f} / {RR['live']:.2f} kN")

st.subheader("Total support reactions")
totals = beam.support_reactions()
rows = [{"Support": lbl, "Gₖ (kN)": round(totals[lbl]['dead'], 3),
         "Qₖ (kN)": round(totals[lbl]['live'], 3),
         "Total (kN)": round(totals[lbl]['dead'] + totals[lbl]['live'], 3)} for lbl in labels]
st.table(rows)

# ---------------- Step 11: diagram ----------------
st.header("11. Diagram")
fig, _ = beam.figure()
st.pyplot(fig, width='stretch')

with st.expander("Full text summary"):
    st.code(beam.tabulate())

# ---------------- Step 12: PDF report ----------------
st.header("12. PDF report")
if st.button("Generate PDF report"):
    pdf_path = beam.generate_pdf("beam_report.pdf")
    with open(pdf_path, "rb") as f:
        st.download_button("Download PDF report", f, file_name="beam_report.pdf", mime="application/pdf")
