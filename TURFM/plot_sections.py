#!/usr/bin/env python
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


DEFAULT_CSV = "BUR-BUR-MER-ATATURK_KESIT_TESLIM_V1_TESTMODIFIED.csv"
DEFAULT_CSV_DIR = "new_data"
REQUIRED_COLUMNS = {"River", "Reach", "Station", "X", "Y", "Z"}


def compute_chainage(x, y):
    dx = np.diff(x)
    dy = np.diff(y)
    dist = np.hypot(dx, dy)
    return np.concatenate(([0.0], np.cumsum(dist)))


def order_points(x, y, z, order):
    if order == "file":
        return x, y, z
    coords = np.column_stack((x, y))
    centered = coords - coords.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    axis = vt[0]
    proj = centered @ axis
    idx = np.argsort(proj)
    return x[idx], y[idx], z[idx]


def station_filename(station, pad):
    return f"station_{int(station):0{pad}d}.png"


def offset_tag(distance):
    if abs(distance - round(distance)) < 1e-9:
        return f"{int(round(distance))}m"
    return f"{str(distance).replace('.', 'p')}m"


def find_channel_top_indices(x, y, z):
    if len(x) < 2:
        return []

    s = compute_chainage(x, y)
    ds = np.diff(s)
    dz = np.diff(z)

    pos_ds = ds[ds > 0]
    median_ds = float(np.median(pos_ds)) if len(pos_ds) else 1.0
    ds_tol = max(1e-4, 0.05 * median_ds)

    z_min = float(np.min(z))
    z_max = float(np.max(z))
    z_range = z_max - z_min
    z_tol = max(0.05, 0.05 * z_range)

    tops = []
    # Primary detector: near-vertical plan jump with strong Z change.
    vertical_mask = (ds <= ds_tol) & (np.abs(dz) >= z_tol)
    groups = []
    i = 0
    while i < len(vertical_mask):
        if not vertical_mask[i]:
            i += 1
            continue
        start = i
        while i < len(vertical_mask) and vertical_mask[i]:
            i += 1
        end = i - 1
        groups.append(np.arange(start, end + 2))

    for idx in groups:
        top_idx = int(idx[np.argmax(z[idx])])
        tops.append({"index": top_idx, "method": "vertical"})

    # Fallback: strongest negative slope left of thalweg + strongest positive right.
    if len(tops) < 2:
        min_idx = np.where(z == z_min)[0]
        thalweg = int(round(float(np.mean(min_idx)))) if len(min_idx) else int(np.argmin(z))

        valid = ds > ds_tol
        slopes = np.full_like(ds, np.nan, dtype=float)
        slopes[valid] = dz[valid] / ds[valid]

        left_idx = np.where((np.arange(len(ds)) < thalweg) & valid)[0]
        if len(left_idx):
            j = int(left_idx[np.nanargmin(slopes[left_idx])])
            tops.append({"index": j, "method": "slope-left"})

        right_idx = np.where((np.arange(len(ds)) >= thalweg) & valid)[0]
        if len(right_idx):
            j = int(right_idx[np.nanargmax(slopes[right_idx])])
            tops.append({"index": j + 1, "method": "slope-right"})

    method_rank = {"vertical": 2, "slope-left": 1, "slope-right": 1}
    best = {}
    for item in tops:
        idx = item["index"]
        method = item["method"]
        if idx not in best or method_rank[method] > method_rank[best[idx]]:
            best[idx] = method

    ordered = sorted(best.items(), key=lambda kv: s[kv[0]])
    return [{"index": idx, "method": method} for idx, method in ordered]


def select_top_items(tops):
    items = [
        {"label": i, "index": item["index"], "method": item["method"]}
        for i, item in enumerate(tops, start=1)
    ]
    if len(items) == 3:
        return [items[0], items[2]]
    if len(items) > 3:
        return [items[0], items[-1]]
    return items


def interp_at_chainage(s, x, y, z, target_s):
    if target_s <= s[0]:
        return float(s[0]), float(x[0]), float(y[0]), float(z[0]), True
    if target_s >= s[-1]:
        return float(s[-1]), float(x[-1]), float(y[-1]), float(z[-1]), True

    i = int(np.searchsorted(s, target_s) - 1)
    i = max(0, min(i, len(s) - 2))
    s0, s1 = s[i], s[i + 1]
    if s1 == s0:
        return float(s0), float(x[i]), float(y[i]), float(z[i]), True

    t = (target_s - s0) / (s1 - s0)
    xi = x[i] + t * (x[i + 1] - x[i])
    yi = y[i] + t * (y[i + 1] - y[i])
    zi = z[i] + t * (z[i + 1] - z[i])
    return float(target_s), float(xi), float(yi), float(zi), False


