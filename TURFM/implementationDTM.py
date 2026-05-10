from __future__ import annotations

import argparse

from configProject import Config
from Automation.DTM import DTM


CONFIG_SOURCE = "folder"
MASTER_PROJECT_PATH = r"C:\Users\Ripple\Downloads\Turkey Flood\Group-4-Model"

# PROJECTS_TO_RUN: list[str] | None = None
# PROJECTS_TO_RUN = ["BUYUKGOKCELI"]
# PROJECTS_TO_RUN = ["ARDICLI", "CIGRI", "CUKUROREN", "CUKUROREN-T"]
# PROJECTS_TO_RUN = ["ECE2", "EVCILER1", "KILCAN"]
PROJECTS_TO_RUN = ["KILCAN"]

TARGET_RES = 0.1
BUFFER_M = 20
BLEND_TYPE = "cubic"
BANK_OFFSET_M = 0.2
SKEWNESS_CORRECTION = False
CENTERLINE_NORMAL_SAMPLE_DISTANCE_M = 3.0
BUILDING_LIFT_M = 12.0
FULL_CROSS_SECTION_WEIGHT_DISTANCE_M = 1.5
TRANSITION_TO_DTM_DISTANCE_M = 5.0
JUNCTION_TOLERANCE = 50.0
JUNCTION_CENTERLINE_GAP_M = 0.5
JUNCTION_BANK_CLIP_BUFFER_M = 5.0
JUNCTION_CLIP_CROSS_SECTION_COUNT = 2
JUNCTION_HALF_SECTION_INTERPOLATION = True
JUNCTION_BANK_STRUCTURE_PROTECTION_M = 1.0
PERIMETER_OFFSET_M = 200.0
WRITE_INTERMEDIATE_TIFS = True
SPLIT_DISCONNECTED_COMPONENTS = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DTM interpolation using the master project folder structure."
    )
    parser.add_argument("--project", action="append", help="Project name to run. Repeat for multiple projects.")
    parser.add_argument(
        "--source",
        choices=["folder"],
        help="Load project structure from a master folder path.",
    )
    parser.add_argument(
        "--master-project-path",
        help="Master project folder to use.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    source = args.source or CONFIG_SOURCE
    master_project_path = args.master_project_path or MASTER_PROJECT_PATH
    config = Config(
        structure_source=source,
        master_project_path=master_project_path,
        project_folder=master_project_path,
    )
    config.BLEND_TYPE = BLEND_TYPE
    config.setup_essential_directories()
    return config


if __name__ == "__main__":
    args = parse_args()
    DTM(build_config(args)).process_structure_projects(
        projects=args.project if args.project else PROJECTS_TO_RUN,
        target_res=TARGET_RES,
        buffer_m=BUFFER_M,
        blend_type=BLEND_TYPE,
        bank_offset_m=BANK_OFFSET_M,
        skewness_correction=SKEWNESS_CORRECTION,
        centerline_normal_sample_distance_m=CENTERLINE_NORMAL_SAMPLE_DISTANCE_M,
        building_lift_m=BUILDING_LIFT_M,
        full_cross_section_weight_distance_m=FULL_CROSS_SECTION_WEIGHT_DISTANCE_M,
        transition_to_dtm_distance_m=TRANSITION_TO_DTM_DISTANCE_M,
        junction_tolerance=JUNCTION_TOLERANCE,
        centerline_gap_m=JUNCTION_CENTERLINE_GAP_M,
        junction_bank_clip_buffer_m=JUNCTION_BANK_CLIP_BUFFER_M,
        junction_clip_cross_section_count=JUNCTION_CLIP_CROSS_SECTION_COUNT,
        junction_half_section_interpolation=JUNCTION_HALF_SECTION_INTERPOLATION,
        junction_bank_structure_protection_m=JUNCTION_BANK_STRUCTURE_PROTECTION_M,
        perimeter_offset_m=PERIMETER_OFFSET_M,
        write_intermediate=WRITE_INTERMEDIATE_TIFS,
        split_disconnected_components=SPLIT_DISCONNECTED_COMPONENTS,
    )
