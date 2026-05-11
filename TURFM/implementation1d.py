from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import re
import sys
from typing import Iterable, TYPE_CHECKING


PWD = Path(__file__).resolve().parent
if str(PWD) not in sys.path:
    sys.path.insert(0, str(PWD))
AUTOMATION_PATH = PWD / "Automation"
if str(AUTOMATION_PATH) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_PATH))

from configProject import Config, SubProjectPaths

if TYPE_CHECKING:
    from hecras import HECRAS


# =============================================================================
# User configuration
# =============================================================================

# Project structure is loaded directly from MASTER_PROJECT_PATH.
# MASTER_PROJECT_PATH: str | None = None

CONFIG_SOURCE = "folder"
MASTER_PROJECT_PATH = r"C:\Users\Ripple\Downloads\Turkey Flood\Group-4-Model"

# None means "run every project listed in the active structure source".
# PROJECTS_TO_RUN: list[str] | None = None
# PROJECTS_TO_RUN = ["ARDICLI","CIGRI","CUKUROREN","CUKUROREN-T"]
PROJECTS_TO_RUN = ["ARDICLI"]

# Optional manual pairing. If omitted, network.csv is used when available.
# Each pair is (main reach, tributary reach).
# Example:
# JUNCTION_PAIRS_BY_PROJECT = {"ATATURK": [("ATATURK-V1", "ATATURK-T")]}
JUNCTION_PAIRS_BY_PROJECT: dict[str, list[tuple[str, str]]] = {}

USE_STRUCTURES = True
HYDROLOGY_BUFFER_METERS = 150.0
CENTERLINE_SAMPLES_PER_SEGMENT = 500
BANK_STATION_MODE = "snap"
RIVER_LINE_METHOD = "simple_distance"
RETURN_PERIODS: list[str] | None = None
ALL_FLOW_IN_SINGLE_PLAN = True
PREPARE_GEOMETRY_HDF = True
RUN_UNCONNECTED_AS_SEPARATE_PROJECTS = True
USE_EXISTING_PROJECT_GEOMETRY_AS_REFERENCE = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("implementation1d")

@dataclass(frozen=True)
class ModelInput:
    project_name: str
    sub_project_name: str
    project_stem: str
    project_title: str
    paths: SubProjectPaths
    structure_csv: Path | None


@dataclass
class WorkflowResult:
    project_name: str
    run_type: str
    output_folder: str
    sub_projects: list[str]
    success: bool
    message: str
    result: dict | None = None


def discover_project_subprojects(config: Config) -> dict[str, list[str]]:
    """Read project/sub-project ordering from the active structure source."""
    return config.discover_project_subprojects()


def filtered_projects(
    project_subprojects: dict[str, list[str]],
    selected_projects: Iterable[str] | None,
) -> dict[str, list[str]]:
    if selected_projects is None:
        return project_subprojects

    normalized_selection = {_normalize_name(project) for project in selected_projects}
    return {
        project: sub_projects
        for project, sub_projects in project_subprojects.items()
        if _normalize_name(project) in normalized_selection
    }


def build_model_input(
    config: Config,
    project_name: str,
    sub_project_name: str,
    use_structures: bool,
) -> ModelInput:
    paths = config.get_sub_project_paths(project_name, sub_project_name)
    project_stem = project_stem_from_cross_section(Path(paths.cross_section_file_path))

    return ModelInput(
        project_name=paths.project_name,
        sub_project_name=paths.sub_project_name,
        project_stem=project_stem,
        project_title=project_stem,
        paths=paths,
        structure_csv=find_structure_csv(Path(paths.sub_project_path)) if use_structures else None,
    )


def project_stem_from_cross_section(cross_section_csv: Path) -> str:
    stem = cross_section_csv.stem
    return re.sub(r"_KESIT_TESLIM(?:[_-]?V\d+)?$", "", stem, flags=re.IGNORECASE)