def write_excel_with_fallback(df, path):
    try:
        df.to_excel(path, index=False)
        return path
    except PermissionError:
        alt = path.with_name(f"{path.stem}_new{path.suffix}")
        df.to_excel(alt, index=False)
        print(f"Excel file locked; wrote to {alt} instead.")
        return alt


def write_top_shapefiles(top_df, out_dir):
    import shapefile  # pyshp

    points_path = out_dir / "channel_tops_points"
    w = shapefile.Writer(str(points_path), shapeType=shapefile.POINT)
    w.field("STATION", "N", size=8, decimal=0)
    w.field("TOP", "N", size=3, decimal=0)
    w.field("BANK", "C", size=5)
    w.field("METH", "C", size=10)
    w.field("S_M", "F", size=12, decimal=3)
    w.field("Z_M", "F", size=12, decimal=3)
    w.field("X", "F", size=15, decimal=3)
    w.field("Y", "F", size=15, decimal=3)
    w.field("RIVER", "C", size=20)
    w.field("REACH", "C", size=20)
    for _, row in top_df.iterrows():
        w.point(float(row["X"]), float(row["Y"]))
        w.record(
            int(row["Station"]),
            int(row["Top"]),
            str(row["Bank"]),
            str(row["Method"])[:10],
            float(row["S_m"]),
            float(row["Z_m"]),
            float(row["X"]),
            float(row["Y"]),
            str(row["River"])[:20],
            str(row["Reach"])[:20],
        )
    w.close()

    lines_path = out_dir / "channel_tops_banks"
    lw = shapefile.Writer(str(lines_path), shapeType=shapefile.POLYLINE)
    lw.field("BANK", "C", size=5)
    lw.field("COUNT", "N", size=8, decimal=0)
    for bank in ["left", "right"]:
        bank_df = top_df[top_df["Bank"] == bank].sort_values("Station")
        pts = [(float(x), float(y)) for x, y in zip(bank_df["X"], bank_df["Y"])]
        if len(pts) < 2:
            continue
        lw.line([pts])
        lw.record(bank, int(len(pts)))
    lw.close()


def write_offset_shapefiles(offset_df, out_dir, distance):
    import shapefile  # pyshp

    tag = offset_tag(distance)
    points_path = out_dir / f"channel_tops_offset_{tag}_points"
    w = shapefile.Writer(str(points_path), shapeType=shapefile.POINT)
    w.field("STATION", "N", size=8, decimal=0)
    w.field("TOP", "N", size=3, decimal=0)
    w.field("BANK", "C", size=5)
    w.field("METH", "C", size=10)
    w.field("OFF_M", "F", size=10, decimal=3)
    w.field("S_M", "F", size=12, decimal=3)
    w.field("Z_M", "F", size=12, decimal=3)
    w.field("X", "F", size=15, decimal=3)
    w.field("Y", "F", size=15, decimal=3)
    w.field("CLAMPED", "L")
    w.field("RIVER", "C", size=20)
    w.field("REACH", "C", size=20)
    for _, row in offset_df.iterrows():
        w.point(float(row["Offset_X"]), float(row["Offset_Y"]))
        w.record(
            int(row["Station"]),
            int(row["Top"]),
            str(row["Bank"]),
            str(row["Method"])[:10],
            float(row["Offset_m"]),
            float(row["Offset_S_m"]),
            float(row["Offset_Z_m"]),
            float(row["Offset_X"]),
            float(row["Offset_Y"]),
            bool(row["Offset_Clamped"]),
            str(row["River"])[:20],
            str(row["Reach"])[:20],
        )
    w.close()

    lines_path = out_dir / f"channel_tops_offset_{tag}_banks"
    lw = shapefile.Writer(str(lines_path), shapeType=shapefile.POLYLINE)
    lw.field("BANK", "C", size=5)
    lw.field("COUNT", "N", size=8, decimal=0)
    lw.field("OFF_M", "F", size=10, decimal=3)
    for bank in ["left", "right"]:
        bank_df = offset_df[offset_df["Bank"] == bank].sort_values("Station")
        pts = [
            (float(x), float(y))
            for x, y in zip(bank_df["Offset_X"], bank_df["Offset_Y"])
        ]
        if len(pts) < 2:
            continue
        lw.line([pts])
        lw.record(bank, int(len(pts)), float(distance))
    lw.close()


