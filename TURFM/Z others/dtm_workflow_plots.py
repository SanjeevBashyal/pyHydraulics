from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "dtm_workflow_images"
DPI = 220

COLORS = {
    "terrain": "#6c757d",
    "channel": "#0077b6",
    "centerline": "#d62828",
    "survey": "#f77f00",
    "junction": "#2a9d8f",
    "envelope": "#90be6d",
    "blend": "#7b2cbf",
    "grid": "#adb5bd",
    "background": "#f8f9fa",
}


def apply_style() -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.labelsize": 10,
            "legend.fontsize": 9,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "lines.linewidth": 2.2,
        }
    )


def save_figure(fig: plt.Figure, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return path


def add_box(ax, xy, width, height, title, body, color):
    box = FancyBboxPatch(
        xy,
        width,
        height,
        boxstyle="round,pad=0.02,rounding_size=0.03",
        linewidth=1.4,
        edgecolor=color,
        facecolor=color,
        alpha=0.14,
    )
    ax.add_patch(box)
    x0, y0 = xy
    ax.text(x0 + width / 2, y0 + height * 0.67, title, ha="center", va="center", fontsize=11, weight="bold")
    ax.text(x0 + width / 2, y0 + height * 0.35, body, ha="center", va="center", fontsize=9)


def add_arrow(ax, start, end, color="#495057"):
    arrow = FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=14, linewidth=1.5, color=color)
    ax.add_patch(arrow)


def make_workflow_overview() -> Path:
    fig, ax = plt.subplots(figsize=(14, 7))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    boxes = [
        ((0.05, 0.68), 0.25, 0.18, "Input Data", "DTM raster\nCross sections\nBank lines", COLORS["terrain"]),
        ((0.38, 0.68), 0.25, 0.18, "Geometry Build", "Centerline\nLocal widths\nInterpolation corridor", COLORS["channel"]),
        ((0.71, 0.68), 0.24, 0.18, "Single-Channel Update", "Map raster cells to\nbracketing sections\nand compute channel bed", COLORS["survey"]),
        ((0.05, 0.28), 0.25, 0.18, "Junction Detection", "Identify tributary endpoint\nmeeting the mid-reach\nof a main channel", COLORS["junction"]),
        ((0.38, 0.28), 0.25, 0.18, "Network Preparation", "Extend tributary banks\nUse one shared raster window\nfor all sub-projects", COLORS["envelope"]),
        ((0.71, 0.28), 0.24, 0.18, "Final DTM", "Merge channel rasters\nby minimum elevation\nand export terrain", COLORS["blend"]),
    ]

    for args in boxes:
        add_box(ax, *args)

    add_arrow(ax, (0.30, 0.77), (0.38, 0.77))
    add_arrow(ax, (0.63, 0.77), (0.71, 0.77))
    add_arrow(ax, (0.17, 0.68), (0.17, 0.48))
    add_arrow(ax, (0.50, 0.68), (0.50, 0.48))
    add_arrow(ax, (0.83, 0.68), (0.83, 0.48))
    add_arrow(ax, (0.30, 0.37), (0.38, 0.37))
    add_arrow(ax, (0.63, 0.37), (0.71, 0.37))

    ax.text(0.5, 0.94, "DTM Interpolation Workflow", ha="center", va="center", fontsize=18, weight="bold")
    ax.text(
        0.5,
        0.08,
        "The implementation moves from surveyed geometry to a raster-based channel update, then extends the same logic to tributaries and junctions on a common terrain window.",
        ha="center",
        va="center",
        fontsize=10,
    )
    return save_figure(fig, "dtm_workflow_overview.png")


