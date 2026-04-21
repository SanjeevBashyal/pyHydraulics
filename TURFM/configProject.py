from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sheets import GoogleSheetsClient, sheet_id


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
    cross_section_path: str
    cross_section_file_path: str
    bank_line_path: str
    bank_line_file_path: str


@dataclass
class Config:
    """
    Reads the Google Sheet Structure tab and builds the project folder tree.

    Similar to configProject.py, this class exposes the legacy path attributes
    required by callDTM.py/implementation.py while still using the Structure
    worksheet as the source of truth for the base folder layout.
    """

    credentials_file: str = "master_credentials.json"
    workbook_id: str = sheet_id
    worksheet_name: str = "Structure"
    project_folder: str | None = None
    PROJECT_NAME: str = "ATATURK"
    SUB_PROJECT_NAME: str = "ATATURK-T"
    PROJECT_SHORT_NAME: str = "ATATURK-T"

    # Processing Config
    BLEND_TYPE: str = "linear"
    HECRAS_VERSION: str = "6.7"
    RAS_EXE_PATH: str = r"C:\Program Files (x86)\HEC\HEC-RAS\6.7 Beta 4\Ras.exe"
    DEM_FILENAME: str = "SET4_27_DTM_070226_R1.tif"
    CROSS_SECTION_DIRNAME: str = "KESIT_TESLIM"
    BANK_LINE_DIRNAME: str = "SEV_USTU"

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
    PROJECT_GROUP: str = field(init=False, default="")

    DEM_PATH: str = field(init=False)
    CROSS_SECTION_PATH: str = field(init=False)
    CROSS_SECTION_FILE_PATH: str = field(init=False)
    BANK_LINE_PATH: str = field(init=False)
    BANK_LINE_FILE_PATH: str = field(init=False)

    def __post_init__(self):
        rows = self._read_structure_sheet()
        master_path, sheet_project_path, structure_rows = self._parse_sheet(rows)

        self.MASTER_PATH = master_path
        self.PROJECT_FOLDER = self._clean_cell(self.project_folder or sheet_project_path)

        if not self.PROJECT_FOLDER:
            raise ValueError("Project folder path could not be resolved from the sheet.")

        self._build_folder_entries(structure_rows)
        self._rebuild_paths()
        self._refresh_compatibility_paths()

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
        self.PROJECT_LONG_NAME = f"BUR-BUR-MER-{self.PROJECT_SHORT_NAME}"

        self.ESSENTIALS_PATH = self._sheet_path_or_default("0 Essentials")
        self.BUR_BUR_PATH = self._sheet_path_or_default("1 Bur-Bur")
        self.HEC_PATH = self._sheet_path_or_default("2 Hecras")
        self.GIS_PATH = self._sheet_path_or_default("3 GIS")
        self.OUTPUT_ROOT_PATH = self._sheet_path_or_default("4 Outputs")
        self.OUTPUT_PATH = self.OUTPUT_ROOT_PATH
        self.TEMP_PATH = self._sheet_path_or_default("5 Temp")

        project_relative_path = self._resolve_project_relative_path()
        self.PROJECT_GROUP = self._project_group_from_relative_path(project_relative_path)
        self.PROJECT_DATA_PATH = self._absolute_from_relative_path(project_relative_path)
        self.HEC_PROJECT_PATH = self._resolve_hec_project_path()
        self.HEC_SUB_PROJECT_PATH = str(Path(self.HEC_PROJECT_PATH) / self.SUB_PROJECT_NAME)

        self.DEM_PATH = str(Path(self.ESSENTIALS_PATH) / self.DEM_FILENAME)
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
    ) -> SubProjectPaths:
        """
        Resolves all important paths for one project/sub-project.

        File lookup is intentionally wildcard-based. Passing
        cross_section_stem="abc" searches for "abc*.csv"; leaving it blank
        searches for "*KESIT_TESLIM*.csv". Bank lines behave the same way
        with ".shp" files.
        """
        project_path = self.get_project_path(project_name)
        sub_project_path = self.get_sub_project_path(project_name, sub_project_name)
        hecras_project_path = self.get_hecras_project_path(project_name)
        hecras_sub_project_path = str(Path(hecras_project_path) / Path(sub_project_path).name)

        cross_section_path = self.get_cross_section_folder_path(project_name, sub_project_name)
        cross_section_file_path = self.find_cross_section_file(
            project_name,
            sub_project_name,
            file_stem=cross_section_stem,
        )

        bank_line_path = self.get_bank_line_folder_path(project_name, sub_project_name)
        bank_line_file_path = self.find_bank_line_file(
            project_name,
            sub_project_name,
            file_stem=bank_line_stem,
        )

        return SubProjectPaths(
            project_name=Path(project_path).name,
            sub_project_name=Path(sub_project_path).name,
            project_path=project_path,
            sub_project_path=sub_project_path,
            hecras_project_path=hecras_project_path,
            hecras_sub_project_path=hecras_sub_project_path,
            cross_section_path=cross_section_path,
            cross_section_file_path=cross_section_file_path,
            bank_line_path=bank_line_path,
            bank_line_file_path=bank_line_file_path,
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
        )

        self.PROJECT_NAME = paths.project_name
        self.SUB_PROJECT_NAME = paths.sub_project_name
        self.PROJECT_GROUP = paths.project_name
        self.PROJECT_DATA_PATH = paths.sub_project_path
        self.HEC_PROJECT_PATH = paths.hecras_project_path
        self.HEC_SUB_PROJECT_PATH = paths.hecras_sub_project_path

        self.CROSS_SECTION_PATH = paths.cross_section_path
        self.CROSS_SECTION_FILE_PATH = paths.cross_section_file_path
        self.BANK_LINE_PATH = paths.bank_line_path
        self.BANK_LINE_FILE_PATH = paths.bank_line_file_path

        self.PROJECT_LONG_NAME = self._project_long_name_from_file(paths.cross_section_file_path)
        self.PROJECT_SHORT_NAME = self._project_short_name_from_long_name(self.PROJECT_LONG_NAME)
        self.OUTPUT_PATH = paths.hecras_sub_project_path if output_to_hecras else self.OUTPUT_ROOT_PATH

        return paths

    def get_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.BUR_BUR_PATH), project_name))

    def get_sub_project_path(self, project_name: str, sub_project_name: str) -> str:
        project_path = Path(self.get_project_path(project_name))
        return str(self._resolve_child_directory(project_path, sub_project_name))

    def get_hecras_project_path(self, project_name: str) -> str:
        return str(self._resolve_child_directory(Path(self.HEC_PATH), project_name, create_fallback=True))

    def get_cross_section_folder_path(self, project_name: str, sub_project_name: str) -> str:
        sub_project_path = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(self._resolve_child_directory(sub_project_path, self.CROSS_SECTION_DIRNAME))

    def get_bank_line_folder_path(self, project_name: str, sub_project_name: str) -> str:
        sub_project_path = Path(self.get_sub_project_path(project_name, sub_project_name))
        return str(self._resolve_child_directory(sub_project_path, self.BANK_LINE_DIRNAME))

    def find_cross_section_file(
        self,
        project_name: str,
        sub_project_name: str,
        file_stem: str | None = None,
    ) -> str:
        folder = Path(self.get_cross_section_folder_path(project_name, sub_project_name))
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
        folder = Path(self.get_bank_line_folder_path(project_name, sub_project_name))
        return str(
            self._resolve_versioned_file(
                folder=folder,
                file_stem=file_stem,
                default_contains=self.BANK_LINE_DIRNAME,
                extension=".shp",
            )
        )

    def get_path(self, *parts: str) -> str:
        """Returns an absolute path for a relative folder path from the sheet."""
        relative_key = Path(*parts).as_posix()
        return self.PATHS[relative_key]

    def setup_directories(self):
        """Creates the folder structure defined in the Structure sheet."""
        directories = {
            Path(self.PROJECT_FOLDER),
            *[Path(self.PATHS[entry.relative_path.as_posix()]) for entry in self.FOLDER_ENTRIES],
            Path(self.PROJECT_DATA_PATH),
            Path(self.HEC_PROJECT_PATH),
            Path(self.HEC_SUB_PROJECT_PATH),
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
            self.PROJECT_SHORT_NAME,
            self.PROJECT_LONG_NAME,
            self._normalize_name(self.PROJECT_SHORT_NAME),
            self._normalize_name(self.PROJECT_LONG_NAME),
        ):
            for candidate in project_candidates:
                candidate_name = Path(candidate).name
                if candidate_name == target_name:
                    return candidate
                if self._normalize_name(candidate_name) == target_name:
                    return candidate

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

        if clean_stem:
            pattern = clean_stem if any(char in clean_stem for char in "*?") else f"{clean_stem}*"
            if Path(pattern).suffix == "":
                pattern = f"{pattern}{clean_extension}"
        else:
            pattern = f"*{default_contains}*{clean_extension}"

        candidates = [
            path
            for path in folder.glob(pattern)
            if path.is_file() and path.suffix.lower() == clean_extension.lower()
        ]

        if not candidates:
            normalized_stem = self._normalize_name(clean_stem)
            normalized_contains = self._normalize_name(default_contains)
            candidates = [
                path
                for path in folder.iterdir()
                if path.is_file()
                and path.suffix.lower() == clean_extension.lower()
                and (
                    not clean_stem
                    and normalized_contains in self._normalize_name(path.stem)
                    or clean_stem
                    and self._normalize_name(path.stem).startswith(normalized_stem)
                )
            ]

        if candidates:
            return self._select_best_path(candidates)

        raise FileNotFoundError(
            f"Could not find a {clean_extension} file matching {pattern!r} inside {folder}"
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


if __name__ == "__main__":
    config = Config()
    print(f"Loaded project structure for: {config.PROJECT_FOLDER}")
    config.setup_directories()