def find_structure_csv(sub_project_path: Path) -> Path | None:
    """Structure tables are optional; return None when the sub-project has none."""
    candidates: list[Path] = []

    for roleve_path in sub_project_path.glob("ROLEVE*"):
        if not roleve_path.is_dir():
            continue
        candidates.extend(roleve_path.glob("structure_dim/*.csv"))
        candidates.extend(
            path
            for path in roleve_path.rglob("*.csv")
            if "structure" in path.name.casefold()
        )

    if not candidates:
        return None

    return sorted(candidates, key=version_sort_key, reverse=True)[0]


def find_essential_file(config: Config, preferred_name: str, pattern: str) -> Path:
    essentials_path = Path(config.ESSENTIALS_PATH)
    preferred_path = essentials_path / preferred_name
    if preferred_path.exists():
        return preferred_path

    candidates = sorted(essentials_path.glob(pattern), key=version_sort_key, reverse=True)
    if candidates:
        return candidates[0]

    raise FileNotFoundError(
        f"Could not find {preferred_name!r} or pattern {pattern!r} in {essentials_path}"
    )


def find_network_csv(config: Config) -> Path | None:
    preferred = Path(config.ESSENTIALS_PATH) / "network.csv"
    if preferred.exists():
        return preferred

    candidates = sorted(Path(config.PROJECT_FOLDER).glob("0*Essentials*/network.csv"))
    if candidates:
        return candidates[0]
    return None


def read_network_pairs(config: Config) -> list[tuple[str, str]]:
    network_csv = find_network_csv(config)
    if network_csv is None:
        return []

    with network_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            return []

        field_lookup = {field.strip().casefold(): field for field in reader.fieldnames}
        from_field = field_lookup.get("from") or reader.fieldnames[0]
        to_field = field_lookup.get("to") or reader.fieldnames[1]
        pairs = []
        for row in reader:
            from_name = str(row.get(from_field, "")).strip()
            to_name = str(row.get(to_field, "")).strip()
            if from_name and to_name:
                pairs.append((from_name, to_name))
        return pairs


