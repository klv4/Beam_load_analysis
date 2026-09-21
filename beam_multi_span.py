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
from typing import List, Dict, Tuple, Optional
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.backends.backend_pdf import PdfPages


# ============================================================
# STEP 1 — Project information
# ============================================================
@dataclass
class ProjectInfo:
    firm_name: str = "Horicon Engineering Solutions"
    address: str = ""
    job_no: str = ""
    calc_sheet_no: str = ""
    designer: str = ""
    date: str = ""
    revision: str = ""
    element: str = ""              # e.g. "Second Floor Beam SF9"
    beam_type: str = "Simply Supported"
    location: str = ""             # e.g. "Along Grid 3/A-C"
    material: str = ""             # e.g. "Beam 9"


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
# STEP 3/4 — slab panels
# ============================================================
@dataclass
class SlabPanel:
    panel_id: int
    thickness_mm: float
    ly_m: float
    lx_m: float
    finishes_kNm2: float
    live_kNm2: float
    has_partition: bool = False
    partition_len_m: float = 0.0
    partition_thk_m: float = 0.0
    partition_ht_m: float = 0.0

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
# STEP 6 — wall loading (direct wall bearing on the beam)
# ============================================================
@dataclass
class WallLoad:
    thickness_m: float = 0.2
    height_m: float = 2.7

    def factored_dead_kNm(self, dc: DesignCriteria) -> float:
        return dc.factor_selfweight_partition * (self.thickness_m * self.height_m * dc.wall_density)


# ============================================================
# STEP 7 — which panels touch which span, and how
# ============================================================
@dataclass
class PanelContribution:
    panel_id: int
    position: str              # e.g. "Left/Top" or "Right/Bottom"
    distribution_factor: float


