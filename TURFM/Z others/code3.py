import os
import numpy as np
import matplotlib.pyplot as plt
import geopandas as gpd
from shapely.geometry import LineString, Point

from Automation.DTM import DTMChannelModifier

PROJECT_FOLDER = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"  # Base folder for the project

PROJECT_SHORT_NAME = "ATATURK-T"      # Name of the project

PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

# PROJ_PATH = os.path.join(PROJECT_FOLDER, '0 Proj')
# BUR_BUR_PATH = os.path.join(PROJECT_FOLDER, '1 Bur-Bur')
# HYDRO_PATH = os.path.join(PROJECT_FOLDER, '2 Hydrology')
# HEC_PATH = os.path.join(PROJECT_FOLDER, '3 Hecras')
# GIS_PATH = os.path.join(PROJECT_FOLDER, '4 GIS')
# OUTPUT_PATH = os.path.join(PROJECT_FOLDER,'5 Outputs')

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')

# Setup Paths
PWD = os.getcwd()
CROSS_SECTION_FILE_PATH = os.path.join(PWD, "cross-section.csv")
BANK_LINE_FILE_PATH = os.path.join(PWD, r"0 HECRAS-Template\1 Bur-Bur\BUR-BUR-MER-ATATURK-T\SEV_USTU\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp")
# DEM_PATH = os.path.join(PWD, r"0 HECRAS-Template\SET4_27_DTM_070226_R1.tif")
OUTPUT_PATH = os.path.join(PWD, r"0 HECRAS-Template\5 Outputs")

if not os.path.exists(OUTPUT_PATH):
    os.makedirs(OUTPUT_PATH, exist_ok=True)


