#!/usr/bin/env python3
"""
TURFM Steady 1D Screening

This script builds a 1D steady HEC-RAS model, selects the closest hydrology point
from the KMZ inside a river buffer, screens design discharges from Q1000 down to Q5,
writes per-flow runs under a model-specific `code_generated/<model>/runs` folder,
and leaves the final representative model in `code_generated/<model>`.

BANK_STATION_MODE controls how bank stations are placed:
- "snap": snap/refine to surveyed profile points
- "interpolate": insert exact bank-intersection points into cross section profile

RIVER_LINE_METHOD controls how the river centerline is derived:
- "simple_distance": legacy distance-based method using the grouped bank shapefile
- "perpendicular": midpoint-between-grouped-banks method, forced to start and end
  on the first and last cross sections
"""

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
import sys
import logging


def _find_repo_root(start_path: Path) -> Path:
    """Walk upward until the repository root containing ras_commander."""
    for candidate in (start_path, *start_path.parents):
        if (candidate / "ras_commander").is_dir():
            return candidate
    return start_path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _find_repo_root(SCRIPT_DIR)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
RAS_COMMANDER_ROOT = Path(r"F:\HEC_RAS_TURKEY\new_ras")
if str(RAS_COMMANDER_ROOT) not in sys.path:
    sys.path.insert(0, str(RAS_COMMANDER_ROOT))

from hecras import HECRAS

