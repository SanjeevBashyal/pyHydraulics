from __future__ import annotations

import argparse
from pathlib import Path

from configProject import Config
from Automation.rasmapper import RasMapper2D


CONFIG_SOURCE = "folder"
MASTER_PROJECT_PATH = r"C:\Users\Ripple\Downloads\Turkey Flood\Group-4-Model"
SHEET_NAME = None

# None means "run every project listed in the active structure source".
# PROJECTS_TO_RUN: list[str] | None = None
PROJECTS_TO_RUN = ["ARDICLI", "CIGRI", "CUKUROREN", "CUKUROREN-T"]

# Default 2D build path. Use --step/--full/--prepare-only from the CLI to adjust.
# prepare keeps the legacy v01 helper generation, while the named steps below
# make landcover Manning's n, DSS, v01-style geometry installation/mesh
# regeneration, and Manning audits explicit before computing the plan.
WORKFLOW_STEPS = (
    "prepare",
    "prepare-mannings",
    "read-dss",
    "install-geometry",
    "apply-mannings",
    "check-mannings",
    "create-unsteady-plan",
    "compute-plan",
)
SKIP_TERRAIN = False
MESH_CELL_SIZE = 5.0
BREAKLINE_NEAR_SPACING = 0.5
BREAKLINE_NEAR_REPEATS = 5
BREAKLINE_FAR_SPACING = 3.0
LANDCOVER_CELL_SIZE = 2.0
PREFERRED_DSS_F_PART = "Q100"
DOWNSTREAM_BC_METHOD = "Normal Depth"
PLAN_COMPUTATION_INTERVAL = "6SEC"
SIMULATION_DURATION_HOURS = 24.0
INSTALL_TIMEOUT_SECONDS = 420
COMPUTE_TIMEOUT_SECONDS = 7200
SKIP_GEOMETRY_REGENERATION = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run 2D HEC-RAS/RAS Mapper project setup using configProject.py "
            "and the prepared DTM outputs from implementationDTM.py."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve inputs and validate paths without creating HEC-RAS files.")
    parser.add_argument("--project", action="append", help="Project name to run. Repeat for multiple projects.")
    parser.add_argument(
        "--step",
        action="append",
        help=(
            "Workflow step to run. Repeat for multiple steps. Common steps: "
            "prepare, prepare-mannings, read-dss, install-geometry, "
            "apply-mannings, check-mannings, create-unsteady-plan, compute-plan."
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Run the complete 2D workflow, including landcover Manning's n, "
            "DSS boundary reads, geometry installation/mesh regeneration, "
            "plan creation, and compute."
        ),
    )
    parser.add_argument("--prepare-only", action="store_true", help="Only prepare the RAS Mapper project shell and helper artifacts.")
    parser.add_argument("--compute", action="store_true", help="Append compute-plan to the selected workflow steps.")
    parser.add_argument("--no-compute", action="store_true", help="Remove compute-plan from the selected workflow.")
    parser.add_argument("--skip-terrain", action="store_true", default=SKIP_TERRAIN, help="Skip terrain HDF creation during prepare.")
    parser.add_argument("--continue-on-error", action="store_true", default=True, help="Continue running remaining projects after an error.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop immediately if one project fails.")
    parser.add_argument(
        "--skip-geometry-regeneration",
        action="store_true",
        default=SKIP_GEOMETRY_REGENERATION,
        help="Skip GUI geometry HDF regeneration during install-geometry.",
    )
    parser.add_argument(
        "--regenerate-geometry",
        dest="skip_geometry_regeneration",
        action="store_false",
        help="Run the RAS Mapper GUI mesh-regeneration step. This can hang on some projects.",
    )
    parser.add_argument("--install-timeout", type=int, default=INSTALL_TIMEOUT_SECONDS, help="Timeout in seconds for optional GUI geometry installation/regeneration.")
    parser.add_argument("--compute-timeout", type=int, default=COMPUTE_TIMEOUT_SECONDS, help="Timeout in seconds for compute-plan.")
    parser.add_argument(
        "--source",
        choices=["sheet", "folder", "auto"],
        help="Load project structure from the Google Sheet, a master folder path, or auto-fallback.",
    )
    parser.add_argument(
        "--master-project-path",
        help="Master project folder to use when --source folder is selected or auto falls back to the filesystem.",
    )
    parser.add_argument("--sheet-name", help="Optional Google Sheet tab name. Defaults to 'Structure'.")
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
    config.setup_essential_directories()
    return config


def selected_steps(args: argparse.Namespace) -> tuple[str, ...]:
    if args.prepare_only:
        steps = ["prepare"]
    elif args.full:
        steps = (
            [step for step in RasMapper2D.FULL_STEPS if step != "compute-plan"]
            if args.no_compute
            else ["full"]
        )
    elif args.step:
        steps = list(args.step)
    else:
        steps = list(WORKFLOW_STEPS)

    if args.compute and "compute-plan" not in {step.lower().replace("_", "-") for step in steps}:
        steps.append("compute-plan")
    if args.no_compute:
        steps = [
            step
            for step in steps
            if step.lower().replace("_", "-") not in {"compute", "run", "compute-plan"}
        ]

    return tuple(steps)


def build_rasmapper(config: Config) -> RasMapper2D:
    return RasMapper2D(
        config,
        mesh_cell_size=MESH_CELL_SIZE,
        breakline_near_spacing=BREAKLINE_NEAR_SPACING,
        breakline_near_repeats=BREAKLINE_NEAR_REPEATS,
        breakline_far_spacing=BREAKLINE_FAR_SPACING,
        landcover_cell_size=LANDCOVER_CELL_SIZE,
        preferred_dss_f_part=PREFERRED_DSS_F_PART,
        downstream_bc_method=DOWNSTREAM_BC_METHOD,
        plan_computation_interval=PLAN_COMPUTATION_INTERVAL,
        simulation_duration_hours=SIMULATION_DURATION_HOURS,
    )


def main() -> list[dict]:
    args = parse_args()
    config = build_config(args)
    runner = build_rasmapper(config)
    project_filter = args.project if args.project else PROJECTS_TO_RUN

    results = runner.process_structure_projects(
        projects=project_filter,
        steps=selected_steps(args),
        skip_terrain=args.skip_terrain,
        dry_run=args.dry_run,
        continue_on_error=not args.stop_on_error,
        install_timeout=args.install_timeout,
        compute_timeout_seconds=args.compute_timeout,
        skip_geometry_regeneration=args.skip_geometry_regeneration,
        compute_overwrite=True,
    )

    summary_path = Path(config.TEMP_PATH) / "implementation2d_summary.json"
    print(f"\n2D summary written to: {summary_path}")
    for result in results:
        status = "OK" if result.get("success") else "FAILED"
        print(f"[{status}] {result.get('project_name')}")
        print(f"  Output: {result.get('output_folder')}")
        print(f"  {result.get('message')}")

    return results


if __name__ == "__main__":
    main()