def make_single_channel_geometry() -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), gridspec_kw={"width_ratios": [1.3, 1.0]})

    x = np.linspace(0, 10, 500)
    center = 0.35 * np.sin(0.8 * x) + 0.12 * np.sin(2.1 * x)
    width = 1.25 + 0.18 * np.cos(0.7 * x)
    bank_left = center + width
    bank_right = center - width
    env_left = bank_left + 0.65
    env_right = bank_right - 0.65

    ax = axes[0]
    ax.fill_between(x, env_right, env_left, color=COLORS["envelope"], alpha=0.16, label="Interpolation envelope")
    ax.fill_between(x, bank_right, bank_left, color=COLORS["channel"], alpha=0.18, label="Bank polygon")
    ax.plot(x, bank_left, color=COLORS["channel"], label="Bank lines")
    ax.plot(x, bank_right, color=COLORS["channel"])
    ax.plot(x, center, color=COLORS["centerline"], linestyle="--", label="Generated centerline")

    xs_positions = [1.4, 3.4, 5.8, 8.0]
    for idx, x0 in enumerate(xs_positions):
        i = np.argmin(np.abs(x - x0))
        color = COLORS["survey"] if idx in (1, 2) else "#f4a261"
        ax.plot([x[i], x[i]], [bank_right[i], bank_left[i]], color=color, linewidth=2.0)

    p_x = 4.85
    i = np.argmin(np.abs(x - p_x))
    cp = (x[i], center[i])
    p = (x[i], center[i] + 0.72)
    ax.scatter([p[0]], [p[1]], color=COLORS["blend"], s=55, zorder=5)
    ax.scatter([cp[0]], [cp[1]], color=COLORS["centerline"], s=50, zorder=5)
    ax.plot([p[0], cp[0]], [p[1], cp[1]], color=COLORS["blend"], linestyle=":", linewidth=2)

    ax.text(p[0] + 0.15, p[1] + 0.05, "Raster cell p", color=COLORS["blend"])
    ax.text(cp[0] + 0.1, cp[1] - 0.35, "Projected point C(p)", color=COLORS["centerline"])
    ax.text(3.15, bank_left[np.argmin(np.abs(x - 3.4))] + 0.35, "Upstream section", color=COLORS["survey"])
    ax.text(5.55, bank_left[np.argmin(np.abs(x - 5.8))] + 0.35, "Downstream section", color=COLORS["survey"])

    ax.set_title("Plan-View Geometry Used for Single-Channel Interpolation")
    ax.set_xlabel("Local channel direction")
    ax.set_ylabel("Cross-stream direction")
    ax.legend(loc="upper right", frameon=True)
    ax.set_aspect("equal", adjustable="box")

    ax2 = axes[1]
    xi = np.linspace(-6, 6, 400)
    z_up = 101.2 + 0.05 * xi**2 - 1.55 * np.exp(-(xi / 2.1) ** 2)
    z_dn = 100.6 + 0.045 * (xi - 0.35) ** 2 - 1.35 * np.exp(-((xi - 0.35) / 1.9) ** 2)
    ax2.plot(xi, z_up, color=COLORS["survey"], label="Upstream section")
    ax2.plot(xi, z_dn, color=COLORS["junction"], label="Downstream section")

    xi_up = 1.6
    xi_dn = 1.95
    z_up_s = np.interp(xi_up, xi, z_up)
    z_dn_s = np.interp(xi_dn, xi, z_dn)
    z_mix = 0.55 * z_up_s + 0.45 * z_dn_s

    ax2.axvline(0, color=COLORS["centerline"], linestyle="--", linewidth=1.8, label="Centerline crossing")
    ax2.scatter([xi_up], [z_up_s], color=COLORS["survey"], s=50)
    ax2.scatter([xi_dn], [z_dn_s], color=COLORS["junction"], s=50)
    ax2.axhline(z_mix, color=COLORS["blend"], linestyle=":", linewidth=2.2, label="Interpolated channel elevation")

    ax2.text(xi_up + 0.2, z_up_s + 0.15, "Mapped point on\nupstream section", color=COLORS["survey"])
    ax2.text(xi_dn + 0.2, z_dn_s - 0.5, "Mapped point on\ndownstream section", color=COLORS["junction"])
    ax2.text(-5.5, z_mix + 0.18, "Cell elevation before terrain blending", color=COLORS["blend"])
    ax2.set_title("Cross-Section Sampling at the Cell Location")
    ax2.set_xlabel("Distance along cross section")
    ax2.set_ylabel("Elevation")
    ax2.legend(loc="upper left", frameon=True)

    return save_figure(fig, "dtm_single_channel_geometry.png")


