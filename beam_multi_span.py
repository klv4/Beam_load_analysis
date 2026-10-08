"""
beam_multi_span.py
====================
Multi-span beam load-analysis engine — generalizes the Horicon Engineering
Solutions "Load Analysis" spreadsheet to any number of spans, with an
interactive step-by-step wizard (beam_wizard.py) built on top of it.

References: BS 6399-1:1996 Table 1, EN 1990-1:2002 Table A1.2(B),
BS 8110-1:1997 Tables 3.3 & 3.4.

Engineering model
------------------
* Each span is analysed with simple statics — this matches the spreadsheet,
  which does not solve indeterminate continuous-beam bending. Each span's
  reactions are computed independently and shared supports simply sum the
  contributions of the two spans meeting there (pinned & continuous).
* The two OUTER ends of the whole beam may be "Pin" (ordinary simple
  support) or "Cantilever" (that end is unsupported — the end span
  overhangs, so its single real support takes 100% of that span's load
  and the free tip takes zero).
* Where two or more panels bear onto the same side of a span, the
  GOVERNING (critical) panel — the one producing the larger load — is
  identified and reported explicitly, exactly as the spreadsheet's
  MAXIFS(...) logic does.
"""

from dataclasses import dataclass, field
import math
import os
import re
from typing import List, Dict, Tuple, Optional
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.backends.backend_pdf import PdfPages

# Upright (non-italic) mathtext so subscripts such as nG_k, f_cu, l_x render cleanly
matplotlib.rcParams['mathtext.default'] = 'regular'


def sub(txt: str) -> str:
    """Turns plain engineering shorthand into proper subscripts (matplotlib mathtext):
    nGk -> nG_k, nQk -> nQ_k, Gk, Qk, fcu, fy, fyv, lx, ly, beta_vx, beta_vy."""
    txt = re.sub(r'\b(n?)([GQ])k\b', r'\1$\2_{k}$', txt)
    txt = txt.replace('βvx', r'$\beta_{vx}$').replace('βvy', r'$\beta_{vy}$')
    txt = re.sub(r'\bfcu\b', r'$f_{cu}$', txt)
    txt = re.sub(r'\bfyv\b', r'$f_{yv}$', txt)
    txt = re.sub(r'\bfy\b', r'$f_{y}$', txt)
    txt = re.sub(r'\bl([xy])\b', r'$l_{\1}$', txt)
    return txt


# ============================================================
# STEP 1 — Project information
# ============================================================
@dataclass
class ProjectInfo:
    firm_name: str = "INFRAS ENGINEERING SOLUTIONS"
    address: str = "P.O Box 45297-00100 Nairobi"
    tel: str = "+254 723703705"
    email: str = "info@infrasengineering.com"
    logo_path: str = ""            # blank -> logo.png in the same folder as this module
    project_title: str = ""        # shown in the title block
    checked_by: str = ""
    job_no: str = ""
    calc_sheet_no: str = ""
    designer: str = ""
    date: str = ""
    revision: str = ""
    element: str = ""              # e.g. "Second Floor Beam SF9"
    beam_type: str = ""
    along_grid: str = ""           # grid reference, part 1:  e.g. "1"
    between_grids: str = ""        # grid reference, part 2:  e.g. "D/1 and G/1"
    material: str = ""             # e.g. "Beam 9"

    def drawing_title(self) -> str:
        """e.g. 'CONTINUOUS BEAM F3 ALONG GRID 1 BETWEEN GRIDS D/1 AND G/1' — built from the
        element field plus the two-part grid reference."""
        def clean(t, word):
            return re.sub(rf'^\s*{word}\s+', '', (t or '').strip(), flags=re.IGNORECASE)
        parts = [(self.element or "").strip()]
        if (self.along_grid or "").strip():
            parts.append(f"ALONG GRID {clean(self.along_grid, 'grid')}")
        if (self.between_grids or "").strip():
            parts.append(f"BETWEEN GRIDS {clean(self.between_grids, 'grids?')}")
        return " ".join(x for x in parts if x).upper()


# ============================================================
# Design criteria & selectable load options (from the spreadsheet)
# ============================================================
FINISHES_OPTIONS = {
    "1": ("Open Areas (APP & Interlocking Blocks)", 2.5),
    "2": ("Residential Areas (Screed and Tiles)", 1.5),
}
LIVE_LOAD_OPTIONS = {
    "1": ("Open Areas (Water tanks / Solar Panels)", 5.0),
    "2": ("Residential Areas (General occupancy)", 1.5),
    "3": ("Offices & Bed Areas", 2.5),
    "4": ("Corridors", 5.0),
}


@dataclass
class DesignCriteria:
    concrete_density: float = 24.0             # kN/m3
    wall_density: float = 20.0                 # kN/m3
    factor_selfweight_partition: float = 1.0   # applied to self-weight + partitions + wall
    factor_finishes: float = 1.35              # applied to finishes
    factor_live: float = 1.5                   # applied to live loads
    # Reporting-only fields (BS 8110-1:1997 Tables 3.3 & 3.4) — no effect on the calculation,
    # included so the PDF report matches the spreadsheet's "Design Criteria & Materials" table.
    concrete_grade: str = "Grade 25"
    steel_grade: str = "High Yield Steel"
    exposure_condition: str = "Mild"
    fire_resistance_hours: float = 1.5
    concrete_cover_mm: float = 25


# ============================================================
# BS 8110-1:1997 Table 3.15 — Shear force coefficients for uniformly loaded
# rectangular panels supported on four sides with provision for torsion at
# corners. Used to AUTOMATICALLY derive each panel-to-beam distribution
# factor from the panel's aspect ratio (ly/lx) and edge continuity, instead
# of it being typed in by hand.
#
# Per the code's own note: vs = vsx (uses beta_vx, interpolated across
# ly/lx) when l = ly  -> i.e. for the LONG edges (top/bottom, length ly);
#           vs = vsy (uses beta_vy, ~constant)   when l = lx  -> i.e. for the
# SHORT edges (left/right, length lx). This matches Figure 3.8's convention:
# left/right edges have length lx ("short edges"), top/bottom have length ly
# ("long edges").
# ============================================================
BS8110_RATIOS = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0]

BS8110_TABLE_3_15 = {
    "Four edges continuous": {
        "continuous": {"vx": [0.33, 0.36, 0.39, 0.41, 0.43, 0.45, 0.48, 0.50], "vy": 0.33},
    },
    "One short edge discontinuous": {
        "continuous": {"vx": [0.36, 0.39, 0.42, 0.44, 0.45, 0.47, 0.50, 0.52], "vy": 0.36},
        "discontinuous": {"vx": None, "vy": 0.24},
    },
    "One long edge discontinuous": {
        "continuous": {"vx": [0.36, 0.40, 0.44, 0.47, 0.49, 0.51, 0.55, 0.59], "vy": 0.36},
        "discontinuous": {"vx": [0.24, 0.27, 0.29, 0.31, 0.32, 0.34, 0.36, 0.38], "vy": None},
    },
    "Two adjacent edges discontinuous": {
        "continuous": {"vx": [0.40, 0.44, 0.47, 0.50, 0.52, 0.54, 0.57, 0.60], "vy": 0.40},
        "discontinuous": {"vx": [0.26, 0.29, 0.31, 0.33, 0.34, 0.35, 0.38, 0.40], "vy": 0.26},
    },
    "Two short edges discontinuous": {
        "continuous": {"vx": [0.40, 0.43, 0.45, 0.47, 0.48, 0.49, 0.52, 0.54], "vy": None},
        "discontinuous": {"vx": None, "vy": 0.26},
    },
    "Two long edges discontinuous": {
        "continuous": {"vx": None, "vy": 0.40},
        "discontinuous": {"vx": [0.26, 0.30, 0.33, 0.36, 0.38, 0.40, 0.44, 0.47], "vy": None},
    },
    "Three edges discontinuous (one long edge continuous)": {
        "continuous": {"vx": [0.45, 0.48, 0.51, 0.53, 0.55, 0.57, 0.60, 0.63], "vy": None},
        "discontinuous": {"vx": [0.30, 0.32, 0.34, 0.35, 0.36, 0.37, 0.39, 0.41], "vy": 0.29},
    },
    "Three edges discontinuous (one short edge continuous)": {
        "continuous": {"vx": None, "vy": 0.45},
        "discontinuous": {"vx": [0.29, 0.33, 0.36, 0.38, 0.40, 0.42, 0.45, 0.48], "vy": 0.30},
    },
    "Four edges discontinuous": {
        "discontinuous": {"vx": [0.33, 0.36, 0.39, 0.41, 0.43, 0.45, 0.48, 0.50], "vy": 0.33},
    },
}

EDGE_NAMES = ("top", "bottom", "left", "right")
OPPOSITE_EDGE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
# Defaults, used only when a panel-specific orientation isn't supplied. Which
# physical edges actually carry ly vs lx is now per-panel (see
# SlabPanel.ly_edges/lx_edges) since a panel's long dimension can run
# horizontally or vertically depending on how it was drawn.
SHORT_EDGES = ("left", "right")   # length lx (default orientation)
LONG_EDGES = ("top", "bottom")    # length ly (default orientation)