def network_pairings_for_project(
    config: Config,
    models: list[ModelInput],
    network_pairs: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    model_aliases: list[tuple[ModelInput, set[str]]] = []
    for model in models:
        aliases = {
            model.sub_project_name,
            model.project_stem,
            Path(model.paths.sub_project_path).name,
            Path(model.paths.cross_section_file_path).stem,
        }
        model_aliases.append((model, {_normalize_name(alias) for alias in aliases if alias}))

    def find_model(name: str) -> ModelInput | None:
        normalized = _normalize_name(name)
        for model, aliases in model_aliases:
            if any(_names_match(normalized, alias) for alias in aliases):
                return model
        return None

    pairings: list[tuple[str, str]] = []
    pairs = network_pairs if network_pairs is not None else read_network_pairs(config)
    for from_name, to_name in pairs:
        tributary_model = find_model(from_name)
        main_model = find_model(to_name)
        if tributary_model is None or main_model is None:
            continue
        if tributary_model.sub_project_name == main_model.sub_project_name:
            continue
        pairings.append((main_model.sub_project_name, tributary_model.sub_project_name))

    return pairings


def find_combined_bank_lines_shp(
    config: Config,
    main_model: ModelInput,
    tributary_model: ModelInput,
) -> Path | None:
    gis_project_path = Path(config.get_gis_project_path(main_model.project_name))
    if not gis_project_path.is_dir():
        logger.warning(
            "GIS project folder was not found for %s; junction run will not use "
            "a DTM combined SEV_USTU bank-line shapefile: %s",
            main_model.project_name,
            gis_project_path,
        )
        return None

    candidates = sorted(
        gis_project_path.rglob("*_SEV_USTU_combined.shp"),
        key=version_sort_key,
        reverse=True,
    )
    if not candidates:
        logger.warning(
            "No DTM combined bank-line shapefile matching '*_SEV_USTU_combined.shp' "
            "was found in %s for junction %s + %s.",
            gis_project_path,
            main_model.sub_project_name,
            tributary_model.sub_project_name,
        )
        return None

    main_identifiers = {
        _normalize_name(value)
        for value in (
            main_model.sub_project_name,
            main_model.project_stem,
            main_model.project_stem.replace("BUR-BUR-MER-", ""),
        )
        if value
    }
    tributary_identifiers = {
        _normalize_name(value)
        for value in (
            tributary_model.sub_project_name,
            tributary_model.project_stem,
            tributary_model.project_stem.replace("BUR-BUR-MER-", ""),
        )
        if value
    }

    def mentions_model(path: Path, identifiers: set[str]) -> bool:
        stem = _normalize_name(path.stem)
        return any(identifier and identifier in stem for identifier in identifiers)

    matched_candidates = [
        candidate
        for candidate in candidates
        if mentions_model(candidate, main_identifiers)
        and mentions_model(candidate, tributary_identifiers)
    ]
    if matched_candidates:
        selected = matched_candidates[0]
        logger.info("Using DTM combined SEV_USTU bank lines for junction: %s", selected)
        return selected

    if len(candidates) == 1:
        selected = candidates[0]
        logger.warning(
            "Using the only DTM combined SEV_USTU bank-line shapefile found in %s, "
            "but its name did not match both junction reaches %s and %s: %s",
            gis_project_path,
            main_model.sub_project_name,
            tributary_model.sub_project_name,
            selected,
        )
        return selected

    logger.warning(
        "Found %s DTM combined SEV_USTU bank-line shapefiles in %s, but none matched "
        "both junction reaches %s and %s. Junction run will fall back to source "
        "SEV_USTU remapping.",
        len(candidates),
        gis_project_path,
        main_model.sub_project_name,
        tributary_model.sub_project_name,
    )

    return None


def find_reference_geometry(config: Config, project_name: str, preferred_stem: str) -> Path | None:
    hecras_project_path = Path(config.get_hecras_project_path(project_name))
    output_project_path = Path(config.get_output_project_path(project_name))
    if USE_EXISTING_PROJECT_GEOMETRY_AS_REFERENCE:
        for root in (output_project_path, hecras_project_path):
            preferred = root / f"{preferred_stem}.g01"
            if preferred.exists():
                return preferred

    for root in [
        output_project_path / "self_example",
        hecras_project_path / "self_example",
        Path(config.HEC_PATH) / "self_example",
    ]:
        if not root.is_dir():
            continue
        preferred = root / f"{preferred_stem}.g01"
        if preferred.exists():
            return preferred
        matches = sorted(root.glob("*.g01"), key=version_sort_key, reverse=True)
        if matches:
            return matches[0]

    return None


def run_single_model(
    hec: HECRAS,
    config: Config,
    model: ModelInput,
    hydrology_kmz: Path,
    projection_file: Path,
    dry_run: bool,
) -> WorkflowResult:
    output_folder = Path(model.paths.output_sub_project_path)
    kwargs = {
        "project_folder": output_folder,
        "project_stem": model.project_stem,
        "project_title": model.project_title,
        "cross_section_csv": Path(model.paths.cross_section_file_path),
        "bank_lines_shp": Path(model.paths.bank_line_file_path),
        "structure_csv": model.structure_csv,
        "hydrology_kmz": hydrology_kmz,
        "buffer_distance": HYDROLOGY_BUFFER_METERS,
        "projection_file": projection_file,
        "centerline_samples_per_segment": CENTERLINE_SAMPLES_PER_SEGMENT,
        "bank_station_mode": BANK_STATION_MODE,
        "river_line_method": RIVER_LINE_METHOD,
        "return_periods": RETURN_PERIODS,
        "all_flows_in_single_plan": ALL_FLOW_IN_SINGLE_PLAN,
        "prepare_geometry_hdf": PREPARE_GEOMETRY_HDF,
    }

    logger.info("Single model %s/%s -> %s", model.project_name, model.sub_project_name, output_folder)
    if dry_run:
        return WorkflowResult(
            project_name=model.project_name,
            run_type="single",
            output_folder=str(output_folder),
            sub_projects=[model.sub_project_name],
            success=True,
            message="Dry run assembled single-model arguments.",
            result=serializable_kwargs(kwargs),
        )

    screening = hec.screen_steady_flows_from_kmz(**kwargs)
    return WorkflowResult(
        project_name=model.project_name,
        run_type="single",
        output_folder=str(output_folder),
        sub_projects=[model.sub_project_name],
        success=True,
        message=screening.message,
        result=screening.to_dict(),
    )


def run_junction_model(
    hec: HECRAS,
    config: Config,
    main_model: ModelInput,
    tributary_model: ModelInput,
    additional_models: list[ModelInput] | None,
    hydrology_kmz: Path,
    projection_file: Path,
    dry_run: bool,
    project_stem: str | None = None,
) -> WorkflowResult:
    output_folder = Path(config.get_output_project_path(main_model.project_name))
    output_stem = project_stem or main_model.project_name
    reference_geometry = find_reference_geometry(config, main_model.project_name, output_stem)
    if reference_geometry is not None:
        output_stem = reference_geometry.stem or output_stem

    kwargs = {
        "project_folder": output_folder,
        "project_stem": output_stem,
        "project_title": output_stem,
        "main_cross_section_csv": Path(main_model.paths.cross_section_file_path),
        "main_bank_lines_shp": Path(main_model.paths.bank_line_file_path),
        "tributary_cross_section_csv": Path(tributary_model.paths.cross_section_file_path),
        "tributary_bank_lines_shp": Path(tributary_model.paths.bank_line_file_path),
        "main_structure_csv": main_model.structure_csv,
        "tributary_structure_csv": tributary_model.structure_csv,
        "combined_bank_lines_shp": find_combined_bank_lines_shp(config, main_model, tributary_model),
        "additional_reaches": [
            {
                "name": model.sub_project_name,
                "project_stem": model.project_stem,
                "project_title": model.project_title,
                "cross_section_csv": Path(model.paths.cross_section_file_path),
                "bank_lines_shp": Path(model.paths.bank_line_file_path),
                "structure_csv": model.structure_csv,
            }
            for model in (additional_models or [])
        ],
        "hydrology_kmz": hydrology_kmz,
        "buffer_distance": HYDROLOGY_BUFFER_METERS,
        "reference_geometry_file": reference_geometry,
        "projection_file": projection_file,
        "centerline_samples_per_segment": CENTERLINE_SAMPLES_PER_SEGMENT,
        "bank_station_mode": BANK_STATION_MODE,
        "river_line_method": RIVER_LINE_METHOD,
        "return_periods": RETURN_PERIODS,
        "all_flows_in_single_plan": ALL_FLOW_IN_SINGLE_PLAN,
        "prepare_geometry_hdf": PREPARE_GEOMETRY_HDF,
    }

    sub_projects = [
        main_model.sub_project_name,
        tributary_model.sub_project_name,
        *[model.sub_project_name for model in (additional_models or [])],
    ]
    logger.info("Junction model %s %s -> %s", main_model.project_name, sub_projects, output_folder)
    if dry_run:
        return WorkflowResult(
            project_name=main_model.project_name,
            run_type="junction",
            output_folder=str(output_folder),
            sub_projects=sub_projects,
            success=True,
            message="Dry run assembled junction-model arguments.",
            result=serializable_kwargs(kwargs),
        )

    screening = hec.screen_steady_junction_flows_from_kmz(**kwargs)
    return WorkflowResult(
        project_name=main_model.project_name,
        run_type="junction",
        output_folder=str(output_folder),
        sub_projects=sub_projects,
        success=True,
        message=screening.message,
        result=screening.to_dict(),
    )


def run_project(
    hec: HECRAS,
    config: Config,
    project_name: str,
    sub_project_names: list[str],
    hydrology_kmz: Path,
    projection_file: Path,
    dry_run: bool,
    continue_on_error: bool,
) -> list[WorkflowResult]:
    models = [
        build_model_input(config, project_name, sub_project_name, use_structures=USE_STRUCTURES)
        for sub_project_name in sub_project_names
    ]

    if len(models) == 1:
        return [
            guarded_run(
                lambda: run_single_model(
                    hec=hec,
                    config=config,
                    model=models[0],
                    hydrology_kmz=hydrology_kmz,
                    projection_file=projection_file,
                    dry_run=dry_run,
                ),
                project_name=project_name,
                sub_projects=[models[0].sub_project_name],
                run_type="single",
                continue_on_error=continue_on_error,
            )
        ]

    pairings = JUNCTION_PAIRS_BY_PROJECT.get(project_name)
    network_pairs = read_network_pairs(config)
    if pairings is None:
        pairings = network_pairings_for_project(config, models, network_pairs=network_pairs)
    if not pairings and network_pairs:
        logger.info(
            "No network.csv junction pair matched project %s; running sub-projects separately.",
            project_name,
        )
        return [
            guarded_run(
                lambda model=model: run_single_model(
                    hec=hec,
                    config=config,
                    model=model,
                    hydrology_kmz=hydrology_kmz,
                    projection_file=projection_file,
                    dry_run=dry_run,
                ),
                project_name=project_name,
                sub_projects=[model.sub_project_name],
                run_type="single",
                continue_on_error=continue_on_error,
            )
            for model in models
        ]
    if not pairings:
        main_model = models[0]
        pairings = [(main_model.sub_project_name, tributary.sub_project_name) for tributary in models[1:]]

    model_by_name = {_normalize_name(model.sub_project_name): model for model in models}
    paired_model_names = {
        _normalize_name(name)
        for pair in pairings
        for name in pair
    }
    unpaired_models = [
        model
        for model in models
        if _normalize_name(model.sub_project_name) not in paired_model_names
    ]
    results: list[WorkflowResult] = []
    for main_name, tributary_name in pairings:
        main_model = model_by_name[_normalize_name(main_name)]
        tributary_model = model_by_name[_normalize_name(tributary_name)]
        project_stem = project_name if len(pairings) == 1 else f"{project_name}_{tributary_model.sub_project_name}"
        additional_models = (
            []
            if RUN_UNCONNECTED_AS_SEPARATE_PROJECTS
            else unpaired_models if len(pairings) == 1 else []
        )
        results.append(
            guarded_run(
                lambda main=main_model, trib=tributary_model, extras=additional_models, stem=project_stem: run_junction_model(
                    hec=hec,
                    config=config,
                    main_model=main,
                    tributary_model=trib,
                    additional_models=extras,
                    hydrology_kmz=hydrology_kmz,
                    projection_file=projection_file,
                    dry_run=dry_run,
                    project_stem=stem,
                ),
                project_name=project_name,
                sub_projects=[main_model.sub_project_name, tributary_model.sub_project_name],
                run_type="junction",
                continue_on_error=continue_on_error,
            )
        )

    if RUN_UNCONNECTED_AS_SEPARATE_PROJECTS and unpaired_models:
        logger.info(
            "Running %s unconnected sub-project(s) for %s as separate HEC-RAS project(s): %s",
            len(unpaired_models),
            project_name,
            [model.sub_project_name for model in unpaired_models],
        )
    for model in (unpaired_models if RUN_UNCONNECTED_AS_SEPARATE_PROJECTS else []):
        results.append(
            guarded_run(
                lambda model=model: run_single_model(
                    hec=hec,
                    config=config,
                    model=model,
                    hydrology_kmz=hydrology_kmz,
                    projection_file=projection_file,
                    dry_run=dry_run,
                ),
                project_name=project_name,
                sub_projects=[model.sub_project_name],
                run_type="single",
                continue_on_error=continue_on_error,
            )
        )

    return results


def guarded_run(
    runner,
    project_name: str,
    sub_projects: list[str],
    run_type: str,
    continue_on_error: bool,
) -> WorkflowResult:
    try:
        return runner()
    except Exception as exc:
        logger.exception("Failed %s run for %s %s", run_type, project_name, sub_projects)
        if not continue_on_error:
            raise
        return WorkflowResult(
            project_name=project_name,
            run_type=run_type,
            output_folder="",
            sub_projects=sub_projects,
            success=False,
            message=str(exc),
            result=None,
        )


def serializable_kwargs(kwargs: dict) -> dict:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in kwargs.items()
    }