def read_sections_csv(csv_path):
    return pd.read_csv(csv_path, sep=None, engine="python")


def process_csv(csv_path, result_dir, args):
    df = read_sections_csv(csv_path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ValueError(f"{csv_path.name}: missing required columns: {missing_text}")

    plots_dir = result_dir / "sections"
    ann_dir = result_dir / "sections_annotated"
    plots_dir.mkdir(parents=True, exist_ok=True)
    ann_dir.mkdir(parents=True, exist_ok=True)

    pad = len(str(int(df["Station"].max())))
    summary_rows = []
    top_rows = []
    offset_rows = {dist: [] for dist in args.offset_distances}

    dist_palette = ["#2ca02c", "#ff7f0e", "#17becf", "#9467bd"]
    dist_markers = ["s", "^", "D", "v"]

    for station, group in df.groupby("Station", sort=True):
        group = group.reset_index(drop=True)
        x = group["X"].to_numpy()
        y = group["Y"].to_numpy()
        z = group["Z"].to_numpy()
        x, y, z = order_points(x, y, z, args.order)
        s = compute_chainage(x, y)
        length = float(s[-1]) if len(s) else 0.0

        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(s, z, "-o", color="#1f77b4", markersize=2.5, linewidth=1)
        ax.set_xlabel("Distance along section (m)")
        ax.set_ylabel("Elevation Z (m)")
        ax.grid(True, alpha=0.3)
        if not args.no_title:
            ax.set_title(f"Station {int(station)} (n={len(group)}, L={length:.1f} m)")

        fig.tight_layout()
        fig.savefig(plots_dir / station_filename(station, pad), dpi=args.dpi)

        tops = select_top_items(find_channel_top_indices(x, y, z))
        legend_seen = set()
        for top in tops:
            idx = int(top["index"])
            label = int(top["label"])
            method = str(top["method"])
            bank = "left" if label == 1 else "right"
            offset_sign = -1 if label == 1 else 1

            top_rows.append(
                {
                    "Station": int(station),
                    "River": str(group["River"].iloc[0]),
                    "Reach": str(group["Reach"].iloc[0]),
                    "Top": label,
                    "Index": idx,
                    "Method": method,
                    "Bank": bank,
                    "S_m": float(s[idx]),
                    "Z_m": float(z[idx]),
                    "X": float(x[idx]),
                    "Y": float(y[idx]),
                }
            )

            if not args.no_annotate_plots:
                top_label = "Top bank" if "top" not in legend_seen else None
                legend_seen.add("top")
                ax.plot(
                    s[idx],
                    z[idx],
                    "o",
                    color="#d62728",
                    markersize=6,
                    label=top_label,
                )
                ax.annotate(
                    f"Top {label}",
                    xy=(s[idx], z[idx]),
                    xytext=(6, 8),
                    textcoords="offset points",
                    color="#d62728",
                    fontsize=args.annotate_fontsize,
                )

            for j, distance in enumerate(args.offset_distances):
                target_s = float(s[idx] + offset_sign * distance)
                off_s, off_x, off_y, off_z, clamped = interp_at_chainage(
                    s, x, y, z, target_s
                )
                offset_rows[distance].append(
                    {
                        "Station": int(station),
                        "River": str(group["River"].iloc[0]),
                        "Reach": str(group["Reach"].iloc[0]),
                        "Top": label,
                        "Index": idx,
                        "Method": method,
                        "Bank": bank,
                        "Offset_m": float(distance),
                        "Offset_S_m": float(off_s),
                        "Offset_Z_m": float(off_z),
                        "Offset_X": float(off_x),
                        "Offset_Y": float(off_y),
                        "Offset_Clamped": bool(clamped),
                    }
                )

                if not args.no_annotate_plots:
                    dist_key = f"off-{distance}"
                    point_label = (
                        f"Offset {distance:g} m" if dist_key not in legend_seen else None
                    )
                    legend_seen.add(dist_key)
                    ax.plot(
                        off_s,
                        off_z,
                        dist_markers[j % len(dist_markers)],
                        color=dist_palette[j % len(dist_palette)],
                        markersize=5,
                        label=point_label,
                    )

        if not args.no_annotate_plots and legend_seen:
            ax.legend(loc="best", fontsize=7)
            fig.tight_layout()
            fig.savefig(ann_dir / station_filename(station, pad), dpi=args.dpi)

        plt.close(fig)
        summary_rows.append(
            {"Station": int(station), "Points": len(group), "Length_m": length}
        )

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(result_dir / "sections_summary.csv", index=False)

    top_df = pd.DataFrame(top_rows)
    top_df.to_csv(result_dir / "channel_tops_summary.csv", index=False)
    top_xlsx = write_excel_with_fallback(top_df, result_dir / "channel_tops_summary.xlsx")

    offset_tables = {}
    for distance in args.offset_distances:
        tag = offset_tag(distance)
        table = pd.DataFrame(offset_rows[distance])
        table.to_csv(result_dir / f"offset_{tag}_summary.csv", index=False)
        write_excel_with_fallback(table, result_dir / f"offset_{tag}_summary.xlsx")
        offset_tables[distance] = table

    if not args.no_shapefile:
        try:
            write_top_shapefiles(top_df, result_dir)
            for distance, table in offset_tables.items():
                write_offset_shapefiles(table, result_dir, distance)
        except ModuleNotFoundError:
            print("pyshp not installed; skipping shapefile export.")

    print(f"Processed {csv_path.name} -> {result_dir}")
    print(f"Wrote section summary: {result_dir / 'sections_summary.csv'}")
    print(f"Wrote top summary: {result_dir / 'channel_tops_summary.csv'}")
    print(f"Wrote top summary xlsx: {top_xlsx}")
    for distance in args.offset_distances:
        tag = offset_tag(distance)
        print(f"Wrote offset summary: {result_dir / f'offset_{tag}_summary.csv'}")


def resolve_csv_paths(args):
    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
        if not csv_dir.exists():
            raise FileNotFoundError(f"CSV directory not found: {csv_dir}")
        paths = sorted([p for p in csv_dir.glob("*.csv") if p.is_file()])
        if not paths:
            raise FileNotFoundError(f"No CSV files found in: {csv_dir}")
        return paths

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")
        return [csv_path]

    default_dir = Path(DEFAULT_CSV_DIR)
    if default_dir.exists():
        paths = sorted([p for p in default_dir.glob("*.csv") if p.is_file()])
        if paths:
            return paths

    fallback = Path(DEFAULT_CSV)
    if fallback.exists():
        return [fallback]
    raise FileNotFoundError("No input CSV found. Pass a CSV path or --csv-dir.")


def main():
    parser = argparse.ArgumentParser(
        description="Plot cross-sections and export top/offset bank shapefiles."
    )
    parser.add_argument(
        "csv",
        nargs="?",
        default=None,
        help=f"Single input CSV (fallback: {DEFAULT_CSV}).",
    )
    parser.add_argument(
        "--csv-dir",
        default=None,
        help=f"Directory of CSV files (default fallback: {DEFAULT_CSV_DIR}).",
    )
    parser.add_argument(
        "--out-root",
        default="outputs",
        help="Root output directory. Each CSV gets a subfolder named exactly like the CSV file.",
    )
    parser.add_argument(
        "--order",
        choices=["file", "pca"],
        default="file",
        help="Point ordering within each station.",
    )
    parser.add_argument(
        "--offset-distances",
        nargs="+",
        type=float,
        default=[0.1, 1.0],
        help="Offsets in meters. Defaults to 0.1 and 1.0.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI for PNG output.",
    )
    parser.add_argument(
        "--annotate-fontsize",
        type=int,
        default=8,
        help="Font size for plot annotation labels.",
    )
    parser.add_argument(
        "--no-annotate-plots",
        action="store_true",
        help="Skip saving annotated section plots.",
    )
    parser.add_argument(
        "--annotate-tops",
        action="store_true",
        help="Compatibility flag; annotations are already on by default.",
    )
    parser.add_argument(
        "--no-shapefile",
        action="store_true",
        help="Skip writing shapefiles.",
    )
    parser.add_argument(
        "--no-title",
        action="store_true",
        help="Skip plot titles.",
    )

    args = parser.parse_args()
    csv_paths = resolve_csv_paths(args)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    for csv_path in csv_paths:
        result_dir = out_root / csv_path.name
        result_dir.mkdir(parents=True, exist_ok=True)
        process_csv(csv_path, result_dir, args)


if __name__ == "__main__":
    main()