def bs8110_classify_panel(edge_continuous: Dict[str, bool], long_edges=LONG_EDGES,
                           short_edges=SHORT_EDGES) -> str:
    """Maps the 4 individual edge continuity states to one of Table 3.15/3.14's
    9 named panel types (BS 8110-1:1997 3.5.3.7 / Table 3.14 classification).
    `long_edges`/`short_edges` say which physical edges (top/bottom/left/right)
    carry the ly / lx dimension for THIS panel — see SlabPanel.ly_edges()."""
    n_disc_short = sum(1 for e in short_edges if not edge_continuous[e])
    n_disc_long = sum(1 for e in long_edges if not edge_continuous[e])
    total = n_disc_short + n_disc_long

    if total == 0:
        return "Four edges continuous"
    if total == 4:
        return "Four edges discontinuous"
    if total == 1:
        return "One short edge discontinuous" if n_disc_short == 1 else "One long edge discontinuous"
    if total == 2:
        if n_disc_short == 2:
            return "Two short edges discontinuous"
        if n_disc_long == 2:
            return "Two long edges discontinuous"
        return "Two adjacent edges discontinuous"
    if total == 3:
        return ("Three edges discontinuous (one short edge continuous)" if n_disc_short == 1
                else "Three edges discontinuous (one long edge continuous)")
    raise ValueError("Invalid edge continuity state")


def bs8110_interp(values: List[float], ratio: float) -> float:
    """Linear interpolation of a Table 3.15 vx row across the standard ly/lx columns."""
    ratio = max(BS8110_RATIOS[0], min(ratio, BS8110_RATIOS[-1]))
    for i in range(len(BS8110_RATIOS) - 1):
        r0, r1 = BS8110_RATIOS[i], BS8110_RATIOS[i + 1]
        if r0 <= ratio <= r1:
            v0, v1 = values[i], values[i + 1]
            frac = (ratio - r0) / (r1 - r0) if r1 != r0 else 0.0
            return v0 + frac * (v1 - v0)
    return values[-1]


def bs8110_beta_for_edge(edge_continuous: Dict[str, bool], ly_m: float, lx_m: float, edge: str,
                          long_edges=LONG_EDGES, short_edges=SHORT_EDGES) -> float:
    """The Table 3.15 shear coefficient (beta_vx for long/ly edges, beta_vy for
    short/lx edges) for one specific edge of a solid slab panel.

    Two-way spanning (ly/lx <= 2.0): beta_vx is interpolated across the
    standard ly/lx columns as normal.
    One-way spanning (ly/lx > 2.0): `bs8110_interp` clamps its input ratio to
    the table's highest column (2.0), which IS the rule "take beta_vx for
    ly/lx = 2.0" — so no separate branch is needed here, it falls out of the
    same clamped interpolation used for the two-way case."""
    ratio = (ly_m / lx_m) if lx_m > 0 else 1.0   # ly_m is always >= lx_m (SlabPanel enforces this)
    panel_type = bs8110_classify_panel(edge_continuous, long_edges, short_edges)
    row = BS8110_TABLE_3_15[panel_type]["continuous" if edge_continuous[edge] else "discontinuous"]
    if edge in short_edges:
        if row["vy"] is None:
            raise ValueError(f"Table 3.15 has no vy value for '{panel_type}' — check edge continuity inputs.")
        return row["vy"]
    else:
        if row["vx"] is None:
            raise ValueError(f"Table 3.15 has no vx value for '{panel_type}' — check edge continuity inputs.")
        return bs8110_interp(row["vx"], ratio)


def slab_panel_distribution_factor(panel: "SlabPanel", edge: str) -> float:
    """The value that replaces the old hand-typed 'distribution factor':
    - Ribbed slab       -> fixed 0.5
    - Cantilever        -> fixed 1.0
    - Solid, two-way    -> BS 8110-1:1997 Table 3.15 beta_vx / beta_vy, based
                           on this panel's aspect ratio and the continuity of
                           the specific edge bearing onto the beam.
    - Solid, one-way    -> same Table 3.15 lookup, but beta_vx is taken at
                           ly/lx = 2.0 (handled automatically by the clamped
                           interpolation in bs8110_beta_for_edge)."""
    if panel.slab_type == "ribbed":
        return 0.5
    if panel.slab_type == "cantilever":
        # Arrow (loading direction) perpendicular to the beam edge: the whole
        # cantilever load drains into the beam -> 1.0. Beam lies along the
        # loading direction (parallel): each side takes half -> 0.5.
        return 1.0 if panel.arrow_perpendicular else 0.5
    return bs8110_beta_for_edge(panel.edge_continuous, panel.ly_m, panel.lx_m, edge,
                                 panel.ly_edges(), panel.lx_edges())


# ============================================================
# STEP 3/4 — slab panels
# ============================================================
@dataclass
class SlabPanel:
    panel_id: str
    thickness_mm: float
    ly_m: float
    lx_m: float
    finishes_kNm2: float
    live_kNm2: float
    has_partition: bool = False
    partition_len_m: float = 0.0
    partition_thk_m: float = 0.0
    partition_ht_m: float = 0.0
    slab_type: str = "solid"          # "solid" | "ribbed" | "cantilever"
    edge_continuous: Dict[str, bool] = field(
        default_factory=lambda: {"top": True, "bottom": True, "left": True, "right": True})
    # Orientation: which physical edge carries the ly (long) dimension isn't
    # always top/bottom — a panel can be drawn either way. `primary_edge` is
    # a single reference edge the user points to, and `primary_edge_is_ly`
    # says whether THAT edge (and its opposite) is the ly side or the lx
    # side. Everything else (sketch labelling, which edges use beta_vx vs
    # beta_vy) is derived from this one pair of fields.
    primary_edge: str = "left"
    primary_edge_is_ly: bool = False
    # Ribbed / cantilever only: direction of the rib span (double arrow) or the
    # cantilever loading direction (single arrow), relative to the reference edge
    # (= the edge of the panel that sits on the beam being analysed).
    arrow_perpendicular: bool = True    # True: arrow is perpendicular to the reference edge
    arrow_flip: bool = False            # cantilever, arrow parallel to edge: sketch direction only
    rib_lx_m: float = 0.525             # ribbed, ribs parallel to the beam: load width lx (m)
    partition_floor_ht_m: float = 0.0   # floor height used to derive partition_ht_m
    partition_depth_m: float = 0.0      # slab depth deducted from the floor height
    finishes_label: str = ""            # e.g. "Residential Areas (Screed and Tiles)" — shown in the PDF
    live_label: str = ""                # e.g. "Offices & Bed Areas" — shown in the PDF

    def __post_init__(self):
        # partition wall height = floor height - slab depth (when those are supplied)
        if self.partition_floor_ht_m > 0:
            self.partition_ht_m = max(0.0, self.partition_floor_ht_m - self.partition_depth_m)
        # ly is BY DEFINITION the longer dimension — auto-correct if entered
        # the other way round rather than silently computing with them
        # swapped relative to what Table 3.15 expects.
        if self.lx_m > self.ly_m:
            self.ly_m, self.lx_m = self.lx_m, self.ly_m

    def ly_edges(self) -> set:
        """Which of the 4 edges (top/bottom/left/right) carry the ly (long) dimension."""
        pair = {self.primary_edge, OPPOSITE_EDGE[self.primary_edge]}
        return pair if self.primary_edge_is_ly else ({"top", "bottom", "left", "right"} - pair)

    def lx_edges(self) -> set:
        return {"top", "bottom", "left", "right"} - self.ly_edges()

    def arrow_axis(self) -> str:
        """'horizontal' or 'vertical' — direction of the rib span / cantilever arrow."""
        ref_horizontal = self.primary_edge in ("top", "bottom")
        if self.arrow_perpendicular:
            return "vertical" if ref_horizontal else "horizontal"
        return "horizontal" if ref_horizontal else "vertical"

    def arrow_vector(self) -> Tuple[int, int]:
        """Unit (dx, dy) for the single cantilever arrow (y up). Perpendicular arrows
        point TOWARDS the reference (beam) edge — all the load goes into the beam."""
        if self.arrow_perpendicular:
            return {"bottom": (0, -1), "top": (0, 1), "left": (-1, 0), "right": (1, 0)}[self.primary_edge]
        base = (1, 0) if self.arrow_axis() == "horizontal" else (0, 1)
        return (-base[0], -base[1]) if self.arrow_flip else base

    def span_length_along_arrow(self) -> float:
        """Panel dimension measured parallel to the arrow direction."""
        if self.arrow_axis() == "horizontal":
            return self.ly_m if "top" in self.ly_edges() else self.lx_m
        return self.ly_m if "left" in self.ly_edges() else self.lx_m

    def load_width_m(self) -> float:
        """The 'lx' used in  w = factor * lx * (panel load)."""
        if self.slab_type == "ribbed":
            return self.span_length_along_arrow() if self.arrow_perpendicular else self.rib_lx_m
        if self.slab_type == "cantilever":
            return self.span_length_along_arrow() if self.arrow_perpendicular else self.lx_m
        return self.lx_m

    def describe_bearing(self, edge: str) -> List[str]:
        """Worked-calculation lines describing how this panel bears on the beam (for the PDF)."""
        ly, lx = self.ly_m, self.lx_m
        head = f"Panel {self.panel_id}: {self.thickness_mm:g} mm, bears on beam via {edge} edge"
        if self.slab_type == "solid":
            ratio = self.spanning_ratio()
            is_ly = edge in self.ly_edges()
            cont = self.edge_continuous[edge]
            if ratio <= 2.0:
                sp = f"ly/lx = {ly:.3f}/{lx:.3f} = {ratio:.2f} ≤ 2.0  ⇒  Two-way spanning slab"
            else:
                sp = (f"ly/lx = {ly:.3f}/{lx:.3f} = {ratio:.2f} > 2.0  ⇒  One-way spanning slab, "
                      f"take βvx at ly/lx = 2.0")
            return [head, sp, self.panel_type_bs8110(),
                    f"Edge on beam: {'continuous' if cont else 'discontinuous'} ({'ly' if is_ly else 'lx'} edge)",
                    f"β{'vx' if is_ly else 'vy'} = {self.distribution_factor(edge):.3f}",
                    f"w = β · n · lx ,   lx = {lx:.3f} m"]
        rel = "perpendicular" if self.arrow_perpendicular else "parallel"
        if self.slab_type == "ribbed":
            why = ("lx = panel length along the ribs" if self.arrow_perpendicular
                   else "lx = rib load width")
            return [head, f"Ribbed slab, ribs span (double arrow) {rel} to the beam",
                    f"Panel size: ly = {ly:.3f} m, lx = {lx:.3f} m",
                    "w = 0.5 · n · lx", f"lx = {self.load_width_m():.3f} m  ({why})"]
        why = ("lx = cantilever projection, all load to beam" if self.arrow_perpendicular
               else "beam along loading direction, lx = shortest panel dimension")
        return [head, f"Cantilever slab, loading direction (single arrow) {rel} to the beam",
                f"Panel size: ly = {ly:.3f} m, lx = {lx:.3f} m",
                f"w = {self.distribution_factor(edge):.1f} · n · lx", f"lx = {self.load_width_m():.3f} m  ({why})"]

    def spanning_ratio(self) -> float:
        return self.ly_m / self.lx_m if self.lx_m > 0 else float("inf")

    def spanning_type(self) -> str:
        """BS 8110-1:1997 2.1.3.2 — effectively one-way once ly/lx exceeds 2.0."""
        return "Two-way" if self.spanning_ratio() <= 2.0 else "One-way"

    def panel_type_bs8110(self) -> str:
        """The Table 3.14/3.15 panel classification derived from this panel's edges."""
        return bs8110_classify_panel(self.edge_continuous, self.ly_edges(), self.lx_edges())

    def distribution_factor(self, edge: str) -> float:
        return slab_panel_distribution_factor(self, edge)

    def self_weight_kNm2(self, dc: DesignCriteria) -> float:
        return self.thickness_mm * dc.concrete_density / 1000.0

    def partition_kNm2(self, dc: DesignCriteria) -> float:
        if not self.has_partition or self.ly_m == 0 or self.lx_m == 0:
            return 0.0
        return (self.partition_len_m * self.partition_thk_m * self.partition_ht_m
                * dc.wall_density) / (self.ly_m * self.lx_m)

    def dead_kNm2(self, dc: DesignCriteria) -> float:
        return (dc.factor_selfweight_partition * (self.self_weight_kNm2(dc) + self.partition_kNm2(dc))
                + dc.factor_finishes * self.finishes_kNm2)

    def live_kNm2_factored(self, dc: DesignCriteria) -> float:
        return dc.factor_live * self.live_kNm2


