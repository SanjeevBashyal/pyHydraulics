import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import geopandas as gpd
from shapely.geometry import LineString

from Automation.DTM import DTMChannelModifier

# Setup Paths
PWD = os.getcwd()
CROSS_SECTION_FILE_PATH = os.path.join(PWD, "cross-section.csv")
BANK_LINE_FILE_PATH = os.path.join(PWD, r"0 HECRAS-Template\1 Bur-Bur\BUR-BUR-MER-ATATURK-T\SEV_USTU\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp")
OUTPUT_PATH = os.path.join(PWD, r"0 HECRAS-Template\5 Outputs")

if not os.path.exists(OUTPUT_PATH):
    os.makedirs(OUTPUT_PATH, exist_ok=True)

out_widths_csv = os.path.join(OUTPUT_PATH, "bank_widths.csv")


def plot_bank_widths():
    print("Calculating cross-section bank widths...")
    widths_df = DTMChannelModifier.calculate_bank_widths(
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        out_csv=out_widths_csv
    )

    if widths_df.empty:
        print("No widths calculated.")
        return

    print("Loading geometries for plotting...")
    df = pd.read_csv(CROSS_SECTION_FILE_PATH)
    banks = gpd.read_file(BANK_LINE_FILE_PATH)

    centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks)
    centerline = centerline_gdf.geometry.iloc[0]

    fig, ax = plt.subplots(figsize=(14, 10))

    # Plot Banks
    for geom in banks.geometry:
        if geom.geom_type == 'LineString':
            x, y = geom.xy
            ax.plot(x, y, color='blue', linewidth=2, label='Banks')
        elif geom.geom_type == 'MultiLineString':
            for line in geom.geoms:
                x, y = line.xy
                ax.plot(x, y, color='blue', linewidth=2, label='Banks')

    # Plot Centerline
    x, y = centerline.xy
    ax.plot(x, y, color='red', linestyle='--', linewidth=1.5, label='Centerline')

    group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
    if not group_cols:
        group_cols = ['Station']

    first_xs = True
    for name, group in df.groupby(group_cols):
        coords_3d = group[['X', 'Y', 'Z']].values
        if len(coords_3d) < 2:
            continue

        x_xs = [pt[0] for pt in coords_3d]
        y_xs = [pt[1] for pt in coords_3d]

        ax.plot(x_xs, y_xs, color='black', linewidth=1.5,
                label='Cross Sections' if first_xs else "")
        first_xs = False

        stat = name if not isinstance(name, tuple) else name[-1]

        # Label station number
        ax.text(x_xs[0], y_xs[0], f"St {stat}", fontsize=8, color='black',
                bbox=dict(facecolor='white', alpha=0.6, edgecolor='none'))

    # Draw bank width segments and annotate
    for _, row in widths_df.iterrows():
        lx, ly = row['Left_Bank_X'], row['Left_Bank_Y']
        rx, ry = row['Right_Bank_X'], row['Right_Bank_Y']
        w = row['Bank_Width']

        # Draw the bank-to-bank segment
        ax.plot([lx, rx], [ly, ry], color='green', linewidth=2.5, alpha=0.7,
                label='Bank Width' if _ == 0 else "")

        # Annotate at midpoint
        mx, my = (lx + rx) / 2, (ly + ry) / 2
        ax.text(mx, my, f"{w:.1f}m", fontsize=9, color='green',
                fontweight='bold',
                bbox=dict(facecolor='white', alpha=0.8, edgecolor='none'))

    # Clean legend
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc='upper left')

    ax.set_aspect('equal', adjustable='datalim')
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.set_title("Cross Sections: Bank-to-Bank Widths")
    ax.set_xlabel("Easting (X)")
    ax.set_ylabel("Northing (Y)")

    plt.tight_layout()
    plot_path = os.path.join(OUTPUT_PATH, "map_bank_widths.png")
    plt.savefig(plot_path, dpi=300)
    print(f"\nPlot saved to: {plot_path}")
    plt.show()


if __name__ == "__main__":
    plot_bank_widths()