def write_summary(config: Config, results: list[WorkflowResult], dry_run: bool) -> Path:
    summary_name = "implementation1d_dry_run_summary.json" if dry_run else "implementation1d_summary.json"
    summary_path = Path(config.TEMP_PATH) / summary_name
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2, default=str),
        encoding="utf-8",
    )
    return summary_path


def version_sort_key(path: Path) -> tuple[int, float, str]:
    try:
        modified_time = path.stat().st_mtime
    except OSError:
        modified_time = 0.0
    matches = re.findall(r"(?:^|[_\-\s])V(\d+)(?=$|[^0-9])", path.name, flags=re.IGNORECASE)
    version = max((int(match) for match in matches), default=0)
    return version, modified_time, path.name.upper()


def _normalize_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value).upper()


def _names_match(left_normalized: str, right_normalized: str) -> bool:
    if not left_normalized or not right_normalized:
        return False
    return (
        left_normalized == right_normalized
        or left_normalized.endswith(right_normalized)
        or right_normalized.endswith(left_normalized)
    )


def build_hecras(ras_exe_path: Path) -> HECRAS:
    from hecras import HECRAS

    return HECRAS(ras_exe_path=ras_exe_path)


def build_config(args: argparse.Namespace) -> Config:
    source = args.source or CONFIG_SOURCE
    master_project_path = args.master_project_path or MASTER_PROJECT_PATH
    config = Config(
        structure_source=source,
        master_project_path=master_project_path,
        project_folder=master_project_path,
    )
    logger.info(
        "Loaded structure from %s using root %s",
        config.structure_source,
        config.PROJECT_FOLDER,
    )
    config.setup_essential_directories()
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run 1D HEC-RAS modeling using the master project folder structure."
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve inputs and print calls without running HEC-RAS.")
    parser.add_argument("--project", action="append", help="Project name to run. Repeat for multiple projects.")
    parser.add_argument("--continue-on-error", action="store_true", default=True, help="Continue running remaining projects after an error.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop immediately if one project fails.")
    parser.add_argument("--no-structures", action="store_true", help="Disable optional structure CSV discovery.")
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