# ============================================================
# Panel sketch (shared by the Streamlit app and the PDF report)
# ============================================================
def draw_panel_sketch_ax(ax, panel):
    """Draws a panel sketch on `ax`.
    Solid: thick navy edge + hatch ticks = continuous, thin dashed grey = discontinuous.
    Orange line just INSIDE an edge = the beam (reference edge) — it sits inside the panel
    so it never covers the continuity shading. Ribbed: double arrow = rib span.
    Cantilever: single arrow = loading direction (points toward the beam when perpendicular)."""
    ax.set_xlim(-0.3, 1.3)
    ax.set_ylim(-0.3, 1.3)
    ax.set_aspect('equal')
    ax.axis('off')
    seg = {'top': ([0, 1], [1, 1]), 'bottom': ([0, 1], [0, 0]),
           'left': ([0, 0], [0, 1]), 'right': ([1, 1], [0, 1])}
    normal = {'top': (0, 1), 'bottom': (0, -1), 'left': (-1, 0), 'right': (1, 0)}
    solid = panel.slab_type == "solid"
    for k, (xs, ys) in seg.items():
        cont = panel.edge_continuous[k] if solid else None
        if cont is True:
            ax.plot(xs, ys, color='#1F4E78', lw=3.0, solid_capstyle='butt', zorder=3)
            nx, ny = normal[k]
            for t in [0.05 + 0.9 * j / 9 for j in range(10)]:
                px, py = (t, ys[0]) if k in ('top', 'bottom') else (xs[0], t)
                tx, ty = (0.06, 0) if k in ('top', 'bottom') else (0, 0.06)
                ax.plot([px, px + 0.06 * nx + tx], [py, py + 0.06 * ny + ty], color='#1F4E78', lw=0.9)
        elif cont is False:
            ax.plot(xs, ys, color='#999999', lw=1.2, linestyle=(0, (4, 3)), zorder=2)
        else:
            ax.plot(xs, ys, color='#888888', lw=1.2, zorder=2)
    o = 0.055
    inside = {'top': ([0.05, 0.95], [1 - o, 1 - o]), 'bottom': ([0.05, 0.95], [o, o]),
              'left': ([o, o], [0.05, 0.95]), 'right': ([1 - o, 1 - o], [0.05, 0.95])}
    bx, by = inside[panel.primary_edge]
    ax.plot(bx, by, color='#E07B00', lw=2.6, solid_capstyle='butt', zorder=4)
    lab = {'top': (0.5, 0.86, 'center', 'top', 0), 'bottom': (0.5, 0.14, 'center', 'bottom', 0),
           'left': (0.15, 0.5, 'left', 'center', 90), 'right': (0.85, 0.5, 'right', 'center', 90)}[panel.primary_edge]
    ax.text(lab[0], lab[1], "beam", ha=lab[2], va=lab[3], rotation=lab[4], fontsize=7, color='#E07B00')

    if panel.slab_type in ("ribbed", "cantilever"):
        if panel.slab_type == "cantilever":
            dx, dy = panel.arrow_vector()
            half, style_ = 0.25, '-|>'
        else:
            dx, dy = (1, 0) if panel.arrow_axis() == "horizontal" else (0, 1)
            half, style_ = 0.3, '<|-|>'
        ax.annotate('', xy=(0.5 + half * dx, 0.5 + half * dy), xytext=(0.5 - half * dx, 0.5 - half * dy),
                    zorder=6, arrowprops=dict(arrowstyle=style_, lw=2.0, color='#8B1E2E', mutation_scale=14))

    ly_e = panel.ly_edges()
    top_dim, side_dim = (panel.ly_m, panel.lx_m) if "top" in ly_e else (panel.lx_m, panel.ly_m)
    ax.text(0.5, 1.13, sub(f"{'ly' if 'top' in ly_e else 'lx'} = {top_dim:g} m"), ha='center', va='bottom', fontsize=8)
    ax.text(1.13, 0.5, sub(f"{'ly' if 'left' in ly_e else 'lx'} = {side_dim:g} m"), ha='left', va='center',
            fontsize=8, rotation=90)
    ax.text(0.5, -0.1, f"Panel {panel.panel_id}", ha='center', va='top', fontsize=8.5, fontweight='bold')


def panel_sketch_figure(panel):
    fig, ax = plt.subplots(figsize=(3.4, 3.4))
    draw_panel_sketch_ax(ax, panel)
    return fig


# ============================================================
# STEP 6 — wall loading (direct wall bearing on the beam)
# ============================================================
@dataclass
class WallLoad:
    thickness_m: float = 0.2
    floor_height_m: float = 3.6     # floor-to-floor height
    depth_m: float = 0.45           # depth of the supporting beam (wall sits on it)

    @property
    def height_m(self) -> float:
        """Wall height = floor height - beam depth."""
        return max(0.0, self.floor_height_m - self.depth_m)

    def factored_dead_kNm(self, dc: DesignCriteria) -> float:
        return dc.factor_selfweight_partition * (self.thickness_m * self.height_m * dc.wall_density)


# ============================================================
# STEP 7 — which panels touch which span, and how
# ============================================================
@dataclass
class PanelContribution:
    panel_id: str              # matches a SlabPanel.panel_id
    edge: str                  # "top" | "bottom" | "left" | "right" — which edge of the PANEL
                                # bears onto this beam. Drives the auto-computed distribution
                                # factor AND doubles as the grouping key for critical-panel
                                # selection (contributions sharing the same edge value are
                                # treated as being on the same side of the beam).


# ============================================================
# STEP 8 — point loads
# ============================================================
@dataclass
class PointLoad:
    label: str
    dead_kN: float             # the Ngk value typed in (kN), WITHOUT element self-weight
    live_kN: float
    position_m: float          # measured from this span's LEFT support
    # Optional element (beam / column) whose self-weight is added to dead_kN
    elem_b_m: float = 0.0      # width
    elem_h_m: float = 0.0      # depth
    elem_len_m: float = 0.0    # length
    density_kNm3: float = 24.0
    sw_factor: float = 1.0

    @property
    def self_weight_kN(self) -> float:
        return self.sw_factor * self.elem_b_m * self.elem_h_m * self.elem_len_m * self.density_kNm3

    @property
    def total_dead_kN(self) -> float:
        """Typed Ngk + element self-weight — this is the value used in every calculation."""
        return self.dead_kN + self.self_weight_kN


