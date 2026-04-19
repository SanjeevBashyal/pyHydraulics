import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from shapely.geometry import LineString

from Automation.DTM import DTMChannelModifier

# Setup Paths based on your project structure
PWD = os.getcwd()
CROSS_SECTION_FILE_PATH = os.path.join(PWD, "cross-section.csv")
BANK_LINE_FILE_PATH = os.path.join(PWD, r"0 HECRAS-Template\1 Bur-Bur\BUR-BUR-MER-ATATURK-T\SEV_USTU\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp")
OUTPUT_PATH = os.path.join(PWD, r"0 HECRAS-Template\5 Outputs")

if not os.path.exists(OUTPUT_PATH):
    os.makedirs(OUTPUT_PATH, exist_ok=True)

out_csv = os.path.join(OUTPUT_PATH, "interpolated_cross_sections_0.1m.csv")

def plot_interpolated_sections():
    print("Running interpolation...")
    interpolated_df = DTMChannelModifier.interpolate_cross_sections(
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        step_m=0.1,    # Extract elevation every 0.1m from the centerline
        out_csv=out_csv
    )

    if interpolated_df.empty:
        print("Error: No interpolated data produced.")
        return

    print("Loading original CSV for comparison...")
    orig_df = pd.read_csv(CROSS_SECTION_FILE_PATH)

    print("Generating centerline for alignment of original data...")
    centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(BANK_LINE_FILE_PATH)
    if centerline_gdf.empty:
        print("Failed to generate bank centerline.")
        return
    centerline = centerline_gdf.geometry.iloc[0]

    stations = orig_df['Station'].unique()
    num_stations = len(stations)
    
    print(f"Plotting {num_stations} stations...")
    
    # Create subplots
    fig, axes = plt.subplots(num_stations, 1, figsize=(10, 4 * num_stations), sharex=False)
    if num_stations == 1:
        axes = [axes]

    for ax, stat in zip(axes, stations):
        # Process original station
        stat_orig = orig_df[orig_df['Station'] == stat].copy()
        coords_3d = stat_orig[['X', 'Y', 'Z']].values
        
        if len(coords_3d) < 2:
            continue
            
        xs_line = LineString(coords_3d)
        intersection = xs_line.intersection(centerline)
        
        # Robustly calculate center distance
        center_dist = 0
        if not intersection.is_empty:
            if intersection.geom_type in ['MultiPoint', 'GeometryCollection']:
                pts = [geom for geom in getattr(intersection, 'geoms', [intersection]) if geom.geom_type == 'Point']
                if pts:
                    intersection = pts[0]
            if intersection.geom_type == 'Point':
                center_dist = xs_line.project(intersection)

        # Calculate original distances from center
        seg_lengths = [np.hypot(coords_3d[i+1][0] - coords_3d[i][0], coords_3d[i+1][1] - coords_3d[i][1]) for i in range(len(coords_3d)-1)]
        cum_dist = np.insert(np.cumsum(seg_lengths), 0, 0)
        dist_orig = cum_dist - center_dist

        # Plot Original
        ax.plot(dist_orig, stat_orig['Z'], marker='o', markersize=6, linestyle='-', linewidth=2, color='gray', label='Original Section', alpha=0.6)

        # Plot Interpolated
        stat_interp = interpolated_df[interpolated_df['Station'] == stat]
        if not stat_interp.empty:
            ax.plot(stat_interp['Distance_from_Center'], stat_interp['Z'], marker='.', markersize=4, linestyle='--', color='red', label='Interpolated Section (0.1m)', alpha=1.0)
            
        ax.set_title(f"Cross Section - Station {stat}")
        ax.set_xlabel("Distance from Center Bank Line (m)")
        ax.set_ylabel("Elevation (Z)")
        ax.axvline(x=0, color='blue', linestyle=':', label='Centerline')
        ax.grid(True, linestyle='--', alpha=0.6)
        ax.legend()

    plt.tight_layout()
    plot_path = os.path.join(OUTPUT_PATH, "cross_section_comparison.png")
    plt.savefig(plot_path, dpi=300)
    print(f"\nSuccessfully generated and saved plot to: {plot_path}")
    
    plt.show()

if __name__ == "__main__":
    plot_interpolated_sections()