# Configuration
PROJECT_ROOT = SCRIPT_DIR
BUR_BUR_ROOT = PROJECT_ROOT / "1 Bur-Bur"
DEFAULT_MODEL_FOLDER = "BUR-BUR-MER-TASKAPI4_Rev_V1"
HEC_OUTPUT_ROOT = PROJECT_ROOT / "3 Hecras" / "code_generated"
HYDROLOGY_KMZ = PROJECT_ROOT / "2 Hydrology" / "Burdur Points.kmz"
PROJECTION_FILE = PROJECT_ROOT / "0 Proj" / "TUREF_CM30_projection.prj"
WORKING_FIXED_SHP_ROOT = PROJECT_ROOT / "working" / "fixed_shp"
SELF_EXAMPLE_ROOT = PROJECT_ROOT / "3 Hecras" / "self_example"
RAS_EXE_PATH = Path(r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe")

CENTERLINE_SAMPLES_PER_SEGMENT = 250
HYDROLOGY_BUFFER_METERS = 150.0
BANK_STATION_MODE = "snap"
RIVER_LINE_METHOD = "simple_distance"
ALL_FLOW_IN_SINGLE_PLAN = True
ADD_STRUCTURE = True


@dataclass(frozen=True)
class ModelConfig:
    model_folder: str
    model_dir: Path
    project_stem: str
    project_title: str
    hec_path: Path
    cross_section_csv: Path
    bank_lines_shp: Path
    structure_file: Path | None


def _combined_junction_shp(
    main_model: ModelConfig,
    tributary_model: ModelConfig,
) -> Path | None:
    """Return the saved combined junction shapefile when it exists."""
    combined_name = (
        f"{main_model.model_folder.replace('BUR-BUR-MER-', '')}"
        f"__{tributary_model.model_folder.replace('BUR-BUR-MER-', '')}_combined.shp"
    )
    candidate = WORKING_FIXED_SHP_ROOT / combined_name
    if candidate.exists():
        return candidate
    fallback = (
        WORKING_FIXED_SHP_ROOT / "BUR-BUR-MER-ATATURK-Rev-V1__ATATURK-T_combined.shp"
    )
    if fallback.exists():
        return fallback
    return None


def _junction_reference_geometry(main_model: ModelConfig) -> Path | None:
    """Return the manual self-example geometry file when present."""
    candidate = SELF_EXAMPLE_ROOT / f"{main_model.project_stem}.g01"
    if candidate.exists():
        return candidate
    matches = sorted(SELF_EXAMPLE_ROOT.glob("*.g01"))
    if matches:
        return matches[0]
    return None


def _junction_reference_project_name(
    reference_geometry: Path | None,
    fallback_name: str,
) -> str:
    """Use the reference project stem when available for combined outputs."""
    if reference_geometry is None:
        return fallback_name
    return reference_geometry.stem or fallback_name


def _read_river_name_from_csv(cross_section_csv: Path) -> str:
    """Read the source river name from the cross-section CSV header row."""
    with cross_section_csv.open(
        newline="", encoding="utf-8", errors="ignore"
    ) as handle:
        reader = csv.DictReader(handle)
        first_row = next(reader, None)
    if first_row is None:
        raise ValueError(f"No rows were found in {cross_section_csv}.")
    river_name = str(first_row.get("River", "")).strip()
    if not river_name:
        raise ValueError(f"Column 'River' was empty in {cross_section_csv}.")
    return river_name


def _infer_junction_output_name(
    main_model: ModelConfig,
    tributary_model: ModelConfig,
    fallback_name: str,
) -> str:
    """Infer a stable project stem from the source river names."""
    try:
        main_river = _read_river_name_from_csv(main_model.cross_section_csv)
        tributary_river = _read_river_name_from_csv(tributary_model.cross_section_csv)
        inferred = HECRAS.infer_junction_project_stem(
            main_river=main_river,
            tributary_river=tributary_river,
        )
        return inferred or fallback_name
    except Exception:
        return fallback_name


def _available_model_dirs() -> list[Path]:
    """Return sorted model folders under 1 Bur-Bur."""
    return sorted(path for path in BUR_BUR_ROOT.iterdir() if path.is_dir())


def _resolve_model_dir(model_folder: str) -> Path:
    """Resolve the user-provided model folder name to a Bur-Bur subfolder."""
    direct_path = Path(model_folder)
    if direct_path.is_dir():
        return direct_path.resolve()

    candidate = BUR_BUR_ROOT / model_folder
    if candidate.is_dir():
        return candidate

    matches = [
        path
        for path in _available_model_dirs()
        if path.name.casefold() == model_folder.casefold()
    ]
    if len(matches) == 1:
        return matches[0]

    available = ", ".join(path.name for path in _available_model_dirs())
    raise FileNotFoundError(
        f"Model folder '{model_folder}' was not found under {BUR_BUR_ROOT}. "
        f"Available folders: {available}"
    )


def _select_single_file(
    folder: Path,
    pattern: str,
    description: str,
) -> Path:
    """Return the single file matching the pattern inside folder."""
    candidates = sorted(folder.glob(pattern))
    if not candidates:
        raise FileNotFoundError(
            f"No {description} matching '{pattern}' was found in {folder}."
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise FileNotFoundError(
            f"Expected one {description} in {folder}, found {len(candidates)}: {names}"
        )
    return candidates[0]


def _select_optional_single_file(folder: Path, pattern: str) -> Path | None:
    """Return one matching file when present, otherwise None."""
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob(pattern))
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise FileNotFoundError(
            f"Expected at most one file matching '{pattern}' in {folder}, "
            f"found {len(candidates)}: {names}"
        )
    return candidates[0]


def _select_optional_structure_file(folder: Path) -> Path | None:
    for pattern in ("*.toml", "*.csv"):
        selected = _select_optional_single_file(folder, pattern)
        if selected is not None:
            return selected
    return None


def _select_single_subdir(folder: Path, pattern: str, description: str) -> Path:
    """Return the single subdirectory matching the pattern inside folder."""
    candidates = sorted(path for path in folder.glob(pattern) if path.is_dir())
    if not candidates:
        raise FileNotFoundError(
            f"No {description} matching '{pattern}' was found in {folder}."
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise FileNotFoundError(
            f"Expected one {description} in {folder}, found {len(candidates)}: {names}"
        )
    return candidates[0]


def _build_model_config(model_folder: str) -> ModelConfig:
    """Discover the model files for the requested Bur-Bur folder."""
    model_dir = _resolve_model_dir(model_folder)
    cross_section_dir = _select_single_subdir(
        model_dir,
        "KESIT_TESLIM*",
        "cross section folder",
    )
    bank_root_dir = _select_single_subdir(
        model_dir,
        "SEV_USTU*",
        "bank line folder",
    )
    roleve_dir = _select_single_subdir(
        model_dir,
        "ROLEVE*",
        "survey folder",
    )
    cross_section_csv = _select_single_file(
        cross_section_dir,
        "*.csv",
        "cross section CSV",
    )
    bank_lines_candidates = sorted(bank_root_dir.glob("*.shp"))
    if len(bank_lines_candidates) == 1:
        bank_lines_shp = bank_lines_candidates[0]
    else:
        bank_lines_dir = _select_single_subdir(
            bank_root_dir,
            "*",
            "bank line shapefile folder",
        )
        bank_lines_shp = _select_single_file(
            bank_lines_dir,
            "*.shp",
            "bank line shapefile",
        )
    structure_file = _select_optional_structure_file(roleve_dir / "structure_dim")
    return ModelConfig(
        model_folder=model_dir.name,
        model_dir=model_dir,
        project_stem=model_dir.name,
        project_title=model_dir.name,
        hec_path=HEC_OUTPUT_ROOT / model_dir.name,
        cross_section_csv=cross_section_csv,
        bank_lines_shp=bank_lines_shp,
        structure_file=structure_file,
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI parser for model selection."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the TURFM steady 1D screening workflow for one model folder "
            "inside '1 Bur-Bur'."
        )
    )
    parser.add_argument(
        "model_folder",
        nargs="?",
        default=DEFAULT_MODEL_FOLDER,
        help=(
            "Folder name under '1 Bur-Bur' or a direct path to a model folder. "
            f"Defaults to {DEFAULT_MODEL_FOLDER}."
        ),
    )
    parser.add_argument(
        "--main-folder",
        help="Main-stem folder name under '1 Bur-Bur' for a junction run.",
    )
    parser.add_argument(
        "--tributary-folder",
        help="Tributary folder name under '1 Bur-Bur' for a junction run.",
    )
    parser.add_argument(
        "--structure-file",
        "--structure-csv",
        dest="structure_file",
        help="Optional structure TOML or legacy CSV for single-model mode.",
    )
    parser.add_argument(
        "--main-structure-file",
        "--main-structure-csv",
        dest="main_structure_file",
        help="Optional structure TOML or legacy CSV for the main-stem junction model.",
    )
    parser.add_argument(
        "--tributary-structure-file",
        "--tributary-structure-csv",
        dest="tributary_structure_file",
        help="Optional structure TOML or legacy CSV for the tributary junction model.",
    )
    parser.add_argument(
        "--no-structures",
        action="store_true",
        help="Disable structure loading even when a structure file is discovered.",
    )
    parser.add_argument(
        "--all-flow-in-single-plan",
        action="store_true",
        help=(
            "Write all tested steady flows as profiles in one flow file and "
            "compute one plan instead of creating one run folder per flow."
        ),
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List the available folders under '1 Bur-Bur' and exit.",
    )
    return parser


def main(argv: list[str] | None = None):
    """Execute TURFM steady 1D screening workflow."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        print("Available Bur-Bur model folders:")
        for path in _available_model_dirs():
            print(path.name)
        return {"available_models": [path.name for path in _available_model_dirs()]}

    junction_mode = bool(args.main_folder or args.tributary_folder)
    if junction_mode and args.model_folder:
        parser.error(
            "Use either single-model mode with 'model_folder' or junction mode "
            "with '--main-folder' and '--tributary-folder', not both."
        )
    if junction_mode and (not args.main_folder or not args.tributary_folder):
        parser.error(
            "Junction mode requires both '--main-folder' and '--tributary-folder'."
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger(__name__)
    logger.info("Starting TURFM steady screening workflow")
    hec = HECRAS(ras_exe_path=RAS_EXE_PATH)
    all_flow_in_single_plan = bool(
        ALL_FLOW_IN_SINGLE_PLAN or args.all_flow_in_single_plan
    )

    if junction_mode:
        if all_flow_in_single_plan:
            logger.warning(
                "ALL_FLOW_IN_SINGLE_PLAN is currently implemented for "
                "single-model screening only; junction screening will use "
                "the existing per-flow run folders."
            )
        main_model = _build_model_config(args.main_folder)
        tributary_model = _build_model_config(args.tributary_folder)
        main_structure_file = None
        tributary_structure_file = None
        if ADD_STRUCTURE and not args.no_structures:
            main_structure_file = (
                Path(args.main_structure_file)
                if args.main_structure_file
                else main_model.structure_file
            )
            tributary_structure_file = (
                Path(args.tributary_structure_file)
                if args.tributary_structure_file
                else tributary_model.structure_file
            )
        combined_name = f"{main_model.model_folder}__{tributary_model.model_folder}"
        combined_bank_shp = _combined_junction_shp(main_model, tributary_model)
        reference_geometry = _junction_reference_geometry(main_model)
        output_name = _junction_reference_project_name(
            reference_geometry,
            _infer_junction_output_name(
                main_model,
                tributary_model,
                combined_name,
            ),
        )
        output_folder = HEC_OUTPUT_ROOT / output_name
        logger.info("Selected main model folder: %s", main_model.model_folder)
        logger.info(
            "Selected tributary model folder: %s",
            tributary_model.model_folder,
        )
        logger.info("Combined junction name: %s", combined_name)
        logger.info("Reference-driven output name: %s", output_name)
        logger.info("Junction output folder: %s", output_folder)
        logger.info("Combined bank shapefile: %s", combined_bank_shp)
        logger.info("Reference junction geometry: %s", reference_geometry)
        logger.info("Main structure file: %s", main_structure_file)
        logger.info("Tributary structure file: %s", tributary_structure_file)
        screening = hec.screen_steady_junction_flows_from_kmz(
            project_folder=output_folder,
            project_stem=output_name,
            project_title=output_name,
            main_cross_section_csv=main_model.cross_section_csv,
            main_bank_lines_shp=main_model.bank_lines_shp,
            tributary_cross_section_csv=tributary_model.cross_section_csv,
            tributary_bank_lines_shp=tributary_model.bank_lines_shp,
            main_structure_csv=main_structure_file,
            tributary_structure_csv=tributary_structure_file,
            combined_bank_lines_shp=combined_bank_shp,
            hydrology_kmz=HYDROLOGY_KMZ,
            buffer_distance=HYDROLOGY_BUFFER_METERS,
            reference_geometry_file=reference_geometry,
            projection_file=PROJECTION_FILE,
            centerline_samples_per_segment=CENTERLINE_SAMPLES_PER_SEGMENT,
            bank_station_mode=BANK_STATION_MODE,
            river_line_method=RIVER_LINE_METHOD,
        )
    else:
        model = _build_model_config(args.model_folder)
        structure_file = None
        if ADD_STRUCTURE and not args.no_structures:
            structure_file = (
                Path(args.structure_file)
                if args.structure_file
                else model.structure_file
            )
        logger.info("Selected model folder: %s", model.model_folder)
        logger.info("Cross-section CSV: %s", model.cross_section_csv)
        logger.info("Bank lines shapefile: %s", model.bank_lines_shp)
        logger.info("Structure file: %s", structure_file)
        logger.info("Output folder: %s", model.hec_path)
        screening = hec.screen_steady_flows_from_kmz(
            project_folder=model.hec_path,
            project_stem=model.project_stem,
            project_title=model.project_title,
            cross_section_csv=model.cross_section_csv,
            bank_lines_shp=model.bank_lines_shp,
            structure_csv=structure_file,
            hydrology_kmz=HYDROLOGY_KMZ,
            buffer_distance=HYDROLOGY_BUFFER_METERS,
            projection_file=PROJECTION_FILE,
            centerline_samples_per_segment=CENTERLINE_SAMPLES_PER_SEGMENT,
            bank_station_mode=BANK_STATION_MODE,
            river_line_method=RIVER_LINE_METHOD,
            all_flows_in_single_plan=all_flow_in_single_plan,
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
