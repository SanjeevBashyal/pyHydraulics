#!/usr/bin/env python3
"""
TURFM Steady 1D Screening

This script builds a 1D steady HEC-RAS model, selects the closest hydrology point 
from the KMZ inside a river buffer, screens design discharges from Q1000 down to Q5, 
writes per-flow runs under code_generated/runs, and leaves the final representative 
model in code_generated.

BANK_STATION_MODE controls how bank stations are placed:
- "snap": snap/refine to surveyed profile points
- "interpolate": insert exact bank-intersection points into cross section profile

RIVER_LINE_METHOD controls how the river centerline is derived:
- "simple_distance": legacy distance-based method using the grouped bank shapefile
- "perpendicular": midpoint-between-grouped-banks method, forced to start and end
  on the first and last cross sections
"""

from pathlib import Path
import sys
import logging

# Add repository root to path if needed
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hecras import HECRAS

# Configuration
PROJECT_ROOT = Path(r"F:\HEC_RAS_TURKEY\new_ras\project_folder")
PROJECT_STEM = "BUR-BUR-MER-ATATURK-T"
PROJECT_TITLE = "BUR-BUR-MER-ATATURK-T"

HEC_PATH = PROJECT_ROOT / "3 Hecras" / "code_generated"
CROSS_SECTION_CSV = (
    PROJECT_ROOT
    / "1 Bur-Bur"
    / "BUR-BUR-MER-ATATURK-T"
    / "KESIT_TESLIM"
    / "BUR-BUR-MER-ATATURK-T_KESIT_TESLIM.csv"
)
BANK_LINES_SHP = (
    PROJECT_ROOT
    / "1 Bur-Bur"
    / "BUR-BUR-MER-ATATURK-T"
    / "SEV_USTU"
    / "BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp"
)
HYDROLOGY_KMZ = PROJECT_ROOT / "2 Hydrology" / "Burdur Points.kmz"
PROJECTION_FILE = PROJECT_ROOT / "0 Proj" / "TUREF_CM30_projection.prj"
RAS_EXE_PATH = Path(r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe")

CENTERLINE_SAMPLES_PER_SEGMENT = 500
HYDROLOGY_BUFFER_METERS = 150.0
BANK_STATION_MODE = "snap"
RIVER_LINE_METHOD = "simple_distance"


def main():
    """Execute TURFM steady 1D screening workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    logger = logging.getLogger(__name__)
    logger.info("Starting TURFM steady screening workflow")
    
    # Initialize HECRAS
    hec = HECRAS(ras_exe_path=RAS_EXE_PATH)
    
    # Run screening
    screening = hec.screen_steady_flows_from_kmz(
        project_folder=HEC_PATH,
        project_stem=PROJECT_STEM,
        project_title=PROJECT_TITLE,
        cross_section_csv=CROSS_SECTION_CSV,
        bank_lines_shp=BANK_LINES_SHP,
        hydrology_kmz=HYDROLOGY_KMZ,
        buffer_distance=HYDROLOGY_BUFFER_METERS,
        projection_file=PROJECTION_FILE,
        centerline_samples_per_segment=CENTERLINE_SAMPLES_PER_SEGMENT,
        bank_station_mode=BANK_STATION_MODE,
        river_line_method=RIVER_LINE_METHOD,
    )
    
    # Output results
    screening_info = screening.to_dict()
    print("\n=== Screening Results ===")
    print(screening.message)
    print(f"Report CSV: {screening.report_csv}")
    print(f"Report TXT: {screening.report_txt}")
    print(f"Final model return period: {screening.final_model_return_period}")
    print(f"Final model discharge (cms): {screening.final_model_flow_cms}")
    print(f"Maximum safe return period: {screening.max_safe_return_period}")
    print(f"Maximum safe discharge (cms): {screening.max_safe_flow_cms}")
    
    logger.info("TURFM screening workflow completed")
    return screening_info


if __name__ == "__main__":
    main()
