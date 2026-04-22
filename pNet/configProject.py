from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd


DEFAULT_NETWORK_DIR = Path("MTGHP-core")

BC_SYNONYMS = {
    "known ws": "Known WSE",
    "known wse": "Known WSE",
    "known water surface": "Known WSE",
    "known depth": "Known Depth",
    "normal depth": "Normal Depth",
    "critical depth": "Critical Depth",
    "none": "None",
    "spill end": "Spill End",
    "spill zero": "Spill Zero",
}


@dataclass(frozen=True)
class BoundaryCondition:
    label: str
    channel: str
    end: str
    bc_type: str
    value: Optional[float]
    remarks: str = ""


@dataclass
class ChannelGeometry:
    name: str
    upstream_node: str
    downstream_node: str
    dataframe: pd.DataFrame
    upstream_bc_type: str = ""
    upstream_bc_value: Optional[float] = None
    downstream_bc_type: str = ""
    downstream_bc_value: Optional[float] = None
    junction_delta: float = 0.0

    @property
    def n_sections(self) -> int:
        return len(self.dataframe)

    @property
    def length(self) -> float:
        if self.dataframe.empty:
            return 0.0
        return float(self.dataframe["chainage"].iloc[-1])

    @property
    def start_bed(self) -> float:
        return float(self.dataframe["bed_elevation"].iloc[0])

    @property
    def end_bed(self) -> float:
        return float(self.dataframe["bed_elevation"].iloc[-1])

    @property
    def start_width(self) -> float:
        return float(self.dataframe["bed_width"].iloc[0])

    @property
    def end_width(self) -> float:
        return float(self.dataframe["bed_width"].iloc[-1])

    @property
    def start_side_slope(self) -> float:
        return float(self.dataframe["side_slope"].iloc[0])

    @property
    def end_side_slope(self) -> float:
        return float(self.dataframe["side_slope"].iloc[-1])

    @property
    def start_angle(self) -> float:
        return _endpoint_angle(self.dataframe, at_start=True)

    @property
    def end_angle(self) -> float:
        return _endpoint_angle(self.dataframe, at_start=False)

    @property
    def has_lateral_spill(self) -> bool:
        return bool(self.dataframe["spill_left_on"].any() or self.dataframe["spill_right_on"].any())

    @property
    def is_closed_outlet(self) -> bool:
        return self.downstream_node.lower() == "outlet" and self.downstream_bc_type == "None"

    @property
    def is_spill_end(self) -> bool:
        return self.downstream_node.lower() == "outlet" and self.downstream_bc_type == "Spill End"

    @property
    def is_spill_zero(self) -> bool:
        return self.upstream_node.lower() == "inlet" and self.upstream_bc_type == "Spill Zero"


@dataclass
class NodeConnectivity:
    node: str
    incoming: List[str]
    outgoing: List[str]


@dataclass
class ProjectConfig:
    root: Path
    master_path: Path
    master: pd.DataFrame
    channels: Dict[str, ChannelGeometry]
    boundaries: List[BoundaryCondition]
    topology: Dict[str, NodeConnectivity]

    def channel_names(self) -> List[str]:
        return list(self.channels)

    def summary(self) -> pd.DataFrame:
        rows = []
        for channel in self.channels.values():
            rows.append(
                {
                    "channel": channel.name,
                    "upstream_node": channel.upstream_node,
                    "downstream_node": channel.downstream_node,
                    "sections": channel.n_sections,
                    "length": channel.length,
                    "start_bed": channel.start_bed,
                    "end_bed": channel.end_bed,
                    "start_width": channel.start_width,
                    "end_width": channel.end_width,
                    "upstream_bc_type": channel.upstream_bc_type,
                    "upstream_bc_value": channel.upstream_bc_value,
                    "downstream_bc_type": channel.downstream_bc_type,
                    "downstream_bc_value": channel.downstream_bc_value,
                    "has_lateral_spill": channel.has_lateral_spill,
                    "is_closed_outlet": channel.is_closed_outlet,
                    "is_spill_end": channel.is_spill_end,
                    "is_spill_zero": channel.is_spill_zero,
                    "start_angle_deg": channel.start_angle,
                    "end_angle_deg": channel.end_angle,
                    "junction_delta_deg": channel.junction_delta,
                }
            )
        return pd.DataFrame(rows)

    def boundary_table(self) -> pd.DataFrame:
        return pd.DataFrame([boundary.__dict__ for boundary in self.boundaries])

    def topology_table(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "node": node.node,
                "incoming": ", ".join(node.incoming),
                "outgoing": ", ".join(node.outgoing),
            }
            for node in self.topology.values()
        )


def canonical_bc_type(value) -> str:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    key = " ".join(text.lower().split())
    return BC_SYNONYMS.get(key, text)