# ============================================================
# STEP 8 — point loads
# ============================================================
@dataclass
class PointLoad:
    label: str
    dead_kN: float
    live_kN: float
    position_m: float          # measured from this span's LEFT support


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

    def compute_udl(self, panels: Dict[int, SlabPanel], wall: WallLoad, dc: DesignCriteria):
        rows = []
        candidates: Dict[str, List[Tuple[int, float, float]]] = {}
        for c in self.contributions:
            p = panels[c.panel_id]
            dead = c.distribution_factor * p.lx_m * p.dead_kNm2(dc)
            live = c.distribution_factor * p.lx_m * p.live_kNm2_factored(dc)
            rows.append((c.panel_id, c.position, dead, live))
            candidates.setdefault(c.position, []).append((c.panel_id, dead, live))

        gov_dead_total, gov_live_total = 0.0, 0.0
        self.governing = {}
        for position, opts in candidates.items():
            # governing (critical) panel = the one with the larger DEAD load on this side
            crit = max(opts, key=lambda o: o[1])
            self.governing[position] = dict(panel_id=crit[0], dead=crit[1], live=crit[2],
                                             all_candidates=opts)
            gov_dead_total += crit[1]
            gov_live_total += crit[2]

        wall_dead = wall.factored_dead_kNm(dc) if self.wall_present else 0.0
        self.udl_dead = gov_dead_total + wall_dead + self.self_weight_kNm
        self.udl_live = gov_live_total
        self.distribution_rows = rows
        return rows

    def reactions(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """STEP 9 — returns (R_left, R_right), each {'dead':.., 'live':..},
        honouring Pin / Cantilever end conditions."""
        L = self.length_m
        total_dead = self.udl_dead * L + sum(p.dead_kN for p in self.point_loads)
        total_live = self.udl_live * L + sum(p.live_kN for p in self.point_loads)

        if self.left_condition == "Cantilever":
            return dict(dead=0.0, live=0.0), dict(dead=total_dead, live=total_live)
        if self.right_condition == "Cantilever":
            return dict(dead=total_dead, live=total_live), dict(dead=0.0, live=0.0)

        RL = dict(dead=self.udl_dead * L / 2, live=self.udl_live * L / 2)
        RR = dict(dead=self.udl_dead * L / 2, live=self.udl_live * L / 2)
        for p in self.point_loads:
            RL['dead'] += p.dead_kN * (L - p.position_m) / L
            RL['live'] += p.live_kN * (L - p.position_m) / L
            RR['dead'] += p.dead_kN * p.position_m / L
            RR['live'] += p.live_kN * p.position_m / L
        return RL, RR


# ============================================================
# The whole multi-span beam
# ============================================================
class BeamSystem:
    def __init__(self, dc: DesignCriteria, panels: Dict[int, SlabPanel], wall: WallLoad,
                 project: Optional[ProjectInfo] = None):
        self.dc = dc
        self.panels = panels
        self.wall = wall
        self.project = project or ProjectInfo()
        self.spans: List[Span] = []

    def add_span(self, span: Span):
        self.spans.append(span)

    def support_labels(self) -> List[str]:
        n = len(self.spans)
        return [chr(65 + i) for i in range(n + 1)]

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
                out.append(f"  {p.label:<10}  : Gk = {p.dead_kN:.2f} kN   Qk = {p.live_kN:.2f} kN"
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
    def _draw(self, ax):
        labels = self.support_labels()
        cum = [0.0]
        for s in self.spans:
            cum.append(cum[-1] + s.length_m)
        Ltot = cum[-1]
        totals = self.support_reactions()

        ax.set_xlim(-0.6, Ltot + 0.6)
        ax.set_ylim(-3.4, 4.8)
        ax.axis('off')

        BEAM_Y, BEAM_H = 0, max(0.015 * Ltot, 0.03)
        ax.add_patch(patches.Rectangle((0, BEAM_Y - BEAM_H / 2), Ltot, BEAM_H,
                                        facecolor='#F2C9CE', edgecolor='black', zorder=5))

        def draw_support(x, label, free):
            sN = 0.045 * max(Ltot, 1) + 0.05
            if free:
                ax.plot(x, BEAM_Y, marker='o', markersize=6, color='white',
                         markeredgecolor='black', zorder=6)
            else:
                tri = patches.Polygon([(x - sN, -sN * 2.2), (x + sN, -sN * 2.2), (x, BEAM_Y - BEAM_H / 2)],
                                       closed=True, facecolor='white', edgecolor='black', zorder=6)
                ax.add_patch(tri)
                ax.plot([x - sN * 2, x + sN * 2], [-sN * 2.2] * 2, color='black', lw=1.2, zorder=6)
            ax.text(x, -sN * 2.2 - 0.18, label, ha='center', va='top', fontsize=12, fontweight='bold')

        for i, lbl in enumerate(labels):
            is_free = ((i == 0 and self.spans[0].left_condition == "Cantilever") or
                       (i == len(labels) - 1 and self.spans[-1].right_condition == "Cantilever"))
            draw_support(cum[i], lbl, is_free)

        for i, s in enumerate(self.spans):
            x0, x1 = cum[i], cum[i + 1]
            n = max(int(s.length_m * 3), 2) + 1
            y_top = 1.0
            for k in range(n):
                xi = x0 + k * (x1 - x0) / (n - 1)
                ax.annotate('', xy=(xi, BEAM_Y + BEAM_H / 2), xytext=(xi, y_top),
                            arrowprops=dict(arrowstyle='->', lw=1.1, color='#1F4E78'))
            ax.plot([x0, x1], [y_top, y_top], color='#1F4E78', lw=1.1)
            ax.text((x0 + x1) / 2, y_top + 0.08,
                    f"Gk={s.udl_dead:.2f}  Qk={s.udl_live:.2f} kN/m",
                    ha='center', va='bottom', fontsize=9.5, color='#1F4E78', fontweight='bold')

            heights = [1.8, 2.3, 2.8, 3.3]
            for j, p in enumerate(s.point_loads):
                xp = x0 + p.position_m
                h = heights[j % len(heights)]
                ax.annotate('', xy=(xp, BEAM_Y + BEAM_H / 2 + 0.02), xytext=(xp, h),
                            arrowprops=dict(arrowstyle='-|>', lw=2.2, color='#8B1E2E', mutation_scale=18))
                ax.text(xp, h + 0.08, f"{p.label}: {p.dead_kN:.1f}/{p.live_kN:.1f}kN",
                        ha='center', va='bottom', fontsize=8, color='#8B1E2E', fontweight='bold')

            ax.text((x0 + x1) / 2, -2.35, f"SPAN {i+1} = {s.length_m:.2f} m",
                    ha='center', va='top', fontsize=9, color='#1F4E78')

        for i, lbl in enumerate(labels):
            d, l = totals[lbl]['dead'], totals[lbl]['live']
            cond = self.support_condition_label(i)
            ax.text(cum[i], -1.55, f"Gk={d:.2f} kN\nQk={l:.2f} kN\n({cond})",
                    ha='center', va='top', fontsize=8.5, fontweight='bold')

        ax.set_title("Beam — Load & Reaction Diagram", fontsize=14, fontweight='bold')

    def plot(self, filename: str = "beam_diagram.png") -> str:
        cum_len = sum(s.length_m for s in self.spans) or 1
        fig, ax = plt.subplots(figsize=(max(6, cum_len * 2.4), 7.5))
        self._draw(ax)
        plt.tight_layout()
        plt.savefig(filename, dpi=200, facecolor='white')
        plt.close(fig)
        return filename


    # -------------------- STEP 12: PDF report (matches the Excel layout) --------------------
    def generate_pdf(self, filename: str = "beam_report.pdf") -> str:
        """
        Builds a PDF using real gridded tables that mirror the source
        spreadsheet's sections exactly (Slab Loading & Factoring, Wall
        Loading, Design Criteria & Materials, Load Distribution, Support
        Reactions, Total Support Reactions), with the 'Design Ref.' code
        citation on the left and the headline 'Output' on the right of each
        table, matching columns A and K of the sheet.
        """
        p, dc = self.project, self.dc
        BLUE = '#1F4E78'
        LEFT, RIGHT = 0.05, 0.95
        REF_W, OUT_W = 0.11, 0.14
        TABLE_X0, TABLE_X1 = LEFT + REF_W, RIGHT - OUT_W
        TOP, BOTTOM = 0.96, 0.05

        pages = []
        fig, ax, y = None, None, TOP

        def new_page():
            nonlocal fig, ax, y
            fig = plt.figure(figsize=(9.0, 12.4))
            ax = fig.add_axes([0, 0, 1, 1])
            ax.axis('off'); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_autoscale_on(False)
            ax.text(LEFT, 0.985, "DESIGN REF", fontsize=7, fontweight='bold', color='#888888',
                    transform=ax.transAxes)
            ax.text(TABLE_X1 + 0.01, 0.985, "OUTPUT", fontsize=7, fontweight='bold', color='#888888',
                    transform=ax.transAxes)
            ax.plot([0, 1], [0.978, 0.978], color='#CCCCCC', lw=0.6, transform=ax.transAxes)
            y = TOP
            pages.append(fig)

        def ensure_space(h):
            nonlocal y
            if y - h < BOTTOM:
                new_page()

        def section_title(text):
            nonlocal y
            ensure_space(0.05)
            ax.add_patch(patches.Rectangle((0, y - 0.022), 1, 0.028, transform=ax.transAxes,
                                            facecolor=BLUE, edgecolor='none', zorder=1))
            ax.text(0.5, y - 0.008, text, fontsize=10.5, fontweight='bold', color='white',
                    ha='center', va='center', zorder=2, transform=ax.transAxes)
            y -= 0.038

        def side_note(ref="", output=""):
            """Attach a Design Ref (left) / Output (right) note aligned to the table about to be drawn."""
            nonlocal y
            if ref:
                ax.text(LEFT, y, ref, fontsize=7.2, color='#666666', style='italic', va='top',
                        transform=ax.transAxes, wrap=True)
            if output:
                ax.text(TABLE_X1 + 0.01, y, output, fontsize=7.6, color=BLUE, fontweight='bold',
                        va='top', transform=ax.transAxes)

        def table(headers, rows, col_fracs, row_h=0.021, fontsize=7.3, ref="", output=""):
            """Draws a bordered grid table via ax.table, anchored at the current y cursor."""
            nonlocal y
            n_rows = len(rows) + 1
            height = row_h * n_rows
            ensure_space(height + 0.01)
            y0 = y - height
            width = TABLE_X1 - TABLE_X0
            side_note(ref, output)
            tbl = ax.table(cellText=rows, colLabels=headers,
                            bbox=[TABLE_X0, y0, width, height],
                            colWidths=col_fracs, cellLoc='center')
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(fontsize)
            for j in range(len(headers)):
                cell = tbl[0, j]
                cell.set_facecolor(BLUE)
                cell.get_text().set_color('white')
                cell.get_text().set_fontweight('bold')
                cell.get_text().set_fontsize(fontsize)
            for (r, c), cell in tbl.get_celld().items():
                cell.set_edgecolor('#AAAAAA')
                cell.set_linewidth(0.5)
            y = y0 - 0.012
            return tbl

        def text_line(txt, fontsize=8.3, bold=False, indent=0.0):
            nonlocal y
            ensure_space(0.02)
            ax.text(TABLE_X0 + indent, y, txt, fontsize=fontsize,
                    fontweight='bold' if bold else 'normal', va='top', transform=ax.transAxes)
            y -= 0.02

        new_page()

        # ---- Project header ----
        ax.text(LEFT, y, p.firm_name, fontsize=16, fontweight='bold', transform=ax.transAxes)
        y -= 0.026
        ax.text(LEFT, y, p.address, fontsize=8.5, transform=ax.transAxes)
        y -= 0.035
        header_rows = [
            ["Job No.", p.job_no, "Designer", p.designer, "Date", p.date],
            ["Element", p.element, "", "", "Material", p.material],
            ["Type", p.beam_type, "Spans", str(len(self.spans)), "Revision", p.revision],
            ["Location", p.location, "Calc. Sheet No.", p.calc_sheet_no, "", ""],
        ]
        table([""] * 6, header_rows, [0.13, 0.22, 0.13, 0.19, 0.11, 0.22], row_h=0.02, fontsize=7.6)

        # ---- SLAB LOADING AND FACTORING ----
        section_title("SLAB LOADING AND FACTORING")
        headers = ["Panel", "Self Weight\n(kN/m2)", "Finishes\n(kN/m2)", "Partition\n(kN/m2)",
                   "Live Loads\n(kN/m2)", "Dead nGk\n(kN/m2)", "Live nQk\n(kN/m2)"]
        rows = []
        for i, pnl in self.panels.items():
            rows.append([str(i), f"{pnl.self_weight_kNm2(dc):.3f}", f"{pnl.finishes_kNm2:.2f}",
                        f"{pnl.partition_kNm2(dc):.3f}", f"{pnl.live_kNm2:.2f}",
                        f"{pnl.dead_kNm2(dc):.3f}", f"{pnl.live_kNm2_factored(dc):.3f}"])
        table(headers, rows, [0.10, 0.16, 0.14, 0.16, 0.14, 0.15, 0.15],
              ref="BS 6399-1:1996\nTable 1")

        # ---- WALL LOADING ----
        section_title("WALL LOADING")
        headers = ["Present\nanywhere", "Thickness (m)", "Height (m)", "Load (kN/m)", "Factored nGk (kN/m)"]
        any_wall = any(s.wall_present for s in self.spans)
        rows = [["Yes" if any_wall else "No", f"{self.wall.thickness_m}", f"{self.wall.height_m}",
                 f"{self.wall.thickness_m*self.wall.height_m*dc.wall_density:.3f}",
                 f"{self.wall.factored_dead_kNm(dc):.3f}"]]
        table(headers, rows, [0.2, 0.2, 0.2, 0.2, 0.2], ref="BS 8110-1:1997",
              output=f"nGk={self.wall.factored_dead_kNm(dc):.3f} kN/m")

        # ---- DESIGN CRITERIA & MATERIALS ----
        section_title("DESIGN CRITERIA & MATERIALS")
        headers = ["Parameter", "Value", "Unit"]
        rows = [
            ["Concrete Density", f"{dc.concrete_density:.0f}", "kN/m3"],
            ["Wall Material Density", f"{dc.wall_density:.0f}", "kN/m3"],
            ["Finishes — Open Areas", f"{FINISHES_OPTIONS['1'][1]}", "kN/m2"],
            ["Finishes — Residential", f"{FINISHES_OPTIONS['2'][1]}", "kN/m2"],
            ["Live Load — Open Areas", f"{LIVE_LOAD_OPTIONS['1'][1]}", "kN/m2"],
            ["Live Load — Residential", f"{LIVE_LOAD_OPTIONS['2'][1]}", "kN/m2"],
            ["Dead Load Factor (self-wt/partition)", f"{dc.factor_selfweight_partition}", "-"],
            ["Dead Load Factor (finishes)", f"{dc.factor_finishes}", "-"],
            ["Live Load Factor", f"{dc.factor_live}", "-"],
            ["Concrete Characteristic Strength (fcu)", dc.concrete_grade, "-"],
            ["Reinforcement Yield Strength (fy)", dc.steel_grade, "-"],
            ["Exposure Conditions", dc.exposure_condition, "-"],
            ["Fire Resistance", f"{dc.fire_resistance_hours}", "Hours"],
            ["Concrete Cover", f"{dc.concrete_cover_mm:.0f}", "mm"],
        ]
        table(headers, rows, [0.55, 0.25, 0.2], row_h=0.019,
              ref="BS 6399-1:1996 Table 1\nEN 1990-1:2002 A1.2(B)\nBS 8110-1:1997 3.3 & 3.4",
              output=f"Provide {dc.concrete_cover_mm:.0f}mm Cover")

        # ---- Per-span sections ----
        labels = self.support_labels()
        for i, s in enumerate(self.spans):
            RL, RR = s.reactions()

            # -- Load distribution to beam --
            section_title(f"LOAD DISTRIBUTION TO BEAM — SPAN {i+1} ({labels[i]} -> {labels[i+1]})")
            headers = ["Panel", "Position", "Dist.\nFactor", "Lx (m)", "Dead nGk\n(kN/m)", "Live nQk\n(kN/m)"]
            rows = []
            for pid, pos, dead, live in s.distribution_rows:
                crit = s.governing.get(pos, {})
                tag = " *" if crit.get('panel_id') == pid else ""
                rows.append([f"{pid}{tag}", pos, f"{[c.distribution_factor for c in s.contributions if c.panel_id==pid and c.position==pos][0]:.2f}",
                            f"{self.panels[pid].lx_m:.2f}", f"{dead:.4f}", f"{live:.4f}"])
            if not rows:
                rows = [["-", "-", "-", "-", "0", "0"]]
            table(headers, rows, [0.13, 0.22, 0.14, 0.13, 0.19, 0.19],
                  ref="* = critical/governing\npanel on that side")
            crit_panels = sorted({g['panel_id'] for g in s.governing.values()})
            text_line(f"Take: " + ", ".join(f"Panel {pid}" for pid in crit_panels) if crit_panels
                      else "Take: (no panels assigned)", fontsize=7.8)
            text_line(f"Wall Loads Present: {'Yes' if s.wall_present else 'No'}"
                      + (f"    Self-weight: {s.self_weight_kNm:.3f} kN/m" if s.self_weight_kNm else ""),
                      fontsize=7.8)
            side_note(output=f"nGk={s.udl_dead:.4f} kN/m\nnQk={s.udl_live:.4f} kN/m")
            text_line(f"Total Uniformly Distributed Load (UDL):   "
                      f"Gk = {s.udl_dead:.4f} kN/m     Qk = {s.udl_live:.4f} kN/m", bold=True)
            y -= 0.008

            # -- Support reactions --
            section_title(f"SUPPORT REACTIONS — SPAN {i+1}")
            headers = ["Load Type", "Dead nGk\n(kN/m or kN)", "Live nQk\n(kN/m or kN)", "Position\nfrom left",
                       f"Dead {labels[i]}\n(kN)", f"Live {labels[i]}\n(kN)",
                       f"Dead {labels[i+1]}\n(kN)", f"Live {labels[i+1]}\n(kN)"]
            rows = [["udl", f"{s.udl_dead:.4f}", f"{s.udl_live:.4f}", f"{s.length_m:.2f}",
                    f"{s.udl_dead*s.length_m/2:.4f}" if s.left_condition == "Pin" and s.right_condition == "Pin" else "-",
                    f"{s.udl_live*s.length_m/2:.4f}" if s.left_condition == "Pin" and s.right_condition == "Pin" else "-",
                    f"{s.udl_dead*s.length_m/2:.4f}" if s.left_condition == "Pin" and s.right_condition == "Pin" else "-",
                    f"{s.udl_live*s.length_m/2:.4f}" if s.left_condition == "Pin" and s.right_condition == "Pin" else "-"]]
            for pl in s.point_loads:
                left_d = pl.dead_kN * (s.length_m - pl.position_m) / s.length_m
                left_l = pl.live_kN * (s.length_m - pl.position_m) / s.length_m
                right_d = pl.dead_kN * pl.position_m / s.length_m
                right_l = pl.live_kN * pl.position_m / s.length_m
                rows.append([pl.label, f"{pl.dead_kN:.2f}", f"{pl.live_kN:.2f}", f"{pl.position_m:.2f}",
                            f"{left_d:.4f}", f"{left_l:.4f}", f"{right_d:.4f}", f"{right_l:.4f}"])
            rows.append(["Total", "", "", "", f"{RL['dead']:.2f}", f"{RL['live']:.2f}",
                        f"{RR['dead']:.2f}", f"{RR['live']:.2f}"])
            table(headers, rows, [0.09, 0.13, 0.13, 0.10, 0.14, 0.14, 0.14, 0.13], fontsize=6.9,
                  output=f"R{labels[i]}: {RL['dead']:.1f}/{RL['live']:.1f}\n"
                         f"R{labels[i+1]}: {RR['dead']:.1f}/{RR['live']:.1f} kN")
            y -= 0.012

        # ---- TOTAL SUPPORT REACTIONS ----
        section_title("TOTAL SUPPORT REACTIONS")
        totals = self.support_reactions()
        headers = ["Support", "Condition", "Dead nGk (kN)", "Live nQk (kN)", "Total (kN)"]
        rows = []
        for idx, lbl in enumerate(labels):
            cond = self.support_condition_label(idx)
            d, l = totals[lbl]['dead'], totals[lbl]['live']
            rows.append([lbl, cond, f"{d:.2f}", f"{l:.2f}", f"{d+l:.2f}"])
        gtot_d = sum(v['dead'] for v in totals.values())
        gtot_l = sum(v['live'] for v in totals.values())
        rows.append(["Beam Total", "", f"{gtot_d:.4f}", f"{gtot_l:.4f}", f"{gtot_d+gtot_l:.4f}"])
        table(headers, rows, [0.16, 0.28, 0.2, 0.18, 0.18],
              ref="Note: shared/interior\nsupports combine\nreactions from both\nadjacent spans.")

        # ---- Diagram page ----
        fig2, ax2 = plt.subplots(figsize=(11.69, 8.27))
        self._draw(ax2)
        pages.append(fig2)

        with PdfPages(filename) as pdf:
            for f in pages:
                pdf.savefig(f)
                plt.close(f)

        return filename
