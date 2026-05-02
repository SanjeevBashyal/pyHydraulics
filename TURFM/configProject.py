from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from sheets import GoogleSheetsClient, sheet_id


# DIRECT_RUN_SOURCE = "folder"
# DIRECT_RUN_MASTER_PROJECT_PATH: str | None = None
# DIRECT_RUN_SHEET_NAME: str | None = None
# DIRECT_RUN_PREPARE_FULL_STRUCTURE = False

DIRECT_RUN_SOURCE = "folder"
DIRECT_RUN_MASTER_PROJECT_PATH = Path(r"C:\Users\Ripple\Downloads\Turkey Flood\Group-4-Model")
DIRECT_RUN_SHEET_NAME = None
DIRECT_RUN_PREPARE_FULL_STRUCTURE = False


@dataclass(frozen=True)
class FolderEntry:
    """Represents one folder row from the Structure worksheet."""

    name: str
    level: int
    relative_parts: tuple[str, ...]
    contents: str = ""

    @property
    def relative_path(self) -> Path:
        return Path(*self.relative_parts)


@dataclass(frozen=True)
class SubProjectPaths:
    """Resolved paths for one project/sub-project model run."""

    project_name: str
    sub_project_name: str
    project_path: str
    sub_project_path: str
    hecras_project_path: str
    hecras_sub_project_path: str
    gis_project_path: str
    gis_sub_project_path: str
    output_project_path: str
    output_sub_project_path: str
    temp_project_path: str
    temp_sub_project_path: str
    cross_section_path: str
    cross_section_file_path: str
    bank_line_path: str
    bank_line_file_path: str
    dtm_path: str