# ============================================================
# A single span
# ============================================================
@dataclass
class Span:
    index: int
    length_m: float
    left_support: str
    right_support: str
    contributions: List[PanelContribution] = field(default_factory=list)
    wall_present: bool = False
    point_loads: List[PointLoad] = field(default_factory=list)
    self_weight_kNm: float = 0.0        # STEP 8 — beam's own self-weight (kN/m), added to Gk
    left_condition: str = "Pin"         # "Pin" or "Cantilever" — only meaningful on the two outer ends
    right_condition: str = "Pin"
    udl_dead: float = 0.0
    udl_live: float = 0.0
    distribution_rows: List[Tuple] = field(default_factory=list)
    governing: Dict[str, Dict] = field(default_factory=dict)   # STEP 7 — critical panel per side

    def compute_udl(self, panels: Dict[str, SlabPanel], wall: WallLoad, dc: DesignCriteria):
        rows = []
        candidates: Dict[str, List[Tuple[int, float, float]]] = {}
        for c in self.contributions:
            p = panels[c.panel_id]
            factor = p.distribution_factor(c.edge)   # BS 8110 Table 3.15 (or 0.5/1.0 for ribbed/cantilever)
            dead = factor * p.load_width_m() * p.dead_kNm2(dc)
            live = factor * p.load_width_m() * p.live_kNm2_factored(dc)
            rows.append((c.panel_id, c.edge, dead, live))
            candidates.setdefault(c.edge, []).append((c.panel_id, dead, live))

        gov_dead_total, gov_live_total = 0.0, 0.0
        self.governing = {}
        for edge, opts in candidates.items():
            # governing (critical) panel = the one with the larger DEAD load on this side
            crit = max(opts, key=lambda o: o[1])
            self.governing[edge] = dict(panel_id=crit[0], dead=crit[1], live=crit[2],
                                         all_candidates=opts)
            gov_dead_total += crit[1]
            gov_live_total += crit[2]

        wall_dead = wall.factored_dead_kNm(dc) if self.wall_present else 0.0
        self.udl_dead = gov_dead_total + wall_dead + self.self_weight_kNm
        self.udl_live = gov_live_total
        self.distribution_rows = rows
        return rows

    def reaction_terms(self, side: str, kind: str) -> List[Tuple[str, float]]:
        """Worked terms (text, value) making up this span's reaction at side 'L' or 'R'
        for kind 'dead' or 'live' — mirrors reactions() exactly."""
        L = self.length_m
        w = self.udl_dead if kind == "dead" else self.udl_live
        loads = [(pl, pl.total_dead_kN if kind == "dead" else pl.live_kN) for pl in self.point_loads]
        if self.left_condition == "Cantilever":
            full = (side == "R")
        elif self.right_condition == "Cantilever":
            full = (side == "L")
        else:
            full = None
        terms = []
        if full is True:
            if w:
                terms.append((f"{w:.2f} × {L:.3f}", w * L))
            terms += [(f"{P:.2f}", P) for _, P in loads if P]
        elif full is None:
            if w:
                terms.append((f"{w:.2f} × {L:.3f}/2", w * L / 2))
            for pl, P in loads:
                if P:
                    a = (L - pl.position_m) if side == "L" else pl.position_m
                    terms.append((f"{P:.2f} × {a:.3f}/{L:.3f}", P * a / L))
        return terms

    def reactions(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """STEP 9 — returns (R_left, R_right), each {'dead':.., 'live':..},
        honouring Pin / Cantilever end conditions."""
        L = self.length_m
        total_dead = self.udl_dead * L + sum(p.total_dead_kN for p in self.point_loads)
        total_live = self.udl_live * L + sum(p.live_kN for p in self.point_loads)

        if self.left_condition == "Cantilever":
            return dict(dead=0.0, live=0.0), dict(dead=total_dead, live=total_live)
        if self.right_condition == "Cantilever":
            return dict(dead=total_dead, live=total_live), dict(dead=0.0, live=0.0)

        RL = dict(dead=self.udl_dead * L / 2, live=self.udl_live * L / 2)
        RR = dict(dead=self.udl_dead * L / 2, live=self.udl_live * L / 2)
        for p in self.point_loads:
            RL['dead'] += p.total_dead_kN * (L - p.position_m) / L
            RL['live'] += p.live_kN * (L - p.position_m) / L
            RR['dead'] += p.total_dead_kN * p.position_m / L
            RR['live'] += p.live_kN * p.position_m / L
        return RL, RR


# ============================================================
# The whole multi-span beam
# ============================================================
class BeamSystem:
    def __init__(self, dc: DesignCriteria, panels: Dict[str, SlabPanel], wall: WallLoad,
                 project: Optional[ProjectInfo] = None):
        self.dc = dc
        self.panels = panels
        self.wall = wall
        self.project = project or ProjectInfo()
        self.spans: List[Span] = []

    def add_span(self, span: Span):
        self.spans.append(span)

    def support_labels(self) -> List[str]:
        """A, B, ..., Z, AA, AB, ... — Excel-style, so it never runs out even
        with a very large number of spans."""
        n = len(self.spans)
        labels = []
        for i in range(n + 1):
            k = i
            label = ""
            while True:
                label = chr(65 + k % 26) + label
                k = k // 26 - 1
                if k < 0:
                    break
            labels.append(label)
        return labels

    def compute_all(self):
        for s in self.spans:
            s.compute_udl(self.panels, self.wall, self.dc)

    def support_reactions(self) -> Dict[str, Dict[str, float]]:
        labels = self.support_labels()
        totals = {lbl: dict(dead=0.0, live=0.0) for lbl in labels}
        for i, s in enumerate(self.spans):
            RL, RR = s.reactions()
            left_lbl, right_lbl = labels[i], labels[i + 1]
            totals[left_lbl]['dead'] += RL['dead']
            totals[left_lbl]['live'] += RL['live']
            totals[right_lbl]['dead'] += RR['dead']
            totals[right_lbl]['live'] += RR['live']
        return totals

    def support_condition_label(self, i: int) -> str:
        labels = self.support_labels()
        if i == 0:
            return "Cantilever/free" if self.spans[0].left_condition == "Cantilever" else "Pin"
        if i == len(labels) - 1:
            return "Cantilever/free" if self.spans[-1].right_condition == "Cantilever" else "Pin"
        return "Pin — continuous"

    # -------------------- STEP 4/5: slab loading table --------------------
    def slab_loading_text(self) -> str:
        out = ["SLAB LOADING & FACTORING", "-" * 60]
        for i, p in self.panels.items():
            out.append(f"Panel {i}: Gk = {p.dead_kNm2(self.dc):.3f} kN/m^2   "
                        f"Qk = {p.live_kNm2_factored(self.dc):.3f} kN/m^2"
                        + ("  (incl. partition)" if p.has_partition else ""))
        return "\n".join(out)

    # -------------------- STEP 10: tabulated output --------------------
    def tabulate(self) -> str:
        labels = self.support_labels()
        out = []
        out.append("=" * 72)
        out.append("SPAN SUMMARY")
        out.append("=" * 72)
        for i, s in enumerate(self.spans):
            RL, RR = s.reactions()
            out.append(f"\nSpan {i+1}  ({labels[i]} -> {labels[i+1]}, L = {s.length_m:.3f} m)"
                        f"   [{s.left_condition}/{s.right_condition}]")
            if s.self_weight_kNm:
                out.append(f"  Self-weight  : {s.self_weight_kNm:.3f} kN/m (included in Gk below)")
            for pos, g in s.governing.items():
                others = [c for c in g['all_candidates'] if c[0] != g['panel_id']]
                note = f" (governs over panel(s) {', '.join(str(o[0]) for o in others)})" if others else ""
                out.append(f"  {pos:<14}: CRITICAL panel {g['panel_id']} -> "
                            f"Gk={g['dead']:.4f} kN/m  Qk={g['live']:.4f} kN/m{note}")
            out.append(f"  UDL (total)  : Gk = {s.udl_dead:.3f} kN/m   Qk = {s.udl_live:.3f} kN/m")
            for p in s.point_loads:
                out.append(f"  {p.label:<10}  : Gk = {p.total_dead_kN:.2f} kN   Qk = {p.live_kN:.2f} kN"
                            f"   @ {p.position_m:.2f} m from {labels[i]}")
            out.append(f"  Reaction {labels[i]:<3}  : Gk = {RL['dead']:.3f} kN   Qk = {RL['live']:.3f} kN")
            out.append(f"  Reaction {labels[i+1]:<3}  : Gk = {RR['dead']:.3f} kN   Qk = {RR['live']:.3f} kN")

        out.append("\n" + "=" * 72)
        out.append("TOTAL SUPPORT REACTIONS  (combining shared supports)")
        out.append("=" * 72)
        totals = self.support_reactions()
        out.append(f"{'Support':<10}{'Condition':<20}{'Gk (kN)':>10}{'Qk (kN)':>10}{'Total (kN)':>12}")
        gtot_d = gtot_l = 0.0
        for i, lbl in enumerate(labels):
            d, l = totals[lbl]['dead'], totals[lbl]['live']
            gtot_d += d; gtot_l += l
            out.append(f"{lbl:<10}{self.support_condition_label(i):<20}{d:>10.3f}{l:>10.3f}{d+l:>12.3f}")
        out.append("-" * 62)
        out.append(f"{'Beam Total':<30}{gtot_d:>10.3f}{gtot_l:>10.3f}{gtot_d+gtot_l:>12.3f}")
        return "\n".join(out)

    # -------------------- STEP 11: graphical output --------------------
    def figure(self):
        """Standalone matplotlib Figure/Axes for the beam diagram (live vector plot)."""
        Ltot = sum(s.length_m for s in self.spans)
        W = max(7.0, Ltot * 1.7)
        fig, ax = plt.subplots(figsize=(W, 6.6))
        self._draw_diagram(ax, W, title=True)
        fig.tight_layout()
        return fig, ax

    def _draw_diagram(self, ax, width_in, title=True):
        """Draws the load & reaction diagram on `ax` (width_in = drawn width in inches).
        Supports are small upward arrows with the reaction R (support letter as a
        subscript), nG_k and nQ_k written directly beneath. Dimension lines: one line
        (spans) when there are no point loads; two tiers (point-load positions, then
        spans) when there are. UDL blocks have no arrows."""
        labels = self.support_labels()
        cum = [0.0]
        for s in self.spans:
            cum.append(cum[-1] + s.length_m)
        Ltot = cum[-1]
        totals = self.support_reactions()
        n_spans = len(self.spans)
        has_pl = any(s.point_loads for s in self.spans)

        # stepped UDL blocks: each span's block height reflects its own nG_k + nQ_k
        mags = [s.udl_dead + s.udl_live for s in self.spans]
        max_mag = max(mags) if mags and max(mags) > 0 else 1.0
        block_h = [0.6 + 1.8 * (m / max_mag) for m in mags]

        min_span = min([s.length_m for s in self.spans] + [Ltot]) if n_spans else 1.0
        in_per_m = width_in / (Ltot + 1.6)
        fscale = max(0.72, min(1.0, min_span * in_per_m / 1.3))
        fs_r = max(5.5, min(8.5, 8.5 * min_span * in_per_m / 1.35))

        ARROW_L = 0.5
        y_text = -ARROW_L - 0.1
        y_dim1 = y_text - 3 * 0.3 - 0.3
        y_dim2 = y_dim1 - 0.42
        y_last = y_dim2 if has_pl else y_dim1

        ax.set_xlim(-0.8, Ltot + 0.8)
        hs = [1.15, 1.7, 2.25, 2.8]
        top = max([block_h[i] + (max(hs[j % 4] for j in range(len(s.point_loads))) + 0.8
                                 if s.point_loads else 0.9)
                   for i, s in enumerate(self.spans)] + [1.5])
        ax.set_ylim(y_last - 0.4, top + 0.3)
        ax.axis('off')

        BEAM_Y, BEAM_H = 0, max(0.012 * Ltot, 0.025)
        ax.add_patch(patches.Rectangle((0, BEAM_Y - BEAM_H / 2), Ltot, BEAM_H,
                                        facecolor='#F2C9CE', edgecolor='black', zorder=6))

        # ---- supports: small upward arrows + reaction text beneath ----
        for i, lbl in enumerate(labels):
            x = cum[i]
            free = ((i == 0 and self.spans[0].left_condition == "Cantilever") or
                    (i == len(labels) - 1 and self.spans[-1].right_condition == "Cantilever"))
            if not free:
                ax.annotate('', xy=(x, -BEAM_H / 2), xytext=(x, -ARROW_L), zorder=7,
                            arrowprops=dict(arrowstyle='-|>', lw=1.7, color='black', mutation_scale=11))
            ax.text(x, y_text, f"$R_{{{lbl}}}$", ha='center', va='top', fontsize=fs_r + 1.5,
                    fontweight='bold')
            if free:
                ax.text(x, y_text - 0.34, "free end", ha='center', va='top', fontsize=fs_r - 0.5,
                        color='#777777')
            else:
                ax.text(x, y_text - 0.34, f"{sub('nGk')} = {totals[lbl]['dead']:.2f} kN",
                        ha='center', va='top', fontsize=fs_r, color='#1F4E78')
                ax.text(x, y_text - 0.64, f"{sub('nQk')} = {totals[lbl]['live']:.2f} kN",
                        ha='center', va='top', fontsize=fs_r, color='#1F4E78')

        # ---- stepped UDL blocks + point loads, per span ----
        for i, s in enumerate(self.spans):
            x0, x1 = cum[i], cum[i + 1]
            h = block_h[i]
            ax.add_patch(patches.Rectangle((x0, BEAM_Y + BEAM_H / 2), x1 - x0, h,
                                            facecolor='#DCE8F5', edgecolor='#1F4E78', lw=1.3, zorder=2))
            # keep the UDL label clear of point-load arrows: use the widest free stretch
            hw = 0.62 * fscale / in_per_m
            cuts = [x0] + sorted(x0 + p.position_m for p in s.point_loads) + [x1]
            xlab = (x0 + x1) / 2
            if any(abs(c - xlab) < hw for c in cuts[1:-1]):
                a_, b_ = max(zip(cuts[:-1], cuts[1:]), key=lambda t: t[1] - t[0])
                xlab = (a_ + b_) / 2
            ax.text(xlab, h + 0.08,
                    f"{sub('nGk')} = {s.udl_dead:.2f} kN/m\n{sub('nQk')} = {s.udl_live:.2f} kN/m",
                    ha='center', va='bottom', fontsize=8.8 * fscale, color='#1F4E78', fontweight='bold')
            heights = [1.15, 1.7, 2.25, 2.8]
            for j, p in enumerate(s.point_loads):
                xp = x0 + p.position_m
                ph = h + heights[j % len(heights)]
                ax.annotate('', xy=(xp, BEAM_Y + BEAM_H / 2 + 0.02), xytext=(xp, ph),
                            arrowprops=dict(arrowstyle='-|>', lw=2.1, color='#8B1E2E', mutation_scale=16))
                ax.text(xp, ph + 0.06, f"{p.label}: {sub('Gk')}={p.total_dead_kN:.1f} {sub('Qk')}={p.live_kN:.1f} kN",
                        ha='center', va='bottom', fontsize=7.6 * fscale, color='#8B1E2E', fontweight='bold')

        # ---- dimension lines ----
        def dim_line(x0, x1, y, text, color='#333333'):
            ax.annotate('', xy=(x1, y), xytext=(x0, y),
                        arrowprops=dict(arrowstyle='<->', lw=0.8, color=color))
            ax.plot([x0, x0], [y - 0.07, y + 0.07], color=color, lw=0.8)
            ax.plot([x1, x1], [y - 0.07, y + 0.07], color=color, lw=0.8)
            ax.text((x0 + x1) / 2, y - 0.1, text, ha='center', va='top', fontsize=7.6 * fscale, color=color)

        if has_pl:
            # Tier 1: positions of the point loads measured from the supports
            points_x = sorted(set([round(c, 6) for c in cum] +
                                  [round(cum[i] + p.position_m, 6) for i, s in enumerate(self.spans)
                                   for p in s.point_loads]))
            for a, b in zip(points_x[:-1], points_x[1:]):
                dim_line(a, b, y_dim1, f"{(b - a):.2f} m")
            # Tier 2: beam spans
            for i, s in enumerate(self.spans):
                dim_line(cum[i], cum[i + 1], y_dim2, f"Span {i+1} = {s.length_m:.2f} m", color='#1F4E78')
        else:
            for i, s in enumerate(self.spans):
                dim_line(cum[i], cum[i + 1], y_dim1, f"Span {i+1} = {s.length_m:.2f} m", color='#1F4E78')

        if title:
            ax.set_title("Beam — Load & Reaction Diagram", fontsize=14, fontweight='bold', pad=14)


    def plot(self, filename: str = "beam_diagram.svg") -> str:
        """Saves the diagram to a file. Use an .svg (default) or .pdf extension
        for true vector output, or .png if you specifically need a raster image."""
        fig, ax = self.figure()
        fig.savefig(filename, facecolor='white')
        plt.close(fig)
        return filename


    # -------------------- STEP 12: PDF report (calculation-sheet style) --------------------
    def generate_pdf(self, filename: str = "beam_report.pdf") -> str:
        """Calculation-sheet PDF: design criteria first, then slab loading, wall loading,
        then per-span panel sketches with worked calculations, support reactions with
        worked steps, totals and the load/reaction diagram. Design Ref on the left,
        Output on the right of each step, as on a hand calculation sheet."""
        import re
        import textwrap
        p, dc = self.project, self.dc
        BLUE = '#1F4E78'
        LEFT, RIGHT = 0.05, 0.95
        REF_W, OUT_W = 0.11, 0.14
        X0, X1 = LEFT + REF_W, RIGHT - OUT_W
        TOP, BOTTOM = 0.878, 0.05
        STEP = 0.0165
        PAGE_W_IN, PAGE_H_IN = 9.0, 12.4
        labels = self.support_labels()

        pages = []
        st = {"fig": None, "ax": None, "y": TOP}

        # ---- title block geometry (drawn at the top of EVERY page) ----
        ROWS = [0.986, 0.958, 0.934, 0.910, 0.890]
        XL = LEFT + 0.22
        XA, XB = X0 - 0.008, X1 + 0.004
        logo_img = None
        try:
            logo_path = p.logo_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
            logo_img = plt.imread(logo_path)
        except Exception:
            logo_img = None

        def new_page(columns=True):
            fig = plt.figure(figsize=(PAGE_W_IN, PAGE_H_IN))
            ax = fig.add_axes([0, 0, 1, 1])
            ax.axis('off'); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_autoscale_on(False)

            def box(x0, y0, x1, y1):
                ax.add_patch(patches.Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor='none',
                                                edgecolor='black', lw=0.9, zorder=3))

            rend = fig.canvas.get_renderer()
            inv = ax.transData.inverted()

            def fit_text(t, maxw, min_fs=5.0):
                """Shrink (then truncate with an ellipsis) so the text never leaves its cell."""
                def w_():
                    bb = t.get_window_extent(rend)
                    return inv.transform((bb.x1, 0))[0] - inv.transform((bb.x0, 0))[0]
                while w_() > maxw and t.get_fontsize() > min_fs:
                    t.set_fontsize(t.get_fontsize() - 0.5)
                txt = t.get_text()
                while w_() > maxw and len(txt) > 3:
                    txt = txt[:-2]
                    t.set_text(txt.rstrip() + "…")

            def cell(x0, y0, x1, y1, label, value, vfs=8, bold=True):
                box(x0, y0, x1, y1)
                ax.text(x0 + 0.004, y1 - 0.003, label, fontsize=5.8, fontweight='bold', color='#444444', va='top')
                t = ax.text((x0 + x1) / 2, y0 + (y1 - y0) * 0.36, value, fontsize=vfs, ha='center', va='center',
                            fontweight='bold' if bold else 'normal')
                fit_text(t, (x1 - x0) - 0.012)

            # logo cell
            box(LEFT, ROWS[3], XL, ROWS[0])
            if logo_img is not None:
                ww = 0.165
                hh = ww * PAGE_W_IN / PAGE_H_IN * logo_img.shape[0] / logo_img.shape[1]
                iax = ax.inset_axes([LEFT + 0.0275, ROWS[0] - 0.004 - hh, ww, hh])
                iax.imshow(logo_img); iax.axis('off')
                ty = ROWS[0] - 0.004 - hh - 0.003
            else:
                tt = ax.text((LEFT + XL) / 2, ROWS[0] - 0.006, p.firm_name, fontsize=8, fontweight='bold',
                             ha='center', va='top')
                fit_text(tt, (XL - LEFT) - 0.01)
                ty = ROWS[0] - 0.024
            for k, t in enumerate([p.address, f"Tel: {p.tel}" if p.tel else "",
                                   f"Email: {p.email}" if p.email else ""]):
                if t:
                    tt = ax.text((LEFT + XL) / 2, ty - k * 0.0085, t, fontsize=5.2, ha='center', va='top')
                    fit_text(tt, (XL - LEFT) - 0.01, min_fs=4.0)
            # title rows
            cell(XL, ROWS[1], 0.78, ROWS[0], "Project Title:", p.project_title or p.element, vfs=10)
            cell(0.78, ROWS[1], RIGHT, ROWS[0], "Job No:", p.job_no, vfs=8.5)
            cell(XL, ROWS[2], 0.62, ROWS[1], "By:", p.designer, vfs=8.5, bold=False)
            cell(0.62, ROWS[2], RIGHT, ROWS[1], "Checked by:", p.checked_by, vfs=8.5, bold=False)
            cell(XL, ROWS[3], 0.62, ROWS[2], "Calc:", (p.element or "Beam load analysis").upper(), vfs=8)
            cell(0.62, ROWS[3], 0.78, ROWS[2], "Date:", p.date, vfs=8, bold=False)
            cell(0.78, ROWS[3], RIGHT, ROWS[2], "Sheet No:", "", vfs=8)
            if columns:
                # column headings
                for (xa, xb, t) in ((LEFT, XA, "References"), (XA, XB, "Calculation"), (XB, RIGHT, "Output")):
                    box(xa, ROWS[4], xb, ROWS[3])
                    ax.text((xa + xb) / 2, (ROWS[3] + ROWS[4]) / 2, t, fontsize=8, fontweight='bold',
                            ha='center', va='center')
                # calculation area: outer border + column rules
                ax.add_patch(patches.Rectangle((LEFT, 0.04), RIGHT - LEFT, ROWS[4] - 0.04, facecolor='none',
                                                edgecolor='black', lw=0.9, zorder=3))
                ax.plot([XA, XA], [0.04, ROWS[4]], color='black', lw=0.7, zorder=3)
                ax.plot([XB, XB], [0.04, ROWS[4]], color='black', lw=0.7, zorder=3)
            else:
                # plain page (output diagram): outer border only, no inner rules or column headings
                ax.add_patch(patches.Rectangle((LEFT, 0.04), RIGHT - LEFT, ROWS[3] - 0.04, facecolor='none',
                                                edgecolor='black', lw=0.9, zorder=3))
            st.update(fig=fig, ax=ax, y=TOP, ref_y=2.0, out_y=2.0)
            pages.append(fig)

        def ensure_space(h):
            if st["y"] - h < BOTTOM:
                new_page()

        def chars_for(fs, width_frac):
            return max(20, int(width_frac * PAGE_W_IN / (0.56 * fs / 72.0)))

        def _rl(t):   # rendered length: a $...$ subscript group draws as ~1 character
            return len(re.sub(r'\$[^$]*\$', 'X', t))

        def wrap_out(out):
            """Wrap Output-column text so every line fits inside the column (~17 characters)."""
            res = []
            for ln in out.split("\n"):
                cur = ""
                for w in ln.split(" "):
                    cand = (cur + " " + w) if cur else w
                    if _rl(cand) <= 17 or not cur:
                        cur = cand
                    else:
                        res.append(cur)
                        cur = w
                res.append(cur)
            return res

        def fit(h, ref="", out=""):
            """Start a new page unless the text (h tall) AND its side notes fit above the bottom border."""
            n_o = len(wrap_out(sub(out))) if out else 0
            n_r = len(sub(ref).split("\n")) if ref else 0
            y = st["y"]
            lows = [y - h]
            if n_o:
                lows.append(min(y, st["out_y"]) - n_o * 0.0135)
            if n_r:
                lows.append(min(y, st["ref_y"]) - n_r * 0.0125)
            if min(lows) < BOTTOM:
                new_page()

        def side(ref="", out="", yy=None):
            # side notes never reserve vertical space; a note that would overlap the
            # previous one in its column is nudged down beneath it instead.
            ax, y = st["ax"], (st["y"] if yy is None else yy)
            ref, out = sub(ref), sub(out)
            if ref:
                yr = min(y, st["ref_y"])
                ax.text(LEFT + 0.004, yr, ref, fontsize=6.8, color='#666666', style='italic', va='top')
                st["ref_y"] = yr - (ref.count("\n") + 1) * 0.0125 - 0.004
            if out:
                lines_ = wrap_out(out)
                yo = min(y, st["out_y"])
                ax.text(X1 + 0.01, yo, "\n".join(lines_), fontsize=7.6, color=BLUE, fontweight='bold', va='top')
                st["out_y"] = yo - len(lines_) * 0.0135 - 0.004

        def line(txt, indent=0.0, bold=False, fs=7.8, ref="", out="", color='black'):
            txt = sub(txt)
            wrapped = textwrap.wrap(txt, chars_for(fs, X1 - X0 - indent), subsequent_indent="    ",
                                    break_long_words=False, break_on_hyphens=False) or [""]
            need = len(wrapped) * STEP
            fit(need, ref, out)
            ax = st["ax"]
            side(ref, out)
            for k, w in enumerate(wrapped):
                ax.text(X0 + indent, st["y"] - k * STEP, w, fontsize=fs, va='top', color=color,
                        fontweight='bold' if bold else 'normal')
            st["y"] -= len(wrapped) * STEP

        def heading(txt, ref="", out=""):
            fit(STEP * 2, ref, out)
            ax = st["ax"]
            side(ref, out)
            txt = sub(txt)
            ax.text(X0, st["y"], txt, fontsize=8.6, fontweight='bold', va='top')
            wline = min(X1 - X0, len(txt.replace("$", "")) * 0.0068)
            ax.plot([X0, X0 + wline], [st["y"] - 0.0135] * 2, color='black', lw=0.7)
            st["y"] -= STEP * 1.25

        def gap(k=0.5):
            st["y"] -= STEP * k

        def section_title(text):
            st["y"] = min(st["y"], st["out_y"], st["ref_y"])   # clear any tall side note first
            ensure_space(0.06)
            ax = st["ax"]
            ax.add_patch(patches.Rectangle((LEFT, st["y"] - 0.022), RIGHT - LEFT, 0.028, facecolor=BLUE,
                                            edgecolor='none', zorder=1))
            ax.text(0.5, st["y"] - 0.008, text, fontsize=10.5, fontweight='bold', color='white',
                    ha='center', va='center', zorder=2)
            st["y"] -= 0.042

        def table(headers, rows, col_fracs, row_h=0.021, fontsize=7.3, ref="", output=""):
            headers = [sub(h) for h in headers]
            rows = [[sub(str(c)) for c in r] for r in rows]
            n_rows = len(rows) + 1
            height = row_h * n_rows
            fit(height + 0.01, ref, output)
            ax = st["ax"]
            y0 = st["y"] - height
            side(ref, output)
            tbl = ax.table(cellText=rows, colLabels=headers, bbox=[X0, y0, X1 - X0, height],
                           colWidths=col_fracs, cellLoc='center')
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(fontsize)
            for j in range(len(headers)):
                c = tbl[0, j]
                c.set_facecolor(BLUE); c.get_text().set_color('white')
                c.get_text().set_fontweight('bold'); c.get_text().set_fontsize(fontsize)
            for (r, c), cell in tbl.get_celld().items():
                cell.set_edgecolor('#AAAAAA'); cell.set_linewidth(0.5)
            st["y"] = y0 - 0.012

        def sketch_block(panel, lines, ref="", out=""):
            """Panel sketch on the left, worked calculation lines to its right."""
            SK_H = 0.138
            SK_W = SK_H * PAGE_H_IN / PAGE_W_IN
            tx = X0 + SK_W + 0.015
            cw = chars_for(7.6, X1 - tx)
            wrapped = []
            for ln in lines:
                wrapped += textwrap.wrap(sub(ln), cw, subsequent_indent="   ", break_long_words=False,
                                         break_on_hyphens=False) or [""]
            h = max(SK_H, len(wrapped) * STEP) + 0.01
            fit(h, ref, out)
            ax = st["ax"]
            side(ref, out)
            sax = ax.inset_axes([X0, st["y"] - SK_H, SK_W, SK_H])
            draw_panel_sketch_ax(sax, panel)
            for k, w in enumerate(wrapped):
                ax.text(tx, st["y"] - k * STEP, w, fontsize=7.6, va='top',
                        fontweight='bold' if k == 0 else 'normal')
            st["y"] -= h

        def type_txt(label, value, options):
            """'  (Residential Areas, Screed and Tiles)' — the type of load used. Falls back to a
            lookup by value only when that is unambiguous."""
            if not label:
                hits = [lb for lb, v in options.values() if v == value]
                label = hits[0] if len(hits) == 1 else ""
            if not label:
                return ""
            return "   (" + label.replace(" (", ", ").replace(")", "") + ")"

        def num_of(txt):
            m = re.search(r'\d+(\.\d+)?', str(txt))
            return m.group(0) if m else None

        new_page()

        # ================= Element title (bold, underlined) =================
        ttl = p.drawing_title()
        if ttl:
            axp, fig_ = st["ax"], st["fig"]
            rend = fig_.canvas.get_renderer()
            inv = axp.transData.inverted()
            maxw = (X1 - X0) - 0.02

            def extent(txt, fs):
                t_ = axp.text(0, 0, txt, fontsize=fs, fontweight='bold')
                bb = t_.get_window_extent(rend)
                t_.remove()
                return inv.transform((bb.x0, bb.y0))[0], inv.transform((bb.x1, bb.y1))[0]

            width_of = lambda txt, fs: (lambda e: e[1] - e[0])(extent(txt, fs))
            title_lines, fs_t = [ttl], None
            for fs in (10.5, 10, 9.5, 9, 8.5):          # one line if it fits at a sensible size
                if width_of(ttl, fs) <= maxw:
                    fs_t = fs
                    break
            if fs_t is None:                            # otherwise two balanced lines, shrunk to fit
                fs_t = 9.5
                title_lines = textwrap.wrap(ttl, width=len(ttl) // 2 + 6, break_long_words=False)
                while max(width_of(l_, fs_t) for l_ in title_lines) > maxw and fs_t > 6.5:
                    fs_t -= 0.5
            ensure_space(len(title_lines) * 0.026 + 0.03)
            xc = (X0 + X1) / 2
            for l_ in title_lines:
                axp.text(xc, st["y"], l_, fontsize=fs_t, fontweight='bold', ha='center', va='top')
                ux0, ux1 = extent(l_, fs_t)
                axp.plot([xc - (ux1 - ux0) / 2, xc + (ux1 - ux0) / 2], [st["y"] - 0.0145] * 2,
                         color='black', lw=1.0)
                st["y"] -= 0.026
            st["y"] -= 0.014

        # ================= DESIGN CRITERIA (first) =================
        section_title("DESIGN CRITERIA & MATERIALS")
        fcu = num_of(dc.concrete_grade)
        fy = "460" if "high" in dc.steel_grade.lower() else None
        line(f"fcu = {fcu} N/mm²   ({dc.concrete_grade})" if fcu else f"Concrete: {dc.concrete_grade}",
             ref="BS 8110-1:1997\nTables 3.3 & 3.4")
        line(f"fy = fyv = {fy} N/mm²   ({dc.steel_grade})" if fy else f"Reinforcement: {dc.steel_grade}")
        line(f"Exposure condition = {dc.exposure_condition}")
        line(f"Fire resistance = {dc.fire_resistance_hours:g} hours")
        line(f"Cover = {dc.concrete_cover_mm:.0f} mm", out=f"Provide {dc.concrete_cover_mm:.0f} mm\ncover")
        gap(0.6)
        rows = [
            ["Concrete density", f"{dc.concrete_density:.0f}", "kN/m³"],
            ["Wall material density", f"{dc.wall_density:.0f}", "kN/m³"],
            ["Finishes — " + FINISHES_OPTIONS['1'][0], f"{FINISHES_OPTIONS['1'][1]}", "kN/m²"],
            ["Finishes — " + FINISHES_OPTIONS['2'][0], f"{FINISHES_OPTIONS['2'][1]}", "kN/m²"],
            *[[f"Live load — {lb}", f"{v}", "kN/m²"] for lb, v in LIVE_LOAD_OPTIONS.values()],
            ["Load factor: self-weight / partitions / wall", f"{dc.factor_selfweight_partition}", "-"],
            ["Load factor: finishes", f"{dc.factor_finishes}", "-"],
            ["Load factor: live", f"{dc.factor_live}", "-"],
        ]
        table(["Parameter", "Value", "Unit"], rows, [0.62, 0.2, 0.18], row_h=0.019,
              ref="BS 6399-1:1996\nTable 1\nEN 1990-1:2002\nTable A1.2(B)")

        # ================= Beam layout =================
        section_title("BEAM LAYOUT")
        has_pl = any(sp.point_loads for sp in self.spans)
        ensure_space(0.20)
        ax = st["ax"]
        cum = [0.0]
        for sp in self.spans:
            cum.append(cum[-1] + sp.length_m)
        Ltot = cum[-1] or 1.0
        xo = lambda m: X0 + 0.02 + (m / Ltot) * (X1 - X0 - 0.04)
        yb = st["y"] - 0.085
        ax.plot([xo(0), xo(Ltot)], [yb, yb], color='black', lw=2.2)
        for k, lbl in enumerate(labels):
            xk = xo(cum[k])
            free = (k == 0 and self.spans[0].left_condition == "Cantilever") or \
                   (k == len(labels) - 1 and self.spans[-1].right_condition == "Cantilever")
            if free:
                ax.plot(xk, yb, marker='o', ms=5, color='white', markeredgecolor='black', zorder=5)
            else:
                ax.add_patch(patches.Polygon([(xk - 0.007, yb - 0.014), (xk + 0.007, yb - 0.014), (xk, yb)],
                                              closed=True, facecolor='white', edgecolor='black', zorder=5))
            ax.text(xk, yb - 0.016, lbl, ha='center', va='top', fontsize=9, fontweight='bold')
        for i, sp in enumerate(self.spans):
            xm = xo((cum[i] + cum[i + 1]) / 2)
            # panel whose bottom/right edge is on the beam lies ABOVE / LEFT of it -> number on top;
            # panel whose top/left edge is on the beam lies BELOW / RIGHT of it -> number just under the beam line
            top_ids = list(dict.fromkeys(c.panel_id for c in sp.contributions if c.edge in ("bottom", "right")))
            bot_ids = list(dict.fromkeys(c.panel_id for c in sp.contributions if c.edge in ("top", "left")))
            cuts = [xo(cum[i])] + sorted(xo(cum[i] + pl.position_m) for pl in sp.point_loads) + [xo(cum[i + 1])]
            xt = xm
            if any(abs(c_ - xm) < 0.035 for c_ in cuts[1:-1]):
                a_, b_ = max(zip(cuts[:-1], cuts[1:]), key=lambda t_: t_[1] - t_[0])
                xt = (a_ + b_) / 2
            if top_ids:
                ax.text(xt, yb + 0.014, ", ".join(top_ids), ha='center', va='bottom', fontsize=7.5,
                        color=BLUE, fontweight='bold')
            if bot_ids:
                ax.text(xm, yb - 0.006, ", ".join(bot_ids), ha='center', va='top', fontsize=7.5,
                        color=BLUE, fontweight='bold')
            for pl in sp.point_loads:
                xp = xo(cum[i] + pl.position_m)
                ax.annotate('', xy=(xp, yb + 0.003), xytext=(xp, yb + 0.062),
                            arrowprops=dict(arrowstyle='-|>', lw=1.6, color='#8B1E2E', mutation_scale=11))
                ax.text(xp, yb + 0.064, pl.label, ha='center', va='bottom', fontsize=7.2,
                        color='#8B1E2E', fontweight='bold')

        def dim(x0_, x1_, y_, text, color):
            ax.annotate('', xy=(x1_, y_), xytext=(x0_, y_), arrowprops=dict(arrowstyle='<->', lw=0.7, color=color))
            ax.plot([x0_, x0_], [y_ - 0.004, y_ + 0.004], color=color, lw=0.7)
            ax.plot([x1_, x1_], [y_ - 0.004, y_ + 0.004], color=color, lw=0.7)
            ax.text((x0_ + x1_) / 2, y_ - 0.005, text, ha='center', va='top', fontsize=7, color=color)

        y1 = yb - 0.047
        if has_pl:     # tier 1: point-load positions from the supports; tier 2: spans
            pts = sorted(set([round(c_, 6) for c_ in cum] +
                             [round(cum[i] + pl.position_m, 6) for i, sp in enumerate(self.spans)
                              for pl in sp.point_loads]))
            for a_, b_ in zip(pts[:-1], pts[1:]):
                dim(xo(a_), xo(b_), y1, f"{b_ - a_:.2f} m", '#333333')
            y2 = y1 - 0.028
        else:          # no point loads: a single dimension line for the spans
            y2 = y1
        for i, sp in enumerate(self.spans):
            narrow = (xo(cum[i + 1]) - xo(cum[i])) < 0.13
            dim(xo(cum[i]), xo(cum[i + 1]), y2,
                (f"S{i+1} = {sp.length_m:.2f} m" if narrow else f"Span {i+1} = {sp.length_m:.2f} m"), BLUE)
        st["y"] = y2 - 0.035

        # ================= SLAB LOADING (grouped) =================
        section_title("SLAB LOADING")
        groups = {}
        for pid, pn in self.panels.items():
            key = (pn.thickness_mm, pn.finishes_kNm2, pn.live_kNm2, pn.finishes_label, pn.live_label,
                   pn.has_partition,
                   (pn.partition_len_m, pn.partition_thk_m, pn.partition_ht_m, pn.partition_floor_ht_m,
                    pn.partition_depth_m, pn.ly_m, pn.lx_m)
                   if pn.has_partition else None)
            groups.setdefault(key, []).append(pid)
        for key, ids in groups.items():
            pn = self.panels[ids[0]]
            heading("Panel" + ("s " if len(ids) > 1 else " ") + ", ".join(str(i) for i in ids))
            sw, part = pn.self_weight_kNm2(dc), pn.partition_kNm2(dc)
            G, Q = pn.dead_kNm2(dc), pn.live_kNm2_factored(dc)
            line("Dead loads, Gk:", bold=True, ref="BS 6399-1:1996\nTable 1",
                 out=("Panel" + ("s " if len(ids) > 1 else " ") + ", ".join(str(i) for i in ids)
                      + f":\nnGk = {G:.2f} kN/m²\nnQk = {Q:.2f} kN/m²"))
            line(f"Self weight = {pn.thickness_mm/1000:g} × {dc.concrete_density:g} = {sw:.2f} kN/m²", indent=0.06)
            line(f"Finishes = {pn.finishes_kNm2:g} kN/m²{type_txt(pn.finishes_label, pn.finishes_kNm2, FINISHES_OPTIONS)}",
                 indent=0.06)
            if pn.has_partition:
                if pn.partition_floor_ht_m > 0:
                    line(f"Partition wall height = floor height − slab depth = {pn.partition_floor_ht_m:g} − "
                         f"{pn.partition_depth_m:g} = {pn.partition_ht_m:.2f} m", indent=0.06)
                line(f"Partitions = ({pn.partition_len_m:g} × {pn.partition_thk_m:g} × {pn.partition_ht_m:g}"
                     f" × {dc.wall_density:g}) / ({pn.ly_m:g} × {pn.lx_m:g}) = {part:.2f} kN/m²", indent=0.06)
            else:
                line("Partitions = 0 kN/m²", indent=0.06)
            line(f"Live loads, Qk = {pn.live_kNm2:g} kN/m²{type_txt(pn.live_label, pn.live_kNm2, LIVE_LOAD_OPTIONS)}",
                 bold=True)
            line("Factored loads:", bold=True, ref="EN 1990-1:2002\nTable A1.2(B)")
            line(f"nGk = {dc.factor_selfweight_partition:g}({sw:.2f} + {part:.2f}) + "
                 f"{dc.factor_finishes:g}({pn.finishes_kNm2:g}) = {G:.2f} kN/m²", indent=0.06)
            line(f"nQk = {dc.factor_live:g} × {pn.live_kNm2:g} = {Q:.2f} kN/m²", indent=0.06)
            gap(0.7)

        # ================= WALL LOADING =================
        any_wall = any(s.wall_present for s in self.spans)
        if any_wall:
            heading("Wall loading:")
            wl = self.wall
            line(f"Floor height = {wl.floor_height_m:g} m,   beam depth = {wl.depth_m:g} m", indent=0.06,
                 ref="BS 6399-1:1996\nTable 1")
            line(f"Wall height = floor height − beam depth = {wl.floor_height_m:g} − {wl.depth_m:g} = "
                 f"{wl.height_m:.2f} m", indent=0.06)
            line(f"Thickness = {wl.thickness_m:g} m,   γ = {dc.wall_density:g} kN/m³", indent=0.06)
            line(f"nGk = {dc.factor_selfweight_partition:g} × {wl.thickness_m:g} × {wl.height_m:.2f} × "
                 f"{dc.wall_density:g} = {wl.factored_dead_kNm(dc):.2f} kN/m", indent=0.06,
                 out=f"Wall loading:\nnGk = {wl.factored_dead_kNm(dc):.2f} kN/m")
            gap(0.7)

        # ================= PER-SPAN LOAD DISTRIBUTION =================
        for i, s in enumerate(self.spans):
            ensure_space(0.22)
            section_title(f"SPAN {i+1}   ({labels[i]} → {labels[i+1]},  L = {s.length_m:.3f} m,  "
                          f"{s.left_condition}/{s.right_condition})")
            for c in s.contributions:
                pn = self.panels[c.panel_id]
                sketch_block(pn, pn.describe_bearing(c.edge),
                             ref="BS 8110-1:1997\nTable 3.15" if pn.slab_type == "solid" else "")
            if not s.contributions:
                line("No panels bear on this span.")

            # ---- dead loads ----
            line("Dead loads:", bold=True)
            gov_parts, gov_live_parts = [], []
            for c in s.contributions:
                pn = self.panels[c.panel_id]
                f, w, G, Q = pn.distribution_factor(c.edge), pn.load_width_m(), pn.dead_kNm2(dc), pn.live_kNm2_factored(dc)
                g = s.governing[c.edge]
                many = len(g['all_candidates']) > 1
                tag = ("   (critical)" if g['panel_id'] == c.panel_id else "   (not critical)") if many else ""
                line(f"Panel {c.panel_id}:  {f:.3f} × {G:.2f} × {w:.3f} = {f*G*w:.2f} kN/m{tag}", indent=0.06)
            for edge, g in s.governing.items():
                gov_parts.append(f"{g['dead']:.2f}")
                gov_live_parts.append(f"{g['live']:.2f}")
            extras = []
            if s.wall_present:
                extras.append(f"{self.wall.factored_dead_kNm(dc):.2f}")
                line(f"Wall loading = {self.wall.factored_dead_kNm(dc):.2f} kN/m", indent=0.06)
            if s.self_weight_kNm:
                extras.append(f"{s.self_weight_kNm:.2f}")
                line(f"Element self-weight = {s.self_weight_kNm:.2f} kN/m", indent=0.06)
            parts = gov_parts + extras
            line(f"nGk = {' + '.join(parts) if parts else '0'} = {s.udl_dead:.2f} kN/m", bold=True, indent=0.06,
                 out=f"Span {i+1}:\nnGk = {s.udl_dead:.2f} kN/m\nnQk = {s.udl_live:.2f} kN/m")
            # ---- live loads ----
            line("Live loads:", bold=True)
            for c in s.contributions:
                pn = self.panels[c.panel_id]
                f, w, Q = pn.distribution_factor(c.edge), pn.load_width_m(), pn.live_kNm2_factored(dc)
                g = s.governing[c.edge]
                many = len(g['all_candidates']) > 1
                tag = ("   (critical)" if g['panel_id'] == c.panel_id else "   (not critical)") if many else ""
                line(f"Panel {c.panel_id}:  {f:.3f} × {Q:.2f} × {w:.3f} = {f*Q*w:.2f} kN/m{tag}", indent=0.06)
            line(f"nQk = {' + '.join(gov_live_parts) if gov_live_parts else '0'} = {s.udl_live:.2f} kN/m",
                 bold=True, indent=0.06)
            # ---- point loads ----
            for pl in s.point_loads:
                if pl.self_weight_kN:
                    line(f"Point load {pl.label}: self-weight of element = {pl.sw_factor:g} × "
                         f"{pl.elem_b_m:g} × {pl.elem_h_m:g} × {pl.elem_len_m:g} × {pl.density_kNm3:g} "
                         f"= {pl.self_weight_kN:.2f} kN")
                    line(f"nGk = {pl.dead_kN:.2f} + {pl.self_weight_kN:.2f} = {pl.total_dead_kN:.2f} kN",
                         indent=0.06)
                line(f"Point load {pl.label}:  nGk = {pl.total_dead_kN:.2f} kN,  nQk = {pl.live_kN:.2f} kN  "
                     f"at {pl.position_m:.3f} m from {labels[i]}", bold=True,
                     out=f"Point load {pl.label}:\nnGk = {pl.total_dead_kN:.2f} kN\nnQk = {pl.live_kN:.2f} kN")
            gap(0.8)

        # ================= REACTIONS =================
        section_title("SUPPORT REACTIONS")
        totals = self.support_reactions()
        for k, lbl in enumerate(labels):
            for kind, nm in (("dead", "nGk"), ("live", "nQk")):
                terms = []
                if k < len(self.spans):
                    terms += self.spans[k].reaction_terms('L', kind)
                if k > 0:
                    terms += self.spans[k - 1].reaction_terms('R', kind)
                total = sum(v for _, v in terms)
                expr = " + ".join(e for e, _ in terms) if terms else "0"
                out = ""
                if kind == "dead":
                    out = f"$R_{{{lbl}}}$:\nnGk = {totals[lbl]['dead']:.2f} kN\nnQk = {totals[lbl]['live']:.2f} kN"
                line(f"$R_{{{lbl}}}$, {nm} = {expr} = {total:.2f} kN", indent=0.02, out=out)
            gap(0.4)

        # ================= TOTALS =================
        section_title("TOTAL SUPPORT REACTIONS")
        rows = []
        for idx, lbl in enumerate(labels):
            d, l = totals[lbl]['dead'], totals[lbl]['live']
            rows.append([lbl, self.support_condition_label(idx), f"{d:.2f}", f"{l:.2f}"])
        table(["Support", "Condition", "Dead nGk (kN)", "Live nQk (kN)"], rows,
              [0.2, 0.34, 0.23, 0.23],
              ref="Shared supports\ncombine reactions\nfrom both spans.")

        # ================= OUTPUT DIAGRAM (own page: no inner borders, centred; rotated if long) =================
        import io
        import numpy as np
        new_page(columns=False)
        area_top, area_bot = ROWS[3], 0.04
        ymid = (area_top + area_bot) / 2
        Ltot_ = sum(sp.length_m for sp in self.spans)
        min_span_ = min([sp.length_m for sp in self.spans] + [Ltot_])
        portrait_w_in = (RIGHT - LEFT - 0.04) * PAGE_W_IN
        too_long = min_span_ * portrait_w_in / (Ltot_ + 1.6) < 1.15
        if not too_long:
            DH = 4.4 / PAGE_H_IN
            dw = RIGHT - LEFT - 0.04
            st["ax"].text(0.5, area_top - 0.025, "OUTPUT — LOAD & REACTION DIAGRAM", fontsize=11,
                          fontweight='bold', ha='center', va='center', color=BLUE)
            dax = st["ax"].inset_axes([(1 - dw) / 2, ymid - DH / 2 + 0.018, dw, DH])
            self._draw_diagram(dax, dw * PAGE_W_IN, title=False)
        else:
            # long beam: draw the diagram landscape, rotate it 90 degrees and centre it on the page
            LEN_IN, SHORT_IN = 9.8, 6.0
            tf = plt.figure(figsize=(LEN_IN, SHORT_IN))
            tax = tf.add_axes([0.02, 0.02, 0.96, 0.88])
            self._draw_diagram(tax, LEN_IN * 0.96, title=True)
            buf = io.BytesIO()
            tf.savefig(buf, format='png', dpi=300, facecolor='white')
            plt.close(tf)
            buf.seek(0)
            img = np.rot90(plt.imread(buf, format='png'))
            w_, h_ = SHORT_IN / PAGE_W_IN, LEN_IN / PAGE_H_IN
            iax = st["ax"].inset_axes([(1 - w_) / 2, ymid - h_ / 2, w_, h_])
            iax.imshow(img, aspect='auto', interpolation='lanczos')
            iax.axis('off')

        with PdfPages(filename) as pdf:
            for n, f in enumerate(pages, 1):
                f.axes[0].text((0.78 + RIGHT) / 2, ROWS[3] + 0.0075, f"{n} of {len(pages)}", fontsize=8.5,
                               fontweight='bold', ha='center', va='center')
                pdf.savefig(f)
                plt.close(f)
        return filename