def make_blending_logic() -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))
    bank_half = 2.6
    env_half = 7.0

    ax = axes[0]
    lam = np.linspace(0, 1, 200)
    alpha_up = 1 - lam
    alpha_dn = lam
    ax.plot(lam, alpha_up, color=COLORS["survey"], label="Weight of upstream section")
    ax.plot(lam, alpha_dn, color=COLORS["junction"], label="Weight of downstream section")
    ax.fill_between(lam, 0, alpha_up, color=COLORS["survey"], alpha=0.12)
    ax.fill_between(lam, 0, alpha_dn, color=COLORS["junction"], alpha=0.12)
    ax.set_title("Longitudinal Interpolation Between Two Sections")
    ax.set_xlabel("Relative cell position between sections")
    ax.set_ylabel("Weight")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.legend(loc="center")

    ax = axes[1]
    xw = np.linspace(-8, 8, 500)
    absxw = np.abs(xw)
    beta_terrain = np.zeros_like(xw)
    transition_mask = (absxw > bank_half) & (absxw < env_half)
    outer_mask = absxw >= env_half
    beta_terrain[transition_mask] = (absxw[transition_mask] - bank_half) / (env_half - bank_half)
    beta_terrain[outer_mask] = 1.0
    beta_channel = 1.0 - beta_terrain
    beta_exp = np.zeros_like(xw)
    beta_exp[transition_mask] = (np.exp(beta_terrain[transition_mask]) - 1) / (np.e - 1)
    beta_exp[outer_mask] = 1.0

    ax.axvspan(-bank_half, bank_half, color=COLORS["channel"], alpha=0.08)
    ax.axvspan(-env_half, -bank_half, color=COLORS["envelope"], alpha=0.10)
    ax.axvspan(bank_half, env_half, color=COLORS["envelope"], alpha=0.10)
    ax.plot(xw, beta_channel, color=COLORS["channel"], label="Channel interpolation weight")
    ax.plot(xw, beta_terrain, color=COLORS["terrain"], label="Linear terrain weight")
    ax.plot(xw, beta_exp, color=COLORS["blend"], linestyle="--", label="Exponential terrain weight")
    ax.text(0.0, 0.12, "Inside bank polygon:\nterrain weight = 0", color=COLORS["channel"], ha="center")
    ax.text(5.35, 0.72, "Transition zone", color=COLORS["envelope"], ha="center")
    ax.set_title("Transverse Blend Used in Raster Writing")
    ax.set_xlabel("Cross-stream position")
    ax.set_ylabel("Weight")
    ax.set_xlim(-8, 8)
    ax.set_ylim(0, 1.02)
    ax.legend(loc="center")

    ax = axes[2]
    x = np.linspace(-8, 8, 400)
    terrain = 102.7 + 0.03 * x + 0.015 * x**2
    channel = 102.0 + 0.012 * x**2 - 1.8 * np.exp(-(x / 2.6) ** 2)
    absx = np.abs(x)
    terrain_weight = np.zeros_like(x)
    transition_mask = (absx > bank_half) & (absx < env_half)
    outer_mask = absx >= env_half
    terrain_weight[transition_mask] = (absx[transition_mask] - bank_half) / (env_half - bank_half)
    terrain_weight[outer_mask] = 1.0
    final = terrain_weight * terrain + (1 - terrain_weight) * channel
    ax.plot(x, terrain, color=COLORS["terrain"], label="Original terrain")
    ax.plot(x, channel, color=COLORS["channel"], label="Cross-section-derived bed")
    ax.plot(x, final, color=COLORS["blend"], label="Final blended terrain")
    ax.axvspan(-bank_half, bank_half, color=COLORS["channel"], alpha=0.10)
    ax.axvspan(-env_half, -bank_half, color=COLORS["envelope"], alpha=0.08)
    ax.axvspan(bank_half, env_half, color=COLORS["envelope"], alpha=0.08)
    center_idx = np.argmin(np.abs(x))
    ax.text(0.0, channel[center_idx] - 0.55, "Inside bank polygon:\nfinal = channel bed", color=COLORS["blend"], ha="center")
    ax.text(5.2, terrain[np.argmin(np.abs(x - 6.2))] + 0.08, "Outside envelope:\nfinal = original terrain", color=COLORS["terrain"], ha="center")
    ax.set_title("Final Elevation Written to the Raster")
    ax.set_xlabel("Cross-stream position")
    ax.set_ylabel("Elevation")
    ax.legend(loc="upper center")

    return save_figure(fig, "dtm_blending_logic.png")


