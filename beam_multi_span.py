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

    # -------------------- STEP 12: PDF report --------------------
    def generate_pdf(self, filename: str = "beam_report.pdf") -> str:
        p = self.project
        with PdfPages(filename) as pdf:
            # ---- Page 1: project info + slab loading ----
            fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
            fig.suptitle(p.firm_name, fontsize=16, fontweight='bold', x=0.08, ha='left', y=0.97)
            fig.text(0.08, 0.945, p.address, fontsize=9)

            header_lines = [
                f"Job No.: {p.job_no}        Calc. Sheet No.: {p.calc_sheet_no}",
                f"Designer: {p.designer}        Date: {p.date}        Revision: {p.revision}",
                f"Element: {p.element}",
                f"Type: {p.beam_type}        Spans: {len(self.spans)}",
                f"Location: {p.location}        Material: {p.material}",
            ]
            fig.text(0.08, 0.90, "\n".join(header_lines), fontsize=9.5, va='top', family='monospace')

            fig.text(0.08, 0.72, "SLAB LOADING & FACTORING", fontsize=11, fontweight='bold')
            fig.text(0.08, 0.70, self.slab_loading_text().split("\n", 2)[-1],
                     fontsize=8.5, va='top', family='monospace')

            wall_text = (f"Wall on beam: thickness {self.wall.thickness_m} m, height {self.wall.height_m} m, "
                         f"factored Gk = {self.wall.factored_dead_kNm(self.dc):.3f} kN/m")
            fig.text(0.08, 0.50, "WALL LOADING", fontsize=11, fontweight='bold')
            fig.text(0.08, 0.48, wall_text, fontsize=8.5, va='top')

            fig.text(0.08, 0.40, "DESIGN CRITERIA", fontsize=11, fontweight='bold')
            dc_text = (f"Concrete density = {self.dc.concrete_density} kN/m3\n"
                       f"Wall density = {self.dc.wall_density} kN/m3\n"
                       f"Load factors: self-weight/partition = {self.dc.factor_selfweight_partition}, "
                       f"finishes = {self.dc.factor_finishes}, live = {self.dc.factor_live}")
            fig.text(0.08, 0.38, dc_text, fontsize=8.5, va='top', family='monospace')
            pdf.savefig(fig)
            plt.close(fig)

            # ---- Page 2: tabulated results ----
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.08, 0.96, "TABULATED RESULTS", fontsize=13, fontweight='bold')
            fig.text(0.06, 0.93, self.tabulate(), fontsize=7.6, va='top', family='monospace')
            pdf.savefig(fig)
            plt.close(fig)

            # ---- Page 3: diagram ----
            fig, ax = plt.subplots(figsize=(11.69, 8.27))  # landscape
            self._draw(ax)
            pdf.savefig(fig)
            plt.close(fig)

        return filename