def float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def yes_no(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"yes", "true", "1"}


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols: List[str] = []
    seen: Dict[str, int] = {}
    for idx, name in enumerate(df.columns):
        label = str(name).strip()
        if not label or label.lower().startswith("unnamed:"):
            label = f"blank_{idx}"
        if label in seen:
            seen[label] += 1
            label = f"{label}.{seen[label]}"
        else:
            seen[label] = 0
        cols.append(label)
    result = df.copy()
    result.columns = cols
    return result


def optional_series(df: pd.DataFrame, column_name: str, default_value) -> pd.Series:
    if column_name in df.columns:
        return df[column_name]
    return pd.Series([default_value] * len(df), index=df.index)


def fill_numeric(series: pd.Series, default: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    values = values.interpolate(limit_direction="both").bfill().ffill()
    return values.fillna(default)


def angle_deg(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dy, dx))


def angle_diff(a1_deg: float, a2_deg: float) -> float:
    diff = (a2_deg - a1_deg + 180.0) % 360.0 - 180.0
    return abs(diff)


def cumulative_distances(df: pd.DataFrame) -> List[float]:
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    distances = [0.0]
    for idx in range(1, len(df)):
        distances.append(distances[-1] + math.hypot(east[idx] - east[idx - 1], north[idx] - north[idx - 1]))
    return distances


def deflection_angles(df: pd.DataFrame) -> List[float]:
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    angles = [0.0] * len(df)
    for idx in range(1, len(df) - 1):
        upstream_angle = angle_deg(east[idx] - east[idx - 1], north[idx] - north[idx - 1])
        downstream_angle = angle_deg(east[idx + 1] - east[idx], north[idx + 1] - north[idx])
        angles[idx] = angle_diff(upstream_angle, downstream_angle)
    return angles


def _endpoint_angle(df: pd.DataFrame, *, at_start: bool) -> float:
    if len(df) < 2:
        return 0.0
    if at_start:
        row0 = df.iloc[0]
        row1 = df.iloc[1]
    else:
        row0 = df.iloc[-2]
        row1 = df.iloc[-1]
    return angle_deg(float(row1["easting"] - row0["easting"]), float(row1["northing"] - row0["northing"]))


def boundary_label(channel_name: str, end: str) -> str:
    return f"{'Inlet' if end == 'start' else 'Outlet'}-{channel_name}"


def discover_model_files(root: Path = DEFAULT_NETWORK_DIR) -> Dict[str, Path]:
    root = Path(root)
    files = {"master": root / "master.xlsx"}
    for csv_path in sorted(root.glob("*.csv")):
        files[csv_path.stem] = csv_path
    return files


def load_channel_csv(csv_path: Path) -> pd.DataFrame:
    raw = clean_columns(pd.read_csv(csv_path))
    required = {"SN", "Easting", "Northing"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"{csv_path} is missing required columns: {', '.join(missing)}")

    df = raw.copy()
    df["bed_width"] = fill_numeric(optional_series(df, "Bed width", 1.0), 1.0)
    df["side_slope"] = fill_numeric(optional_series(df, "Bank slope", 0.0), 0.0)
    df["bed_elevation"] = fill_numeric(optional_series(df, "Bed elevation", 0.0), 0.0)
    df["chainage"] = cumulative_distances(df)
    df["deflection_angle"] = deflection_angles(df)
    df["section_type"] = optional_series(df, "Type", "").fillna("").astype(str)

    left_to = optional_series(df, "Spill to", "").fillna("").astype(str).str.strip()
    right_to = optional_series(df, "Spill to.1", "").fillna("").astype(str).str.strip()
    left_crest = fill_numeric(optional_series(df, "Spillway Left Elevation", float("nan")), float("nan"))
    right_crest = fill_numeric(optional_series(df, "Spill Right Elevation", float("nan")), float("nan"))

    df["spill_left_on"] = optional_series(df, "Spill left", "").map(yes_no)
    df["spill_right_on"] = optional_series(df, "Spill Right", "").map(yes_no)
    df["spill_left_to"] = left_to
    df["spill_right_to"] = right_to
    df["spill_left_crest"] = left_crest
    df["spill_right_crest"] = right_crest

    df["easting"] = pd.to_numeric(df["Easting"], errors="coerce")
    df["northing"] = pd.to_numeric(df["Northing"], errors="coerce")
    df["sn"] = pd.to_numeric(df["SN"], errors="coerce").astype("Int64")
    return df