def make_junction_workflow() -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))

    ax = axes[0]
    x = np.linspace(0, 12, 500)
    center_main = 0.25 * np.sin(0.45 * x)
    bank_width = 0.95
    ax.plot(x, center_main + bank_width, color=COLORS["channel"])
    ax.plot(x, center_main - bank_width, color=COLORS["channel"], label="Main-channel banks")
    ax.plot(x, center_main, color=COLORS["centerline"], linestyle="--", label="Main centerline")

    yt = np.linspace(5.8, 0.8, 220)
    xt = 6.1 + 0.25 * np.sin(1.2 * yt)
    ax.plot(xt - 0.75, yt, color=COLORS["junction"])
    ax.plot(xt + 0.75, yt, color=COLORS["junction"], label="Tributary banks")
    ax.plot(xt, yt, color="#1d3557", linestyle="--", label="Tributary centerline")

    junction = (6.1, np.interp(6.1, x, center_main))
    ax.scatter([junction[0]], [junction[1]], color=COLORS["blend"], s=65, zorder=5)
    ax.plot([xt[-1] - 0.75, 5.2], [yt[-1], junction[1] + 0.95], color=COLORS["blend"], linestyle=":")
    ax.plot([xt[-1] + 0.75, 7.0], [yt[-1], junction[1] - 0.95], color=COLORS["blend"], linestyle=":")

    ax.text(junction[0] + 0.15, junction[1] + 0.18, "Detected junction", color=COLORS["blend"])
    ax.text(6.95, 2.7, "Bank extension\nbefore interpolation", color=COLORS["blend"])
    ax.set_title("Junction Detection and Tributary Bank Extension")
    ax.set_xlabel("Plan-view x")
    ax.set_ylabel("Plan-view y")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper left")

    ax = axes[1]
    ax.plot(x, center_main + bank_width, color=COLORS["channel"])
    ax.plot(x, center_main - bank_width, color=COLORS["channel"])
    ax.plot(xt - 0.75, yt, color=COLORS["junction"])
    ax.plot(xt + 0.75, yt, color=COLORS["junction"])
    ax.plot(x, center_main, color=COLORS["centerline"], linestyle="--")
    ax.plot(xt, yt, color="#1d3557", linestyle="--")
    rect = Rectangle((0.7, -2.3), 10.8, 8.7, fill=False, linewidth=2.2, linestyle="-.", edgecolor=COLORS["terrain"])
    ax.add_patch(rect)
    for xv in np.linspace(0.7, 11.5, 12):
        ax.plot([xv, xv], [-2.3, 6.4], color=COLORS["grid"], linewidth=0.6, alpha=0.75)
    for yv in np.linspace(-2.3, 6.4, 10):
        ax.plot([0.7, 11.5], [yv, yv], color=COLORS["grid"], linewidth=0.6, alpha=0.75)
    ax.text(1.0, 6.65, "All channels are interpolated on one shared raster window", color=COLORS["terrain"])
    ax.set_title("Shared Raster Extent for the Whole River System")
    ax.set_xlabel("Plan-view x")
    ax.set_ylabel("Plan-view y")
    ax.set_aspect("equal", adjustable="box")

    ax = axes[2]
    s = np.linspace(0, 1, 250)
    z_main = 101.8 - 1.2 * np.exp(-((s - 0.45) / 0.23) ** 2) + 0.08 * np.sin(8 * s)
    z_trib = 101.6 - 1.55 * np.exp(-((s - 0.58) / 0.18) ** 2) + 0.05 * np.cos(7 * s)
    z_net = np.minimum(z_main, z_trib)
    ax.plot(s, z_main, color=COLORS["channel"], label="Main-channel interpolated surface")
    ax.plot(s, z_trib, color=COLORS["junction"], label="Tributary interpolated surface")
    ax.plot(s, z_net, color=COLORS["blend"], linewidth=2.8, label="Final network surface = cellwise minimum")
    ax.fill_between(s, z_net, np.maximum(z_main, z_trib), color=COLORS["blend"], alpha=0.08)
    ax.set_title("Final Merge in the Overlap Zone")
    ax.set_xlabel("Relative position inside the overlap region")
    ax.set_ylabel("Elevation")
    ax.legend(loc="upper center")

    return save_figure(fig, "dtm_junction_workflow.png")


def main() -> None:
    apply_style()
    generated = [
        make_workflow_overview(),
        make_single_channel_geometry(),
        make_blending_logic(),
        make_junction_workflow(),
    ]
    print("Generated workflow figures:")
    for path in generated:
        print(path)


if __name__ == "__main__":
    main()
