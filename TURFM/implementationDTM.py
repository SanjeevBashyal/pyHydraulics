from __future__ import annotations

import argparse

from configProject import Config
from Automation.callDTM import DTM


CONFIG_SOURCE = "folder"
MASTER_PROJECT_PATH = r"C:\Users\Ripple\Downloads\Turkey Flood\Group-0"
SHEET_NAME = None

PROJECTS_TO_RUN: list[str] | None = None
TARGET_RES = 0.1
BUFFER_M = 20.0
BLEND_TYPE = "cubic"
BANK_OFFSET_M = 0.2
FULL_CROSS_SECTION_WEIGHT_DISTANCE_M = 1.5
TRANSITION_TO_DTM_DISTANCE_M = 5.0
JUNCTION_TOLERANCE = 50.0
PERIMETER_OFFSET_M = 500.0
WRITE_INTERMEDIATE_TIFS = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DTM interpolation using either the Structure sheet or a master project folder."
    )
    parser.add_argument("--project", action="append", help="Project name to run. Repeat for multiple projects.")
    parser.add_argument(
        "--source",
        choices=["sheet", "folder", "auto"],
        help="Load project structure from the Google Sheet, a master folder path, or auto-fallback.",
    )
    parser.add_argument(
        "--master-project-path",
        help="Master project folder to use when --source folder is selected or auto falls back to the filesystem.",
    )
    parser.add_argument(
        "--sheet-name",
        help="Optional Google Sheet tab name. Defaults to 'Structure'.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    source = args.source or CONFIG_SOURCE
    master_project_path = args.master_project_path or MASTER_PROJECT_PATH
    sheet_name = args.sheet_name or SHEET_NAME
    config = Config(
        structure_source=source,
        master_project_path=master_project_path,
        project_folder=master_project_path,
        sheet_name=sheet_name,
    )
    config.BLEND_TYPE = BLEND_TYPE
    return config


if __name__ == "__main__":
    args = parse_args()
    DTM(build_config(args)).process_structure_projects(
        projects=args.project if args.project else PROJECTS_TO_RUN,
        target_res=TARGET_RES,
        buffer_m=BUFFER_M,
        blend_type=BLEND_TYPE,
        bank_offset_m=BANK_OFFSET_M,
        full_cross_section_weight_distance_m=FULL_CROSS_SECTION_WEIGHT_DISTANCE_M,
        transition_to_dtm_distance_m=TRANSITION_TO_DTM_DISTANCE_M,
        junction_tolerance=JUNCTION_TOLERANCE,
        perimeter_offset_m=PERIMETER_OFFSET_M,
        write_intermediate=WRITE_INTERMEDIATE_TIFS,
    )