def _column(master: pd.DataFrame, *candidates: str) -> Optional[str]:
    lower_map = {str(col).strip().lower(): col for col in master.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower_map:
            return lower_map[key]
    return None


def _build_topology(channels: Iterable[ChannelGeometry]) -> Dict[str, NodeConnectivity]:
    topology: Dict[str, NodeConnectivity] = {}
    for channel in channels:
        topology.setdefault(channel.upstream_node, NodeConnectivity(channel.upstream_node, [], [])).outgoing.append(channel.name)
        topology.setdefault(channel.downstream_node, NodeConnectivity(channel.downstream_node, [], [])).incoming.append(channel.name)
    return topology


def _build_boundaries(channels: Iterable[ChannelGeometry]) -> List[BoundaryCondition]:
    boundaries: List[BoundaryCondition] = []
    for channel in channels:
        if channel.upstream_node.lower() == "inlet":
            boundaries.append(
                BoundaryCondition(
                    label=boundary_label(channel.name, "start"),
                    channel=channel.name,
                    end="start",
                    bc_type=channel.upstream_bc_type or "Normal Depth",
                    value=channel.upstream_bc_value,
                    remarks="Master inlet boundary" if channel.upstream_bc_type else "Default inlet boundary",
                )
            )
        if channel.downstream_node.lower() == "outlet":
            boundaries.append(
                BoundaryCondition(
                    label=boundary_label(channel.name, "end"),
                    channel=channel.name,
                    end="end",
                    bc_type=channel.downstream_bc_type or "Normal Depth",
                    value=channel.downstream_bc_value,
                    remarks="Master outlet boundary" if channel.downstream_bc_type else "Default outlet boundary",
                )
            )
    return boundaries


def _assign_junction_angles(channels: Dict[str, ChannelGeometry], topology: Dict[str, NodeConnectivity]) -> None:
    for node, links in topology.items():
        if node.lower() in {"inlet", "outlet", "spill"} or not links.incoming:
            continue
        main = channels[links.incoming[0]]
        for outgoing_name in links.outgoing:
            outgoing = channels[outgoing_name]
            outgoing.junction_delta = angle_diff(main.end_angle, outgoing.start_angle)


def load_project(root: Path = DEFAULT_NETWORK_DIR) -> ProjectConfig:
    root = Path(root)
    master_path = root / "master.xlsx"
    if not master_path.exists():
        raise FileNotFoundError(f"Cannot find master workbook: {master_path}")

    master = clean_columns(pd.read_excel(master_path, keep_default_na=False))
    channel_col = _column(master, "Channel")
    upstream_col = _column(master, "Upstream", "From")
    downstream_col = _column(master, "Downstream", "To")
    up_bc_col = _column(master, "Upstream BC")
    up_bc_value_col = _column(master, "Upstream BC Value")
    dn_bc_col = _column(master, "Downstream BC")
    dn_bc_value_col = _column(master, "Downstream BC Value")

    if not channel_col or not upstream_col or not downstream_col:
        raise ValueError("master.xlsx must contain Channel, Upstream/From, and Downstream/To columns")

    channels: Dict[str, ChannelGeometry] = {}
    for _, record in master.iterrows():
        name = str(record[channel_col]).strip()
        if not name:
            continue
        csv_path = root / f"{name}.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"Channel '{name}' is listed in master.xlsx but {csv_path.name} was not found")

        channels[name] = ChannelGeometry(
            name=name,
            upstream_node=str(record[upstream_col]).strip(),
            downstream_node=str(record[downstream_col]).strip(),
            dataframe=load_channel_csv(csv_path),
            upstream_bc_type=canonical_bc_type(record[up_bc_col]) if up_bc_col else "",
            upstream_bc_value=float_or_none(record[up_bc_value_col]) if up_bc_value_col else None,
            downstream_bc_type=canonical_bc_type(record[dn_bc_col]) if dn_bc_col else "",
            downstream_bc_value=float_or_none(record[dn_bc_value_col]) if dn_bc_value_col else None,
        )

    topology = _build_topology(channels.values())
    _assign_junction_angles(channels, topology)
    boundaries = _build_boundaries(channels.values())
    return ProjectConfig(root=root, master_path=master_path, master=master, channels=channels, boundaries=boundaries, topology=topology)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect and load a steady 1D project geometry folder.")
    parser.add_argument("--root", type=Path, default=DEFAULT_NETWORK_DIR, help="Folder containing master.xlsx and channel CSV files.")
    parser.add_argument("--summary-csv", type=Path, default=None, help="Optional path to write the project summary CSV.")
    parser.add_argument("--topology-csv", type=Path, default=None, help="Optional path to write the topology CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project = load_project(args.root)
    print("Project root:", project.root.resolve())
    print("\nChannels")
    print(project.summary().to_string(index=False))
    print("\nTopology")
    print(project.topology_table().to_string(index=False))
    print("\nBoundaries")
    print(project.boundary_table().to_string(index=False))

    if args.summary_csv:
        args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
        project.summary().to_csv(args.summary_csv, index=False)
    if args.topology_csv:
        args.topology_csv.parent.mkdir(parents=True, exist_ok=True)
        project.topology_table().to_csv(args.topology_csv, index=False)


if __name__ == "__main__":
    main()