@dataclass
class Config:
    """
    Builds the project folder tree from either:

    1. the Google Sheet Structure tab, or
    2. a master project folder with the same directory layout.

    This class still exposes the legacy path attributes required by the
    implementation scripts and automation helpers.
    """

    credentials_file: str = "master_credentials.json"
    workbook_id: str = sheet_id
    worksheet_name: str = "Structure"
    sheet_name: str | None = None
    structure_source: str = "sheet"
    master_project_path: str | None = None
    project_folder: str | None = None
    create_master_folder_if_missing: bool = False
    PROJECT_NAME: str = ""
    SUB_PROJECT_NAME: str = ""
    PROJECT_SHORT_NAME: str = ""

    # Processing Config
    BLEND_TYPE: str = "linear"
    HECRAS_VERSION: str = "6.7"
    RAS_EXE_PATH: str = r"C:\Program Files (x86)\HEC\HEC-RAS\6.7 Beta 4\Ras.exe"
    DEM_FILENAME: str = "SET4_27_DTM_070226_R1.tif"
    DTM_INDEX_FILENAME: str = "dtm.csv"
    DTM_DIRNAME: str = "DTMs"
    CROSS_SECTION_DIRNAME: str = "KESIT_TESLIM"
    BANK_LINE_DIRNAME: str = "SEV_USTU"
    IGNORED_PROJECT_FOLDER_NAMES: tuple[str, ...] = ("Z Received",)

    MASTER_PATH: str = field(init=False)
    PROJECT_FOLDER: str = field(init=False)
    PROJECT_LONG_NAME: str = field(init=False)
    FOLDER_ENTRIES: list[FolderEntry] = field(init=False, default_factory=list)
    FOLDER_DESCRIPTIONS: dict[str, str] = field(init=False, default_factory=dict)
    PATHS: dict[str, str] = field(init=False, default_factory=dict)

    ESSENTIALS_PATH: str = field(init=False)
    BUR_BUR_PATH: str = field(init=False)
    HEC_PATH: str = field(init=False)
    GIS_PATH: str = field(init=False)
    OUTPUT_PATH: str = field(init=False)
    OUTPUT_ROOT_PATH: str = field(init=False)
    TEMP_PATH: str = field(init=False)

    PROJECT_DATA_PATH: str = field(init=False)
    HEC_PROJECT_PATH: str = field(init=False)
    HEC_SUB_PROJECT_PATH: str = field(init=False)
    GIS_PROJECT_PATH: str = field(init=False)
    GIS_SUB_PROJECT_PATH: str = field(init=False)
    OUTPUT_PROJECT_PATH: str = field(init=False)
    OUTPUT_SUB_PROJECT_PATH: str = field(init=False)
    TEMP_PROJECT_PATH: str = field(init=False)
    TEMP_SUB_PROJECT_PATH: str = field(init=False)
    PROJECT_GROUP: str = field(init=False, default="")

    DEM_PATH: str = field(init=False)
    DTM_INDEX_PATH: str = field(init=False)
    DTM_FOLDER_PATH: str = field(init=False)
    CROSS_SECTION_PATH: str = field(init=False)
    CROSS_SECTION_FILE_PATH: str = field(init=False)
    BANK_LINE_PATH: str = field(init=False)
    BANK_LINE_FILE_PATH: str = field(init=False)

    @classmethod
    def from_sheet(
        cls,
        *,
        sheet_name: str | None = None,
        project_folder: str | None = None,
        **kwargs,
    ) -> "Config":
        return cls(
            structure_source="sheet",
            sheet_name=sheet_name,
            project_folder=project_folder,
            **kwargs,
        )

    @classmethod
    def from_master_folder(
        cls,
        master_project_path: str,
        *,
        sheet_name: str | None = None,
        create_if_missing: bool = False,
        **kwargs,
    ) -> "Config":
        return cls(
            structure_source="folder",
            master_project_path=master_project_path,
            project_folder=master_project_path,
            sheet_name=sheet_name,
            create_master_folder_if_missing=create_if_missing,
            **kwargs,
        )

    def __post_init__(self):
        self.worksheet_name = self._clean_cell(self.sheet_name or self.worksheet_name or "Structure") or "Structure"
        self.structure_source = self._normalize_structure_source(self.structure_source)
        explicit_master_path = self._clean_cell(self.master_project_path or self.project_folder or "")

        if self.structure_source in {"sheet", "auto"}:
            try:
                rows = self._read_structure_sheet()
                master_path, sheet_project_path, structure_rows = self._parse_sheet(rows)
                self.MASTER_PATH = self._clean_cell(master_path)
                self.PROJECT_FOLDER = self._clean_cell(explicit_master_path or sheet_project_path)

                if not self.PROJECT_FOLDER:
                    raise ValueError("Project folder path could not be resolved from the sheet.")

                self._build_folder_entries(structure_rows)
                self._ensure_top_level_entries()
                self._rebuild_paths()
                self._refresh_compatibility_paths()
                return
            except Exception:
                if self.structure_source == "sheet":
                    raise
                if not explicit_master_path:
                    raise

        if self.structure_source in {"folder", "auto"}:
            if not explicit_master_path:
                raise ValueError(
                    "master_project_path or project_folder must be provided when reading structure from a folder."
                )
            self._initialize_from_master_folder(explicit_master_path)
            return

        raise ValueError(f"Unsupported structure_source: {self.structure_source!r}")

    def _read_structure_sheet(self) -> list[list[str]]:
        client = GoogleSheetsClient(credentials_file=self.credentials_file)
        return client.get_all_values(self.workbook_id, self.worksheet_name)

    def _parse_sheet(self, rows: list[list[str]]) -> tuple[str, str, list[list[str]]]:
        master_path = ""
        project_path = ""
        structure_rows: list[list[str]] = []
        in_structure_section = False

        for row in rows:
            label = self._cell(row, 1)

            if label == "Master Path":
                master_path = self._cell(row, 2)
                continue

            if label in {"Project Path", "Master Project Path"}:
                project_path = self._cell(row, 2)
                continue

            if label in {"Project Folder Structure", "Master Project Folder Structure"}:
                in_structure_section = True
                continue

            if in_structure_section:
                structure_rows.append(row)

        if not master_path:
            raise ValueError("Master Path was not found in the Structure sheet.")

        if not project_path:
            raise ValueError("Project Path was not found in the Structure sheet.")

        return master_path, project_path, structure_rows

    def _build_folder_entries(self, structure_rows: list[list[str]]):
        self.FOLDER_ENTRIES = []
        folder_stack: list[str] = []

        for row in structure_rows:
            folder_name, level, contents = self._extract_folder_row(row)
            if folder_name is None or level is None:
                continue

            folder_stack = folder_stack[:level]
            folder_stack.append(folder_name)

            self.FOLDER_ENTRIES.append(
                FolderEntry(
                    name=folder_name,
                    level=level,
                    relative_parts=tuple(folder_stack),
                    contents=contents,
                )
            )

    def _initialize_from_master_folder(self, folder_path: str):
        resolved_root = Path(self._clean_cell(folder_path)).expanduser()
        if not resolved_root.exists():
            if not self.create_master_folder_if_missing:
                raise FileNotFoundError(f"Master project folder does not exist: {resolved_root}")
            resolved_root.mkdir(parents=True, exist_ok=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"Master project path is not a directory: {resolved_root}")

        self.MASTER_PATH = str(resolved_root)
        self.PROJECT_FOLDER = str(resolved_root)
        self._build_folder_entries_from_master_folder(resolved_root)
        self._ensure_top_level_entries()
        self._rebuild_paths()
        self._refresh_compatibility_paths()

    def _build_folder_entries_from_master_folder(self, root: Path):
        self.FOLDER_ENTRIES = []
        added: set[tuple[str, ...]] = set()

        def add_entry(relative_parts: tuple[str, ...]):
            if not relative_parts or relative_parts in added:
                return
            added.add(relative_parts)
            self.FOLDER_ENTRIES.append(
                FolderEntry(
                    name=relative_parts[-1],
                    level=len(relative_parts) - 1,
                    relative_parts=relative_parts,
                    contents="",
                )
            )

        default_top_levels = list(self._default_top_level_names())
        existing_top_levels = sorted(
            [path.name for path in root.iterdir() if path.is_dir()],
            key=self._top_level_sort_key,
        )
        for top_level_name in default_top_levels:
            add_entry((top_level_name,))
        for top_level_name in existing_top_levels:
            add_entry((top_level_name,))

        bur_bur_path = root / "1 Bur-Bur"
        if bur_bur_path.is_dir():
            # Folder mode contract:
            # 1 Bur-Bur/<Project> is a project, and each immediate child
            # 1 Bur-Bur/<Project>/<Sub-Project> is a sub-project.
            for project_dir in sorted(
                [
                    path
                    for path in bur_bur_path.iterdir()
                    if self._looks_like_project_directory(path)
                ],
                key=lambda item: item.name.upper(),
            ):
                sub_project_dirs = [
                    path
                    for path in project_dir.iterdir()
                    if path.is_dir()
                ]
                if not sub_project_dirs:
                    continue
                add_entry(("1 Bur-Bur", project_dir.name))
                for sub_project_dir in sorted(
                    sub_project_dirs,
                    key=lambda item: (self._version_number(item.name), item.name.upper()),
                    reverse=True,
                ):
                    add_entry(("1 Bur-Bur", project_dir.name, sub_project_dir.name))

        hec_path = root / "2 Hecras"
        if hec_path.is_dir():
            for project_dir in sorted(
                [path for path in hec_path.iterdir() if path.is_dir()],
                key=lambda item: item.name.upper(),
            ):
                add_entry(("2 Hecras", project_dir.name))

    def _ensure_top_level_entries(self):
        if not self.FOLDER_ENTRIES:
            return

        added = {entry.relative_parts for entry in self.FOLDER_ENTRIES}
        for top_level_name in self._default_top_level_names():
            relative_parts = (top_level_name,)
            if relative_parts in added:
                continue
            self.FOLDER_ENTRIES.append(
                FolderEntry(
                    name=top_level_name,
                    level=0,
                    relative_parts=relative_parts,
                    contents="",
                )
            )

    def _extract_folder_row(self, row: list[str]) -> tuple[str | None, int | None, str]:
        contents = self._cell(row, len(row) - 1) if row else ""

        for column_index in range(2, max(len(row) - 1, 2)):
            folder_name = self._cell(row, column_index)
            if folder_name:
                level = column_index - 2
                return folder_name, level, contents

        return None, None, contents

    def _rebuild_paths(self):
        self.PATHS = {"PROJECT_FOLDER": self.PROJECT_FOLDER}
        self.FOLDER_DESCRIPTIONS = {}

        for entry in self.FOLDER_ENTRIES:
            relative_key = entry.relative_path.as_posix()
            absolute_path = str(Path(self.PROJECT_FOLDER) / entry.relative_path)

            self.PATHS[relative_key] = absolute_path
            self.FOLDER_DESCRIPTIONS[relative_key] = entry.contents
            setattr(self, self._to_attr_name(entry.relative_parts), absolute_path)

    def _refresh_compatibility_paths(self):
        self._select_active_names_from_structure_if_missing()
        self.PROJECT_LONG_NAME = self._project_long_name_from_short_name(self.PROJECT_SHORT_NAME)

        self.ESSENTIALS_PATH = self._sheet_path_or_default("0 Essentials")
        self.BUR_BUR_PATH = self._sheet_path_or_default("1 Bur-Bur")
        self.HEC_PATH = self._sheet_path_or_default("2 Hecras")
        self.GIS_PATH = self._sheet_path_or_default("3 GIS")
        self.OUTPUT_ROOT_PATH = self._sheet_path_or_default("4 Outputs")
        self.OUTPUT_PATH = self.OUTPUT_ROOT_PATH
        self.TEMP_PATH = self._sheet_path_or_default("5 Temp")
        self.DTM_INDEX_PATH = str(Path(self.ESSENTIALS_PATH) / self.DTM_INDEX_FILENAME)
        self.DTM_FOLDER_PATH = str(Path(self.ESSENTIALS_PATH) / self.DTM_DIRNAME)

        project_relative_path = self._resolve_project_relative_path()
        self.PROJECT_GROUP = self._project_group_from_relative_path(project_relative_path)
        self.PROJECT_DATA_PATH = self._absolute_from_relative_path(project_relative_path)
        self.HEC_PROJECT_PATH = self._resolve_hec_project_path()
        self.HEC_SUB_PROJECT_PATH = str(Path(self.HEC_PROJECT_PATH) / self.SUB_PROJECT_NAME)
        active_project_name = self.PROJECT_GROUP or self.PROJECT_NAME
        self.GIS_PROJECT_PATH = str(Path(self.GIS_PATH) / active_project_name)
        self.GIS_SUB_PROJECT_PATH = str(Path(self.GIS_PROJECT_PATH) / self.SUB_PROJECT_NAME)
        self.OUTPUT_PROJECT_PATH = str(Path(self.OUTPUT_ROOT_PATH) / active_project_name)
        self.OUTPUT_SUB_PROJECT_PATH = str(Path(self.OUTPUT_PROJECT_PATH) / self.SUB_PROJECT_NAME)
        self.TEMP_PROJECT_PATH = str(Path(self.TEMP_PATH) / active_project_name)
        self.TEMP_SUB_PROJECT_PATH = str(Path(self.TEMP_PROJECT_PATH) / self.SUB_PROJECT_NAME)

        self.DEM_PATH = self.resolve_dtm_path(
            project_name=active_project_name,
            sub_project_name=self.SUB_PROJECT_NAME,
            required=False,
        )
        self.CROSS_SECTION_PATH = str(
            Path(self.PROJECT_DATA_PATH) / self.CROSS_SECTION_DIRNAME
        )
        self.CROSS_SECTION_FILE_PATH = str(
            Path(self.CROSS_SECTION_PATH)
            / f"{self.PROJECT_LONG_NAME}_{self.CROSS_SECTION_DIRNAME}.csv"
        )
        self.BANK_LINE_PATH = str(Path(self.PROJECT_DATA_PATH) / self.BANK_LINE_DIRNAME)
        self.BANK_LINE_FILE_PATH = str(
            Path(self.BANK_LINE_PATH) / f"{self.PROJECT_LONG_NAME}_{self.BANK_LINE_DIRNAME}.shp"
        )

    def _select_active_names_from_structure_if_missing(self):
        """
        Keeps legacy active-project attributes usable without hardcoded project
        names. Project execution still uses discover_project_subprojects().
        """
        project_name = self._clean_cell(self.PROJECT_NAME)
        sub_project_name = self._clean_cell(self.SUB_PROJECT_NAME)
        project_short_name = self._clean_cell(self.PROJECT_SHORT_NAME)

        project_subprojects: dict[str, list[str]] = {}
        for entry in self.FOLDER_ENTRIES:
            parts = entry.relative_parts
            if len(parts) == 2 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], [])
            elif len(parts) == 3 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], []).append(parts[2])

        if not project_name and project_subprojects:
            project_name = next(iter(project_subprojects))

        if not sub_project_name and project_name:
            sub_projects = project_subprojects.get(project_name, [])
            if sub_projects:
                sub_project_name = sub_projects[0]

        if not project_short_name:
            project_short_name = self._project_short_name_from_long_name(sub_project_name or project_name)

        self.PROJECT_NAME = project_name
        self.SUB_PROJECT_NAME = sub_project_name
        self.PROJECT_SHORT_NAME = project_short_name

    def set_project_folder(
        self,
        folder_path: str,
        short_name: str | None = None,
        project_name: str | None = None,
        sub_project_name: str | None = None,
    ):
        """Updates the project folder dynamically and optionally updates the short name."""
        self.project_folder = folder_path
        self.PROJECT_FOLDER = self._clean_cell(folder_path)
        if short_name is not None:
            self.PROJECT_SHORT_NAME = self._clean_cell(short_name)
        if project_name is not None:
            self.PROJECT_NAME = self._clean_cell(project_name)
        if sub_project_name is not None:
            self.SUB_PROJECT_NAME = self._clean_cell(sub_project_name)
        self._rebuild_paths()
        self._refresh_compatibility_paths()

    def get_sub_project_paths(
        self,
        project_name: str,
        sub_project_name: str,
        cross_section_stem: str | None = None,
        bank_line_stem: str | None = None,
        resolve_dtm: bool = False,
    ) -> SubProjectPaths:
        """
        Resolves all important paths for one project/sub-project.

        A sub-project is the direct folder at
        ``1 Bur-Bur/<Project>/<Sub-Project>``. Data files are resolved by
        recursively searching inside that sub-project folder for
        ``*KESIT_TESLIM*.csv`` and ``*SEV_USTU*.shp``.
        """
        project_path = self.get_project_path(project_name)
        sub_project_path = self.get_sub_project_path(project_name, sub_project_name)
        hecras_project_path = self.get_hecras_project_path(project_name)
        hecras_sub_project_path = str(Path(hecras_project_path) / Path(sub_project_path).name)
        cross_section_file_path = self.find_cross_section_file(
            project_name,
            sub_project_name,
            file_stem=cross_section_stem,
        )
        cross_section_path = str(Path(cross_section_file_path).parent)

        bank_line_file_path = self.find_bank_line_file(
            project_name,
            sub_project_name,
            file_stem=bank_line_stem,
        )
        bank_line_path = str(Path(bank_line_file_path).parent)
        dtm_path = self.resolve_dtm_path(project_name, sub_project_name) if resolve_dtm else ""
        gis_project_path = self.get_gis_project_path(project_name)
        output_project_path = self.get_output_project_path(project_name)
        temp_project_path = self.get_temp_project_path(project_name)

        return SubProjectPaths(
            project_name=Path(project_path).name,
            sub_project_name=Path(sub_project_path).name,
            project_path=project_path,
            sub_project_path=sub_project_path,
            hecras_project_path=hecras_project_path,
            hecras_sub_project_path=hecras_sub_project_path,
            gis_project_path=gis_project_path,
            gis_sub_project_path=str(Path(gis_project_path) / Path(sub_project_path).name),
            output_project_path=output_project_path,
            output_sub_project_path=str(Path(output_project_path) / Path(sub_project_path).name),
            temp_project_path=temp_project_path,
            temp_sub_project_path=str(Path(temp_project_path) / Path(sub_project_path).name),
            cross_section_path=cross_section_path,
            cross_section_file_path=cross_section_file_path,
            bank_line_path=bank_line_path,
            bank_line_file_path=bank_line_file_path,
            dtm_path=dtm_path,
        )

    def set_active_sub_project(
        self,
        project_name: str,
        sub_project_name: str,
        cross_section_stem: str | None = None,
        bank_line_stem: str | None = None,
        output_to_hecras: bool = True,
    ) -> SubProjectPaths:
        """
        Makes one sub-project active for legacy callDTM.py consumers.

        This updates CROSS_SECTION_FILE_PATH, BANK_LINE_FILE_PATH, OUTPUT_PATH,
        and the related legacy attributes so existing code can keep reading
        config.<ATTRIBUTE> while callers switch project/sub-project context.
        """
        paths = self.get_sub_project_paths(
            project_name=project_name,
            sub_project_name=sub_project_name,
            cross_section_stem=cross_section_stem,
            bank_line_stem=bank_line_stem,
            resolve_dtm=False,
        )

        self.PROJECT_NAME = paths.project_name
        self.SUB_PROJECT_NAME = paths.sub_project_name
        self.PROJECT_GROUP = paths.project_name
        self.PROJECT_DATA_PATH = paths.sub_project_path
        self.HEC_PROJECT_PATH = paths.hecras_project_path
        self.HEC_SUB_PROJECT_PATH = paths.hecras_sub_project_path
        self.GIS_PROJECT_PATH = paths.gis_project_path
        self.GIS_SUB_PROJECT_PATH = paths.gis_sub_project_path
        self.OUTPUT_PROJECT_PATH = paths.output_project_path
        self.OUTPUT_SUB_PROJECT_PATH = paths.output_sub_project_path
        self.TEMP_PROJECT_PATH = paths.temp_project_path
        self.TEMP_SUB_PROJECT_PATH = paths.temp_sub_project_path

        self.CROSS_SECTION_PATH = paths.cross_section_path
        self.CROSS_SECTION_FILE_PATH = paths.cross_section_file_path
        self.BANK_LINE_PATH = paths.bank_line_path
        self.BANK_LINE_FILE_PATH = paths.bank_line_file_path
        if paths.dtm_path:
            self.DEM_PATH = paths.dtm_path

        self.PROJECT_LONG_NAME = self._project_long_name_from_file(paths.cross_section_file_path)
        self.PROJECT_SHORT_NAME = self._project_short_name_from_long_name(self.PROJECT_LONG_NAME)
        self.OUTPUT_PATH = paths.output_sub_project_path if output_to_hecras else paths.gis_sub_project_path

        return paths

    def get_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.BUR_BUR_PATH), project_name))

    def get_sub_project_path(self, project_name: str, sub_project_name: str) -> str:
        project_path = Path(self.get_project_path(project_name))
        return str(self._resolve_child_directory(project_path, sub_project_name))

    def get_hecras_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.HEC_PATH), project_name, create_fallback=True))

    def get_gis_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.GIS_PATH), project_name, create_fallback=True))

    def get_gis_sub_project_path(self, project_name: str, sub_project_name: str) -> str:
        sub_project_path = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(Path(self.get_gis_project_path(project_name)) / sub_project_path.name)

    def get_output_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.OUTPUT_ROOT_PATH), project_name, create_fallback=True))

    def get_output_sub_project_path(self, project_name: str, sub_project_name: str) -> str:
        sub_project_path = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(Path(self.get_output_project_path(project_name)) / sub_project_path.name)

    def get_temp_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.TEMP_PATH), project_name, create_fallback=True))

    def get_temp_sub_project_path(self, project_name: str, sub_project_name: str) -> str:
        sub_project_path = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(Path(self.get_temp_project_path(project_name)) / sub_project_path.name)

    def get_cross_section_folder_path(self, project_name: str, sub_project_name: str) -> str:
        return str(Path(self.find_cross_section_file(project_name, sub_project_name)).parent)

    def get_bank_line_folder_path(self, project_name: str, sub_project_name: str) -> str:
        return str(Path(self.find_bank_line_file(project_name, sub_project_name)).parent)

    def find_cross_section_file(
        self,
        project_name: str,
        sub_project_name: str,
        file_stem: str | None = None,
    ) -> str:
        folder = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(
            self._resolve_versioned_file(
                folder=folder,
                file_stem=file_stem,
                default_contains=self.CROSS_SECTION_DIRNAME,
                extension=".csv",
            )
        )

    def find_bank_line_file(
        self,
        project_name: str,
        sub_project_name: str,
        file_stem: str | None = None,
    ) -> str:
        folder = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(
            self._resolve_versioned_file(
                folder=folder,
                file_stem=file_stem,
                default_contains=self.BANK_LINE_DIRNAME,
                extension=".shp",
            )
        )

    def resolve_dtm_path(
        self,
        project_name: str | None = None,
        sub_project_name: str | None = None,
        dtm_name: str | None = None,
        required: bool = True,
    ) -> str:
        """
        Resolves the DTM raster for a project/sub-project.

        If ``0 Essentials/dtm.csv`` exists, it is treated as the DTM index and
        the selected DTM token is matched as ``<token>*.tif`` inside
        ``0 Essentials/DTMs``. The root ``0 Essentials`` folder is also searched
        as a backward-compatible fallback.
        """
        selected_dtm_name = self._clean_cell(dtm_name or "")
        if not selected_dtm_name:
            selected_dtm_name = self._lookup_dtm_name(project_name, sub_project_name) or ""

        if selected_dtm_name:
            try:
                return str(self._resolve_dtm_file(selected_dtm_name))
            except FileNotFoundError:
                if required:
                    raise

        if required and Path(self.DTM_INDEX_PATH).exists() and not selected_dtm_name:
            raise FileNotFoundError(
                f"No matching DTM row was found in {Path(self.DTM_INDEX_PATH)} for "
                f"project {project_name!r}, sub-project {sub_project_name!r}."
            )

        fallback = self._resolve_default_dtm_path(required=required)
        return str(fallback)

    def _lookup_dtm_name(
        self,
        project_name: str | None,
        sub_project_name: str | None,
    ) -> str | None:
        dtm_index_path = Path(self.DTM_INDEX_PATH)
        if not dtm_index_path.exists():
            return None

        fieldnames, rows = self._read_delimited_dict_rows(dtm_index_path)
        if not rows or not fieldnames:
            return None

        dtm_column = self._find_dtm_index_column(
            fieldnames,
            preferred=("DTM", "DTMName", "DTMPrefix", "DEM", "DEMName", "Raster", "RasterName", "TIF", "TIFF"),
            contains=("DTM", "DEM", "RASTER", "TIF", "TIFF"),
        )
        if dtm_column is None:
            raise ValueError(
                f"Could not find a DTM name column in {dtm_index_path}. "
                "Use a column such as 'DTM' or 'DTM Name'."
            )

        project_column = self._find_dtm_index_column(
            fieldnames,
            preferred=("Project", "ProjectName", "ProjectShortName", "MainProject"),
            contains=("PROJECT",),
            reject_contains=("SUB",),
        )
        sub_project_column = self._find_dtm_index_column(
            fieldnames,
            preferred=("SubProject", "SubProjectName", "SubProjectFolder", "SubProjectPath", "Channel", "River", "RiverName"),
            contains=("SUBPROJECT", "CHANNEL", "RIVER"),
        )

        project_name = self._clean_cell(project_name or "")
        sub_project_name = self._clean_cell(sub_project_name or "")
        best_score = -1
        best_dtm_name = None

        for row in rows:
            score = 0
            has_filter = False

            if project_column:
                project_value = self._clean_cell(row.get(project_column, ""))
                if project_value:
                    has_filter = True
                    if not self._names_match(project_name, project_value):
                        continue
                    score += 2

            if sub_project_column:
                sub_project_value = self._clean_cell(row.get(sub_project_column, ""))
                if sub_project_value:
                    has_filter = True
                    if not self._names_match(sub_project_name, sub_project_value):
                        continue
                    score += 4

            if not has_filter:
                first_value = self._clean_cell(row.get(fieldnames[0], ""))
                if not (
                    self._names_match(project_name, first_value)
                    or self._names_match(sub_project_name, first_value)
                ):
                    continue
                score += 1

            dtm_value = self._clean_cell(row.get(dtm_column, ""))
            if not dtm_value:
                continue

            if score > best_score:
                best_score = score
                best_dtm_name = dtm_value

        return best_dtm_name

    @classmethod
    def _read_delimited_dict_rows(cls, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        text = path.read_text(encoding="utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return [], []

        sample = "\n".join(lines[:20])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            first_line = lines[0]
            delimiter = "\t" if "\t" in first_line else ";" if ";" in first_line else "|" if "|" in first_line else ","
            dialect = csv.excel()
            dialect.delimiter = delimiter

        reader = csv.DictReader(lines, dialect=dialect)
        if not reader.fieldnames:
            return [], []

        clean_fieldnames = [cls._clean_cell(field or "") for field in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row: dict[str, str] = {}
            for raw_field, clean_field in zip(reader.fieldnames, clean_fieldnames):
                if not clean_field:
                    continue
                row[clean_field] = cls._clean_cell(raw_row.get(raw_field, ""))
            if any(value for value in row.values()):
                rows.append(row)

        return [field for field in clean_fieldnames if field], rows

    @classmethod
    def _find_dtm_index_column(
        cls,
        fieldnames: list[str],
        preferred: tuple[str, ...],
        contains: tuple[str, ...] = (),
        reject_contains: tuple[str, ...] = (),
    ) -> str | None:
        normalized_lookup = {cls._normalize_name(field): field for field in fieldnames}
        for candidate in preferred:
            match = normalized_lookup.get(cls._normalize_name(candidate))
            if match is not None:
                return match

        for field in fieldnames:
            normalized = cls._normalize_name(field)
            if reject_contains and any(token in normalized for token in reject_contains):
                continue
            if contains and any(token in normalized for token in contains):
                return field
        return None

    def _resolve_dtm_file(self, dtm_name: str) -> Path:
        clean_name = self._clean_cell(dtm_name)
        candidate_path = Path(clean_name).expanduser()

        if candidate_path.is_absolute() and candidate_path.exists():
            return candidate_path

        search_roots = [
            Path(self.DTM_FOLDER_PATH),
            Path(self.ESSENTIALS_PATH),
        ]
        if not candidate_path.is_absolute() and candidate_path.parent != Path("."):
            search_roots.insert(0, Path(self.ESSENTIALS_PATH) / candidate_path.parent)
            clean_name = candidate_path.name

        patterns = []
        if any(char in clean_name for char in "*?"):
            patterns.append(clean_name)
        elif Path(clean_name).suffix.lower() in {".tif", ".tiff"}:
            patterns.extend([clean_name, f"{Path(clean_name).stem}*.tif", f"{Path(clean_name).stem}*.tiff"])
        else:
            patterns.extend([f"{clean_name}*.tif", f"{clean_name}*.tiff"])

        candidates: list[Path] = []
        for root in search_roots:
            if not root.exists():
                continue
            for pattern in patterns:
                candidates.extend(
                    path
                    for path in root.glob(pattern)
                    if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
                )

        if candidates:
            return self._select_best_path(list(dict.fromkeys(candidates)))

        available = self._available_dtm_raster_names()
        available_message = f" Available rasters: {', '.join(available)}." if available else ""
        raise FileNotFoundError(
            f"Could not find DTM raster matching {dtm_name!r}. "
            f"Searched {Path(self.DTM_FOLDER_PATH)} and {Path(self.ESSENTIALS_PATH)} using '<DTM name>*.tif'."
            f"{available_message}"
        )

    def _available_dtm_raster_names(self) -> list[str]:
        names = []
        for root in (Path(self.DTM_FOLDER_PATH), Path(self.ESSENTIALS_PATH)):
            if not root.exists():
                continue
            for path in root.glob("*"):
                if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}:
                    names.append(path.name)
        return sorted(set(names))

    def _resolve_default_dtm_path(self, required: bool = True) -> Path:
        candidates = [
            Path(self.ESSENTIALS_PATH) / self.DEM_FILENAME,
            Path(self.DTM_FOLDER_PATH) / self.DEM_FILENAME,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        if required:
            raise FileNotFoundError(
                f"No DTM could be resolved. Add {Path(self.DTM_INDEX_PATH)} "
                f"or place {self.DEM_FILENAME!r} in {Path(self.DTM_FOLDER_PATH)}."
            )
        return candidates[0]

    def get_path(self, *parts: str) -> str:
        """Returns an absolute path for a relative folder path from the sheet."""
        relative_key = Path(*parts).as_posix()
        return self.PATHS[relative_key]

    def discover_project_subprojects(self) -> dict[str, list[str]]:
        """Returns the project/sub-project mapping discovered from the active structure source."""
        project_subprojects: dict[str, list[str]] = {}

        for entry in self.FOLDER_ENTRIES:
            parts = entry.relative_parts
            if len(parts) == 2 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], [])
            elif len(parts) == 3 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], []).append(parts[2])

        return {
            project: sub_projects
            for project, sub_projects in project_subprojects.items()
            if sub_projects
        }

    def get_essential_directories(self) -> list[Path]:
        """Returns the master folder plus the top-level essential project folders."""
        directories = {Path(self.PROJECT_FOLDER)}
        top_level_names = set(self._default_top_level_names())

        for entry in self.FOLDER_ENTRIES:
            if len(entry.relative_parts) != 1:
                continue
            if entry.relative_parts[0] not in top_level_names:
                continue
            directories.add(Path(self.PATHS[entry.relative_path.as_posix()]))

        return sorted(directories, key=lambda item: (len(item.parts), str(item)))

    def setup_essential_directories(self):
        """Creates only the master folder and top-level essential folders."""
        for directory in self.get_essential_directories():
            directory.mkdir(parents=True, exist_ok=True)
            print(f"Ensured essential directory exists: {directory}")

    def setup_directories(self):
        """Creates the folder structure defined in the Structure sheet."""
        directories = {
            *self.get_essential_directories(),
            *[
                Path(self.PATHS[entry.relative_path.as_posix()])
                for entry in self.FOLDER_ENTRIES
                if len(entry.relative_parts) > 1
            ],
            Path(self.PROJECT_DATA_PATH),
            Path(self.HEC_PROJECT_PATH),
            Path(self.HEC_SUB_PROJECT_PATH),
            Path(self.GIS_PROJECT_PATH),
            Path(self.GIS_SUB_PROJECT_PATH),
            Path(self.OUTPUT_PROJECT_PATH),
            Path(self.OUTPUT_SUB_PROJECT_PATH),
            Path(self.TEMP_PROJECT_PATH),
            Path(self.TEMP_SUB_PROJECT_PATH),
            Path(self.OUTPUT_PATH),
            Path(self.CROSS_SECTION_PATH),
            Path(self.BANK_LINE_PATH),
        }

        if self.HEC_PROJECT_PATH:
            directories.add(Path(self.HEC_PROJECT_PATH))

        for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
            directory.mkdir(parents=True, exist_ok=True)
            print(f"Ensured directory exists: {directory}")

    @staticmethod
    def _clean_cell(value: str) -> str:
        return str(value).strip().strip('"')

    @classmethod
    def _cell(cls, row: list[str], index: int) -> str:
        if 0 <= index < len(row):
            return cls._clean_cell(row[index])
        return ""

    @classmethod
    def _normalize_structure_source(cls, value: str | None) -> str:
        normalized = cls._clean_cell(value or "sheet").lower()
        aliases = {
            "sheet": "sheet",
            "sheets": "sheet",
            "google_sheet": "sheet",
            "google_sheets": "sheet",
            "folder": "folder",
            "filesystem": "folder",
            "path": "folder",
            "auto": "auto",
        }
        if normalized not in aliases:
            raise ValueError(
                f"Unsupported structure_source {value!r}. Use 'sheet', 'folder', or 'auto'."
            )
        return aliases[normalized]

    @staticmethod
    def _default_top_level_names() -> tuple[str, ...]:
        return (
            "0 Essentials",
            "1 Bur-Bur",
            "2 Hecras",
            "3 GIS",
            "4 Outputs",
            "5 Temp",
        )

    @classmethod
    def _top_level_sort_key(cls, name: str) -> tuple[int, str]:
        clean_name = cls._clean_cell(name)
        try:
            index = cls._default_top_level_names().index(clean_name)
        except ValueError:
            index = len(cls._default_top_level_names())
        return index, clean_name.upper()

    def _looks_like_project_directory(self, path: Path) -> bool:
        if not path.is_dir():
            return False

        ignored_names = {
            self._normalize_name(name)
            for name in self.IGNORED_PROJECT_FOLDER_NAMES
        }
        return self._normalize_name(path.name) not in ignored_names

    def _looks_like_sub_project_directory(self, path: Path) -> bool:
        if not path.is_dir():
            return False

        if self._normalize_name(path.name).startswith(self._normalize_name("BUR-BUR-MER-")):
            return False

        return (
            self._contains_recursive_file(path, self.CROSS_SECTION_DIRNAME, ".csv")
            or self._contains_recursive_file(path, self.BANK_LINE_DIRNAME, ".shp")
        )

    def _contains_recursive_file(self, root: Path, token: str, extension: str) -> bool:
        clean_extension = extension if extension.startswith(".") else f".{extension}"
        normalized_token = self._normalize_name(token)

        for path in root.rglob("*"):
            if path.is_file() and normalized_token in self._normalize_name(path.stem):
                if path.suffix.lower() != clean_extension.lower():
                    continue
                return True
        return False

    @staticmethod
    def _to_attr_name(parts: tuple[str, ...]) -> str:
        tokens: list[str] = []

        for part in parts:
            cleaned = re.sub(r"^\d+\s*", "", part.strip())
            cleaned = re.sub(r"[^0-9A-Za-z]+", "_", cleaned).strip("_").upper()
            if cleaned:
                tokens.append(cleaned)

        return "_".join(tokens) + "_PATH"

    def _sheet_path_or_default(self, relative_key: str) -> str:
        return self.PATHS.get(relative_key, self._absolute_from_relative_path(relative_key))

    def _absolute_from_relative_path(self, relative_key: str) -> str:
        return str(Path(self.PROJECT_FOLDER) / Path(relative_key))

    def _resolve_project_relative_path(self) -> str:
        project_candidates = [
            entry.relative_path.as_posix()
            for entry in self.FOLDER_ENTRIES
            if entry.relative_parts and entry.relative_parts[0] == "1 Bur-Bur"
        ]

        for target_name in (
            self.PROJECT_NAME,
            self.PROJECT_SHORT_NAME,
            self.PROJECT_LONG_NAME,
            self._normalize_name(self.PROJECT_NAME),
            self._normalize_name(self.PROJECT_SHORT_NAME),
            self._normalize_name(self.PROJECT_LONG_NAME),
        ):
            if not target_name:
                continue
            for candidate in project_candidates:
                candidate_name = Path(candidate).name
                if candidate_name == target_name:
                    return candidate
                if self._normalize_name(candidate_name) == target_name:
                    return candidate

        if self.PROJECT_NAME:
            return Path("1 Bur-Bur", self.PROJECT_NAME).as_posix()

        if project_candidates:
            return project_candidates[0]

        return Path("1 Bur-Bur", self.PROJECT_LONG_NAME).as_posix()

    def _project_group_from_relative_path(self, relative_key: str) -> str:
        parts = Path(relative_key).parts
        if len(parts) >= 3:
            return parts[1]
        return ""

    def _resolve_hec_project_path(self) -> str:
        if self.PROJECT_GROUP:
            group_key = Path("2 Hecras", self.PROJECT_GROUP).as_posix()
            if group_key in self.PATHS:
                return self.PATHS[group_key]

        for candidate_name in (self.PROJECT_SHORT_NAME, self.PROJECT_LONG_NAME):
            candidate_key = Path("2 Hecras", candidate_name).as_posix()
            if candidate_key in self.PATHS:
                return self.PATHS[candidate_key]

        return self.HEC_PATH

    @staticmethod
    def _normalize_name(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z]+", "", value).upper()

    @classmethod
    def _names_match(cls, left: str, right: str) -> bool:
        left_norm = cls._normalize_name(cls._clean_cell(left or ""))
        right_norm = cls._normalize_name(cls._clean_cell(right or ""))
        if not left_norm or not right_norm:
            return False
        return (
            left_norm == right_norm
            or left_norm.endswith(right_norm)
            or right_norm.endswith(left_norm)
        )

    def _resolve_child_directory(
        self,
        parent: Path,
        name: str,
        create_fallback: bool = False,
    ) -> Path:
        clean_name = self._clean_cell(name)
        exact_path = parent / clean_name
        if exact_path.is_dir():
            return exact_path

        if "*" in clean_name or "?" in clean_name:
            candidates = [path for path in parent.glob(clean_name) if path.is_dir()]
        else:
            normalized_name = self._normalize_name(clean_name)
            candidates = [
                path
                for path in parent.iterdir()
                if path.is_dir()
                and (
                    self._normalize_name(path.name) == normalized_name
                    or self._normalize_name(path.name).startswith(normalized_name)
                )
            ] if parent.is_dir() else []

        if candidates:
            return self._select_best_path(candidates)

        if create_fallback:
            return exact_path

        raise FileNotFoundError(
            f"Could not find folder matching {clean_name!r} inside {parent}"
        )

    def _resolve_versioned_file(
        self,
        folder: Path,
        file_stem: str | None,
        default_contains: str,
        extension: str,
    ) -> Path:
        if not folder.is_dir():
            raise FileNotFoundError(f"Folder does not exist: {folder}")

        clean_extension = extension if extension.startswith(".") else f".{extension}"
        clean_stem = self._clean_cell(file_stem or "")

        pattern = f"*{default_contains}*{clean_extension}"
        if clean_stem:
            pattern = clean_stem if any(char in clean_stem for char in "*?") else f"{clean_stem}*"
            if Path(pattern).suffix == "":
                pattern = f"{pattern}{clean_extension}"

        candidates = [
            path
            for path in folder.rglob(pattern)
            if path.is_file() and path.suffix.lower() == clean_extension.lower()
        ]

        normalized_stem = self._normalize_name(clean_stem)
        normalized_contains = self._normalize_name(default_contains)
        if not candidates:
            candidates = [
                path
                for path in folder.rglob("*")
                if path.is_file()
                and path.suffix.lower() == clean_extension.lower()
                and (
                    (
                        not clean_stem
                        and normalized_contains in self._normalize_name(path.stem)
                    )
                    or (
                        clean_stem
                        and self._normalize_name(path.stem).startswith(normalized_stem)
                    )
                )
            ]

        if candidates:
            return self._select_best_path(candidates)

        expected = (
            f"a {clean_extension} file containing {default_contains!r}"
            if not clean_stem
            else f"a {clean_extension} file matching {clean_stem!r}"
        )
        raise FileNotFoundError(
            f"Could not find {expected} anywhere inside sub-project folder: {folder}"
        )

    def _select_best_path(self, paths: list[Path]) -> Path:
        return sorted(paths, key=self._path_sort_key, reverse=True)[0]

    def _path_sort_key(self, path: Path) -> tuple[int, float, str]:
        try:
            modified_time = path.stat().st_mtime
        except OSError:
            modified_time = 0.0
        return (self._version_number(path.name), modified_time, path.name.upper())

    @staticmethod
    def _version_number(name: str) -> int:
        matches = re.findall(r"(?:^|[_\-\s])V(\d+)(?=$|[^0-9])", name, flags=re.IGNORECASE)
        return max((int(match) for match in matches), default=0)

    def _project_long_name_from_file(self, file_path: str) -> str:
        stem = Path(file_path).stem
        escaped_dirname = re.escape(self.CROSS_SECTION_DIRNAME)
        return re.sub(
            rf"_{escaped_dirname}(?:[_-]?V\d+)?$",
            "",
            stem,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _project_short_name_from_long_name(project_long_name: str) -> str:
        prefix = "BUR-BUR-MER-"
        if project_long_name.upper().startswith(prefix):
            return project_long_name[len(prefix):]
        return project_long_name

    @staticmethod
    def _project_long_name_from_short_name(project_short_name: str) -> str:
        project_short_name = str(project_short_name or "").strip()
        if not project_short_name:
            return ""
        prefix = "BUR-BUR-MER-"
        if project_short_name.upper().startswith(prefix):
            return project_short_name
        return f"{prefix}{project_short_name}"


def parse_direct_run_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the master project folder layout. By default this creates the "
            "essential top-level folders under the supplied master path."
        )
    )
    parser.add_argument(
        "master_project_path",
        nargs="?",
        help=(
            "Master project folder path. If omitted, DIRECT_RUN_MASTER_PROJECT_PATH "
            "from configProject.py is used."
        ),
    )
    parser.add_argument(
        "--source",
        choices=["sheet", "folder", "auto"],
        help="Structure source to use while building the config. Defaults to folder for direct runs.",
    )
    parser.add_argument(
        "--sheet-name",
        help="Optional Google Sheet tab name. Defaults to 'Structure'.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Create the full resolved directory set, including the active project/sub-project paths.",
    )
    return parser.parse_args()


def build_direct_run_config(args: argparse.Namespace) -> Config:
    master_project_path = args.master_project_path or DIRECT_RUN_MASTER_PROJECT_PATH
    source = args.source or DIRECT_RUN_SOURCE or ("folder" if master_project_path else "sheet")
    sheet_name = args.sheet_name or DIRECT_RUN_SHEET_NAME

    if not master_project_path and source == "folder":
        source = "sheet"

    if master_project_path:
        return Config(
            structure_source=source,
            master_project_path=master_project_path,
            project_folder=master_project_path,
            sheet_name=sheet_name,
            create_master_folder_if_missing=True,
        )

    return Config(
        structure_source=source,
        sheet_name=sheet_name,
    )


if __name__ == "__main__":
    args = parse_direct_run_args()
    config = build_direct_run_config(args)
    print(f"Loaded project structure for: {config.PROJECT_FOLDER}")

    prepare_full_structure = args.full or DIRECT_RUN_PREPARE_FULL_STRUCTURE
    if prepare_full_structure:
        config.setup_directories()
    else:
        config.setup_essential_directories()
