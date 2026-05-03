from __future__ import annotations

import csv
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import sys
from typing import Iterable


PWD = Path(__file__).resolve().parent
if str(PWD) not in sys.path:
    sys.path.insert(0, str(PWD))
AUTOMATION_PATH = PWD / "Automation"
if str(AUTOMATION_PATH) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_PATH))

import implementation1d as base
from configProject import Config


# Keep the same project/path dependencies as implementation1d.py. This script only
# changes where structure tables come from: 0 Essentials/structures.csv.
CONFIG_SOURCE = base.CONFIG_SOURCE
MASTER_PROJECT_PATH = base.MASTER_PROJECT_PATH
SHEET_NAME = base.SHEET_NAME
PROJECTS_TO_RUN = base.PROJECTS_TO_RUN
JUNCTION_PAIRS_BY_PROJECT = base.JUNCTION_PAIRS_BY_PROJECT

USE_STRUCTURES = True
STRUCTURES_CSV_NAME = "structures.csv"

logger = logging.getLogger("implementation1dStructures")

_STRUCTURE_ROWS_CACHE: dict[Path, list[dict[str, str]]] = {}


def find_essentials_structures_csv(config: Config) -> Path | None:
    essentials_path = Path(config.ESSENTIALS_PATH)
    preferred = essentials_path / STRUCTURES_CSV_NAME
    if preferred.exists():
        return preferred

    candidates = sorted(
        essentials_path.glob("*structure*.csv"),
        key=base.version_sort_key,
        reverse=True,
    )
    return candidates[0] if candidates else None


def read_structure_rows(structures_csv: Path) -> list[dict[str, str]]:
    structures_csv = structures_csv.resolve()
    if structures_csv in _STRUCTURE_ROWS_CACHE:
        return _STRUCTURE_ROWS_CACHE[structures_csv]

    first_line = structures_csv.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[0]
    delimiter = "\t" if "\t" in first_line else ","
    with structures_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows = [
            {str(key).strip(): ("" if value is None else str(value).strip()) for key, value in row.items()}
            for row in reader
        ]

    _STRUCTURE_ROWS_CACHE[structures_csv] = rows
    return rows


def structure_group_key(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?i)(?:[_\-\s]*rev)?[_\-\s]*v\d+$", "", text)
    return normalize_name(text)


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", str(value or "")).upper()


def names_match(left: str, right: str) -> bool:
    left_normalized = normalize_name(left)
    right_normalized = normalize_name(right)
    if not left_normalized or not right_normalized:
        return False
    return (
        left_normalized == right_normalized
        or left_normalized.endswith(right_normalized)
        or right_normalized.endswith(left_normalized)
    )


def row_matches_model(row: dict[str, str], project_name: str, sub_project_name: str) -> bool:
    row_project = row.get("Project") or row.get("project") or ""
    row_subproject = row.get("Subproject") or row.get("SubProject") or row.get("subproject") or ""
    if row_project and not names_match(row_project, project_name):
        return False
    if names_match(row_subproject, sub_project_name):
        return True
    return structure_group_key(row_subproject) == structure_group_key(sub_project_name)


def write_filtered_structure_table(
    config: Config,
    project_name: str,
    sub_project_name: str,
    rows: Iterable[dict[str, str]],
) -> Path | None:
    rows = list(rows)
    if not rows:
        return None

    fieldnames: list[str] = []
    for row in rows:
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)

    output_dir = Path(config.TEMP_PATH) / "implementation1dStructures"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_project = normalize_name(project_name) or "PROJECT"
    safe_subproject = normalize_name(sub_project_name) or "SUBPROJECT"
    output_path = output_dir / f"{safe_project}__{safe_subproject}_structures.csv"
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def find_structure_csv(config: Config, project_name: str, sub_project_name: str) -> Path | None:
    structures_csv = find_essentials_structures_csv(config)
    if structures_csv is None:
        logger.warning("No %s found in %s", STRUCTURES_CSV_NAME, config.ESSENTIALS_PATH)
        return None

    matched_rows = [
        row
        for row in read_structure_rows(structures_csv)
        if row_matches_model(row, project_name, sub_project_name)
    ]
    structure_path = write_filtered_structure_table(
        config=config,
        project_name=project_name,
        sub_project_name=sub_project_name,
        rows=matched_rows,
    )
    if structure_path is not None:
        logger.info(
            "Prepared %s structure row(s) for %s/%s from %s",
            len(matched_rows),
            project_name,
            sub_project_name,
            structures_csv,
        )
    return structure_path


def build_model_input(
    config: Config,
    project_name: str,
    sub_project_name: str,
    use_structures: bool,
) -> base.ModelInput:
    paths = config.get_sub_project_paths(project_name, sub_project_name)
    project_stem = base.project_stem_from_cross_section(Path(paths.cross_section_file_path))
    structure_csv = (
        find_structure_csv(config, paths.project_name, paths.sub_project_name)
        if use_structures
        else None
    )

    return base.ModelInput(
        project_name=paths.project_name,
        sub_project_name=paths.sub_project_name,
        project_stem=project_stem,
        project_title=project_stem,
        paths=paths,
        structure_csv=structure_csv,
    )


def write_summary(config: Config, results: list[base.WorkflowResult], dry_run: bool) -> Path:
    summary_name = (
        "implementation1dStructures_dry_run_summary.json"
        if dry_run
        else "implementation1dStructures_summary.json"
    )
    summary_path = Path(config.TEMP_PATH) / summary_name
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2, default=str),
        encoding="utf-8",
    )
    return summary_path


def configure_base_module() -> None:
    base.CONFIG_SOURCE = CONFIG_SOURCE
    base.MASTER_PROJECT_PATH = MASTER_PROJECT_PATH
    base.SHEET_NAME = SHEET_NAME
    base.PROJECTS_TO_RUN = PROJECTS_TO_RUN
    base.JUNCTION_PAIRS_BY_PROJECT = JUNCTION_PAIRS_BY_PROJECT
    base.USE_STRUCTURES = USE_STRUCTURES
    base.logger = logger
    base.build_model_input = build_model_input
    base.write_summary = write_summary


def main() -> list[base.WorkflowResult]:
    configure_base_module()
    return base.main()


if __name__ == "__main__":
    main()