def main() -> list[WorkflowResult]:
    args = parse_args()
    if args.no_structures:
        global USE_STRUCTURES
        USE_STRUCTURES = False

    config = build_config(args)
    project_filter = args.project if args.project else PROJECTS_TO_RUN
    project_subprojects = filtered_projects(
        discover_project_subprojects(config),
        project_filter,
    )

    if not project_subprojects:
        raise ValueError(
            f"No projects were selected from the active structure source ({config.structure_source}). "
            f"In folder mode, check that {Path(config.BUR_BUR_PATH)} contains project folders with sub-project folders."
        )

    hydrology_kmz = find_essential_file(config, "Burdur Points.kmz", "*.kmz")
    projection_file = find_essential_file(config, "TUREF_CM30_projection.prj", "*.prj")
    hec = None if args.dry_run else build_hecras(Path(config.RAS_EXE_PATH))

    results: list[WorkflowResult] = []
    for project_name, sub_project_names in project_subprojects.items():
        logger.info("Preparing project %s with sub-projects %s", project_name, sub_project_names)
        results.extend(
            run_project(
                hec=hec,
                config=config,
                project_name=project_name,
                sub_project_names=sub_project_names,
                hydrology_kmz=hydrology_kmz,
                projection_file=projection_file,
                dry_run=args.dry_run,
                continue_on_error=not args.stop_on_error,
            )
        )

    summary_path = write_summary(config, results, dry_run=args.dry_run)
    print(f"\nSummary written to: {summary_path}")
    for result in results:
        status = "OK" if result.success else "FAILED"
        sub_projects = ", ".join(result.sub_projects)
        print(f"[{status}] {result.project_name} ({result.run_type}: {sub_projects})")
        print(f"  Output: {result.output_folder}")
        print(f"  {result.message}")

    return results


if __name__ == "__main__":
    main()