def test_single_cell():
    print("Running single-cell test...")

    # Process DTM cells with break_after_first=True
    results, modifier = DTMChannelModifier.process_dtm_cells(
        dtm_path=DEM_PATH,
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        target_res=0.1,
        buffer_m=20.0,
        break_after_first=True
    )

    if not results:
        print("No cells found inside the bank polygon.")
        return

    cell = results[0]
    print(f"\nTest cell result:")
    print(f"  Cell (row={cell['row']}, col={cell['col']})")
    print(f"  Position: ({cell['x']}, {cell['y']}), DTM Z = {cell['dtm_z']}")
    print(f"  Nearest Centerline: ({cell['cx']}, {cell['cy']})")
    print(f"    -> Distance to Centerline: {cell['dist_to_centerline']}m")
    print(f"  Bank Width at that location: {cell['bank_width']}m")
    print(f"  Upstream Cross Section: {cell['up_station']}")
    print(f"    -> Minimum distance from cell to Upstream CS: {cell['min_dist_up']}m")
    print(f"  Downstream Cross Section: {cell['down_station']}")
    print(f"    -> Minimum distance from cell to Downstream CS: {cell['min_dist_down']}m")
    print(f"  --- Elevations ---")
    print(f"  Original DTM Z: {cell['dtm_z']}m")
    print(f"  New Interpolated Z: {cell['new_interpolated_z']}m")

    # ---- Plotting ----
    banks = gpd.read_file(BANK_LINE_FILE_PATH)
    centerline = modifier.centerline_gdf.geometry.iloc[0]
    polygon = modifier.channel_polygon

    import pandas as pd
    from shapely.ops import nearest_points
    cell_pt = Point(cell['x'], cell['y'])

    # Still need to load cross sections for plotting them
    df = pd.read_csv(CROSS_SECTION_FILE_PATH)
    group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
    if not group_cols: group_cols = ['Station']

    upstream_line = None
    downstream_line = None

    for name, group in df.groupby(group_cols):
        coords_3d = group[['X', 'Y', 'Z']].values
        if len(coords_3d) < 2: continue
        line = LineString(coords_3d)
        stat_name = str(name if not isinstance(name, tuple) else name[-1])
        
        if stat_name == str(cell['up_station']):
            upstream_line = line
        if stat_name == str(cell['down_station']):
            downstream_line = line

    # Get the bank lines for plotting the width line
    bank_lines = []
    for geom in banks.geometry:
        if geom.geom_type == 'LineString':
            bank_lines.append(geom)
        elif geom.geom_type == 'MultiLineString':
            bank_lines.extend(geom.geoms)
    left_bank, right_bank = bank_lines[0], bank_lines[1]

    # Find the perpendicular bank width line at the nearest centerline point
    cl_pt = Point(cell['cx'], cell['cy'])

    # Project centerline point onto each bank to get left/right bank intersection
    pt_on_left = left_bank.interpolate(left_bank.project(cl_pt))
    pt_on_right = right_bank.interpolate(right_bank.project(cl_pt))

    fig, ax = plt.subplots(figsize=(14, 10))

    # Plot bank polygon (fill)
    poly_x, poly_y = polygon.exterior.xy
    ax.fill(poly_x, poly_y, alpha=0.15, color='cyan', label='Bank Polygon Mask')
    ax.plot(poly_x, poly_y, color='cyan', linewidth=1)

    # Plot dynamic linearly interpolated cross section bounds mask
    print("Evaluating cross section boundary interpolations along 1m intervals...")
    xs_mask_poly = DTMChannelModifier.create_cross_section_mask(CROSS_SECTION_FILE_PATH, BANK_LINE_FILE_PATH, interval=1.0)
    xs_poly_x, xs_poly_y = xs_mask_poly.exterior.xy
    ax.fill(xs_poly_x, xs_poly_y, alpha=0.1, color='magenta', label='Cross Section Interpolated Mask')
    ax.plot(xs_poly_x, xs_poly_y, color='magenta', linewidth=1.5, linestyle='--')

    # Plot bank lines
    for geom in banks.geometry:
        if geom.geom_type == 'LineString':
            x, y = geom.xy
            ax.plot(x, y, color='blue', linewidth=2, label='Banks')
        elif geom.geom_type == 'MultiLineString':
            for line in geom.geoms:
                x, y = line.xy
                ax.plot(x, y, color='blue', linewidth=2, label='Banks')

    # Plot centerline
    cl_x, cl_y = centerline.xy
    ax.plot(cl_x, cl_y, color='red', linestyle='--', linewidth=1.5, label='Centerline')

    # Plot the DTM cell
    ax.plot(cell['x'], cell['y'], marker='s', color='orange', markersize=12,
            markeredgecolor='black', markeredgewidth=1.5, zorder=5, label='DTM Cell')

    # Plot nearest centerline point
    ax.plot(cell['cx'], cell['cy'], marker='o', color='red', markersize=10,
            markeredgecolor='black', markeredgewidth=1.5, zorder=5,
            label='Nearest Centerline Point')

    # Draw line from cell to nearest centerline point
    ax.plot([cell['x'], cell['cx']], [cell['y'], cell['cy']],
            color='orange', linestyle=':', linewidth=2, 
            label=f"Cell → CL ({cell['dist_to_centerline']:.2f}m)")
            
    # Annotate distance near midpoint of the cell->CL line
    mid_cx = (cell['x'] + cell['cx']) / 2
    mid_cy = (cell['y'] + cell['cy']) / 2
    ax.text(mid_cx, mid_cy, f"Dist to CL: {cell['dist_to_centerline']:.2f}m",
            fontsize=10, color='orange', fontweight='bold',
            bbox=dict(facecolor='white', alpha=0.7, edgecolor='orange'),
            ha='right', va='bottom')

    # Draw bank width line (left bank to right bank through centerline point)
    ax.plot([pt_on_left.x, pt_on_right.x], [pt_on_left.y, pt_on_right.y],
            color='green', linewidth=3, alpha=0.8, label=f"Bank Width = {cell['bank_width']:.2f}m")

    # Mark bank intersection points
    ax.plot(pt_on_left.x, pt_on_left.y, marker='D', color='green', markersize=8,
            markeredgecolor='black', zorder=5)
    ax.plot(pt_on_right.x, pt_on_right.y, marker='D', color='green', markersize=8,
            markeredgecolor='black', zorder=5)

    # Annotate cross sections in plot (the bracketing ones)
    if upstream_line:
        up_x, up_y = upstream_line.xy
        ax.plot(up_x, up_y, color='black', linewidth=1.5, label='Cross Sections' if not downstream_line else "")
        pt_up, _ = nearest_points(upstream_line, cell_pt)
        ax.plot([cell_pt.x, pt_up.x], [cell_pt.y, pt_up.y], color='purple', linestyle='--', linewidth=2, 
                label=f"Min Dist to CS ({cell['min_dist_up']}m)" if upstream_line else "")
        ax.text(up_x[0], up_y[0], f"St: {cell['up_station']} (Upstream)", fontsize=8, color='black',
                bbox=dict(facecolor='white', alpha=0.6, edgecolor='none'))
                
    if downstream_line:
        dn_x, dn_y = downstream_line.xy
        ax.plot(dn_x, dn_y, color='black', linewidth=1.5, label='Cross Sections' if not upstream_line else "")
        pt_dn, _ = nearest_points(downstream_line, cell_pt)
        ax.plot([cell_pt.x, pt_dn.x], [cell_pt.y, pt_dn.y], color='purple', linestyle='--', linewidth=2,
                label=f"Min Dist to CS ({cell['min_dist_down']}m)" if not upstream_line else "")
        ax.text(dn_x[0], dn_y[0], f"St: {cell['down_station']} (Downstream)", fontsize=8, color='black',
                bbox=dict(facecolor='white', alpha=0.6, edgecolor='none'))

    # Annotate width at midpoint
    mid_x = (pt_on_left.x + pt_on_right.x) / 2
    mid_y = (pt_on_left.y + pt_on_right.y) / 2
    ax.text(mid_x, mid_y + 1, f"Bank Width: {cell['bank_width']:.2f}m",
            fontsize=11, color='green', fontweight='bold',
            bbox=dict(facecolor='white', alpha=0.8, edgecolor='green'),
            ha='center')

    # Annotate cell info with old and new Z
    ax.text(cell['x'] + 10, cell['y'] - 10,
            f"Cell ({cell['row']},{cell['col']})\nOld Z: {cell['dtm_z']}m\nNew Z: {cell['new_interpolated_z']}m",
            fontsize=10, color='brown', fontweight='bold',
            bbox=dict(facecolor='white', alpha=0.9, edgecolor='orange'))

    # Clean legend
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc='upper left', fontsize=9)

    ax.set_aspect('equal', adjustable='datalim')
    ax.grid(True, linestyle='--', alpha=0.5)
    ax.set_title("Single Cell Test: Bank Polygon, Nearest Centerline & Bank Width", fontsize=13)
    ax.set_xlabel("Easting (X)")
    ax.set_ylabel("Northing (Y)")

    plt.tight_layout()
    plot_path = os.path.join(OUTPUT_PATH, "single_cell_test.png")
    plt.savefig(plot_path, dpi=300)
    print(f"\nPlot saved to: {plot_path}")
    plt.show()


if __name__ == "__main__":
    test_single_cell()
