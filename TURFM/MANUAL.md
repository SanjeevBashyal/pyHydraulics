# TURFM / HEC-RAS Merged Run Manual

This folder is a standalone merged version of the previous `testing_junction`
and `tesing_structure` workflows.

The merged workflow supports:

- single-reach model generation and flow screening
- optional structure insertion for a single reach
- junction model generation and flow screening
- optional structure insertion on the main and tributary reaches in junction mode

The two main files are:

- `TURFM.py`: command-line entry point
- `hecras.py`: geometry building, file writing, screening, and compute logic

## What The Workflow Does

At a high level, the workflow:

1. discovers the required input files from the project folder
2. reads cross sections from CSV
3. reads bank lines from shapefiles
4. builds a river centerline and reach lengths
5. optionally builds culvert/structure nodes from a structure CSV
6. selects hydrology point(s) from the KMZ within a buffer distance
7. screens flows from `Q1000` down to `Q5`
8. writes HEC-RAS project files for each trial flow under `runs/`
9. writes the final representative project in the main output folder
10. optionally tries to run HEC-RAS through `ras-commander`

## Folder Structure

`TURFM.py` assumes that `PROJECT_ROOT` is the folder that contains the script
itself. In this merged folder, that means the folder layout must look like this:

```text
testing_merged/
├── 0 Proj/
│   └── TUREF_CM30_projection.prj
├── 1 Bur-Bur/
│   ├── <model-folder-1>/
│   │   ├── KESIT_TESLIM/
│   │   │   └── *.csv
│   │   ├── SEV_USTU/
│   │   │   └── *.shp
│   │   └── ROLEVE/
│   │       └── structure_dim/
│   │           └── *.csv
│   └── <model-folder-2>/
│       ├── KESIT_TESLIM/
│       │   └── *.csv
│       ├── SEV_USTU/
│       │   └── *.shp
│       └── ROLEVE/
│           └── structure_dim/
│               └── *.csv
├── 2 Hydrology/
│   └── Burdur Points.kmz
├── 3 Hecras/
│   ├── code_generated/
│   └── self_example/
│       └── *.g01
├── working/
│   └── fixed_shp/
│       └── *_combined.shp
├── TURFM.py
├── hecras.py
└── README.md
```

### Required Subfolders

- `0 Proj`
  - must contain the projection file used for GIS reprojection and copied into
    generated HEC-RAS folders

- `1 Bur-Bur`
  - contains one subfolder per model

- `2 Hydrology`
  - must contain the hydrology KMZ file

- `3 Hecras/code_generated`
  - output root for generated HEC-RAS projects

- `3 Hecras/self_example`
  - optional but strongly recommended for junction runs
  - used to read naming and manual reference geometry information

- `working/fixed_shp`
  - optional
  - if present, can provide a combined main/tributary bank shapefile for a
    junction run

## Required Input Files Per Model

Each model folder under `1 Bur-Bur` is expected to contain:

- `KESIT_TESLIM/*.csv`
  - exactly one cross-section CSV

- `SEV_USTU/*.shp`
  - exactly one bank-line shapefile

Optional:

- `ROLEVE/structure_dim/*.csv`
  - exactly one structure CSV if the model needs culverts/structures

The script discovers these automatically.

## Important Path Configuration

These constants are defined near the top of `TURFM.py`:

- `PROJECT_ROOT = SCRIPT_DIR`
- `BUR_BUR_ROOT = PROJECT_ROOT / "1 Bur-Bur"`
- `HEC_OUTPUT_ROOT = PROJECT_ROOT / "3 Hecras" / "code_generated"`
- `HYDROLOGY_KMZ = PROJECT_ROOT / "2 Hydrology" / "Burdur Points.kmz"`
- `PROJECTION_FILE = PROJECT_ROOT / "0 Proj" / "TUREF_CM30_projection.prj"`
- `WORKING_FIXED_SHP_ROOT = PROJECT_ROOT / "working" / "fixed_shp"`
- `SELF_EXAMPLE_ROOT = PROJECT_ROOT / "3 Hecras" / "self_example"`
- `RAS_EXE_PATH = Path(r"C:\\Program Files (x86)\\HEC\\HEC-RAS\\6.6\\Ras.exe")`

This means the folder becomes portable as long as the internal layout above is
preserved.

## CLI Commands

List available model folders:

```bash
python TURFM.py --list-models
```

Run a single model:

```bash
python TURFM.py BUR-BUR-MER-ATATURK-Rev-V1
```

Run a single model and force a specific structure CSV:

```bash
python TURFM.py BUR-BUR-MER-ATATURK-Rev-V1 \
  --structure-csv "F:/path/to/structure_dim_enriched.csv"
```

Run a single model and disable structure loading:

```bash
python TURFM.py BUR-BUR-MER-ATATURK-Rev-V1 --no-structures
```

Run a junction model:

```bash
python TURFM.py \
  --main-folder BUR-BUR-MER-ATATURK-Rev-V1 \
  --tributary-folder BUR-BUR-MER-ATATURK-T
```

Run a junction model with explicit structure CSVs:

```bash
python TURFM.py \
  --main-folder BUR-BUR-MER-ATATURK-Rev-V1 \
  --tributary-folder BUR-BUR-MER-ATATURK-T \
  --main-structure-csv "F:/path/to/main_structure.csv" \
  --tributary-structure-csv "F:/path/to/tributary_structure.csv"
```

Run a junction model without structures:

```bash
python TURFM.py \
  --main-folder BUR-BUR-MER-ATATURK-Rev-V1 \
  --tributary-folder BUR-BUR-MER-ATATURK-T \
  --no-structures
```

## Run Modes

### Single-Reach Mode

Single-reach mode is used when the positional `model_folder` argument is
provided and neither `--main-folder` nor `--tributary-folder` is set.

The script:

- finds one model folder under `1 Bur-Bur`
- discovers its cross-section CSV and bank-line shapefile
- optionally discovers a structure CSV
- selects one hydrology point from the KMZ
- tests return periods in this order:
  - `Q1000`
  - `Q500`
  - `Q100`
  - `Q50`
  - `Q25`
  - `Q10`
  - `Q5`
- writes trial projects into `runs/`
- keeps the final representative project in the main output folder

### Junction Mode

Junction mode is used when both `--main-folder` and `--tributary-folder` are
provided.

The script:

- discovers the main and tributary model folders
- builds separate geometry contexts for both
- finds a reference geometry in `3 Hecras/self_example`
- optionally finds a combined bank shapefile in `working/fixed_shp`
- derives a coupled junction geometry
- selects one hydrology point for the main reach
- selects one hydrology point for the tributary reach
- sums the two flows to get the lower reach flow
- screens the same return periods from `Q1000` to `Q5`
- writes trial projects into `runs/`
- writes the final representative junction model in the main output folder

## Geometry And File Creation Logic

The main logic lives in `hecras.py`.

### Primary Entry Methods

- `build_steady_1d_model(...)`
  - builds a single-reach HEC-RAS model without flow screening

- `screen_steady_flows_from_kmz(...)`
  - full single-reach screening workflow

- `screen_steady_junction_flows_from_kmz(...)`
  - full junction screening workflow

### Geometry Preparation

For a single reach, `_prepare_geometry_context(...)`:

- reads the cross-section CSV
- normalizes river and reach names
- reads bank lines from the shapefile
- repairs bank openings when possible
- groups bank lines by connectivity
- intersects each cross section with the bank lines
- identifies left and right bank stations
- filters near-duplicate sections if needed
- derives a centerline
- computes reach lengths
- assigns river stations
- optionally builds structures from the structure CSV

For a junction, `_prepare_junction_geometry_context(...)`:

- prepares geometry for the main reach
- prepares geometry for the tributary reach
- identifies the local confluence opening
- optionally uses a combined bank shapefile
- extends the tributary to the junction point
- splits the main reach into:
  - upper main reach
  - lower reach
- applies junction naming from the reference geometry when available
- builds three reach contexts:
  - tributary
  - main
  - lower

## How Hydrology Selection Works

Hydrology points are loaded from `2 Hydrology/Burdur Points.kmz`.

For a single reach:

- the code computes distance from every hydrology point to the derived river
  centerline
- only points within `HYDROLOGY_BUFFER_METERS` are considered
- the closest candidate is selected

For a junction:

- the same selection is done once for the main reach
- and once for the tributary reach
- and once for the lower reach downstream of the junction when a distinct
  downstream point is available

The current default search buffer is:

- `HYDROLOGY_BUFFER_METERS = 150.0`

## Bank And Centerline Behavior

These settings are defined in `TURFM.py`:

- `BANK_STATION_MODE = "snap"`
- `RIVER_LINE_METHOD = "simple_distance"`
- `CENTERLINE_SAMPLES_PER_SEGMENT = 500`

### `BANK_STATION_MODE`

- `snap`
  - snaps bank stations to surveyed cross-section profile points and tries to
    refine them

- `interpolate`
  - inserts exact bank-intersection stations into the profile

### `RIVER_LINE_METHOD`

- `simple_distance`
  - builds centerline points from the midpoint between bank measures along
    adjacent cross sections

- `perpendicular`
  - builds a midpoint-based centerline from the two grouped banks over the full
    reach length

## Structure Support

The merged workflow includes structure support from the former
`tesing_structure` branch.

### Supported Structure Type

Currently implemented:

- box culverts only

If `structure_type` is not `box`, the code raises `NotImplementedError`.

### Structure CSV Discovery

Automatic discovery looks in:

```text
<model-folder>/ROLEVE/structure_dim/*.csv
```

If a structure CSV is not present there, you can pass one explicitly with:

- `--structure-csv`
- `--main-structure-csv`
- `--tributary-structure-csv`

### Required Structure CSV Columns

The merged code currently requires these columns:

- `structure_type`
- `upstream_invert_elevation`
- `downstream_invert_elevation`
- `rise_upstream`
- `span_upstream`
- `rise_downstream`
- `span_downstream`
- `min_rise`
- `min_span`
- `culvert_length`
- `upstream_point_1_x`
- `upstream_point_1_y`
- `upstream_point_2_x`
- `upstream_point_2_y`
- `downstream_point_1_x`
- `downstream_point_1_y`
- `downstream_point_2_x`
- `downstream_point_2_y`

Optional fields are also used when present:

- `structure_name`
- `opening_offset_from_left_bank`
- `minimum_deck_cover`
- `deck_distance`
- `deck_width`
- `deck_weir_coefficient`
- `deck_skew`
- `deck_max_submerge`
- `culvert_mannings_n`
- `culvert_bottom_n`
- `entrance_loss`
- `exit_loss`
- `inlet_type`
- `outlet_type`
- `culvert_chart_number`
- `num_barrels`

## Generated Files

All model writing flows through `_write_model_files(...)`.

That method writes:

- `<project_stem>.g01`
  - HEC-RAS geometry file

- `<project_stem>.f01`
  - HEC-RAS steady flow file

- `<project_stem>.p01`
  - HEC-RAS plan file

- `<project_stem>.prj`
  - HEC-RAS project file

- `RASImport.sdf`
  - GIS import file

- copied projection file
  - for example `TUREF_CM30_projection.prj`

### Geometry File Writing

Single reach geometry is written by `_write_geometry_file(...)`.

Junction geometry is written by `_write_junction_geometry_file(...)`.

These geometry writers include:

- reach polylines
- cross sections
- bank stations
- levee lines
- Manning's n values
- reach lengths
- optional structure nodes
- junction node information for coupled models

### Flow File Writing

Single reach steady flow is written by `_write_steady_flow_file(...)`.

Junction steady flow is written by `_write_junction_steady_flow_file(...)`.

In junction mode, the flow file contains:

- tributary flow
- main flow
- lower combined flow

### SDF Writing

Single-reach SDF is written by `_write_sdf_file(...)`.

Junction SDF is written by `_write_junction_sdf_file(...)`.

The SDF file contains:

- stream network
- centerline vertices
- cross-section cut lines
- bank fractions
- reach lengths

## Output Folder Layout

### Single-Reach Output

```text
3 Hecras/code_generated/<model-name>/
├── <model-name>.prj
├── <model-name>.g01
├── <model-name>.f01
├── <model-name>.p01
├── RASImport.sdf
├── flow_screening_report.csv
├── flow_screening_report.txt
└── runs/
    ├── Q1000_.../
    ├── Q500_.../
    ├── Q100_.../
    └── ...
```

### Junction Output

```text
3 Hecras/code_generated/<junction-output-name>/
├── <junction-output-name>.prj
├── <junction-output-name>.g01
├── <junction-output-name>.f01
├── <junction-output-name>.p01
├── RASImport.sdf
├── flow_screening_report.csv
├── flow_screening_report.txt
└── runs/
    ├── Q1000_.../
    ├── Q500_.../
    ├── Q100_.../
    └── ...
```

## How Screening Decides The Final Model

For each tested return period:

- a run folder is created
- model files are written
- `compute_project(...)` is called
- results are checked for overbank flow if steady results exist

### Lower-Reach Flow Override Logic In Junction Mode

The merged junction logic now performs an extra downstream check.

If a distinct hydrology point is found on the lower reach downstream of the
junction, the code compares:

- `upstream_sum = main_flow + tributary_flow`
- `downstream_point_flow = flow from the lower-reach hydrology point`

Then it chooses the lower-reach flow like this:

- if `upstream_sum > downstream_point_flow`, use `upstream_sum`
- if `upstream_sum < downstream_point_flow`, use `downstream_point_flow`
- if no distinct downstream lower-reach point exists, use `upstream_sum`

This comparison is performed separately for every tested return period.

The terminal log now shows, for each return period:

- main flow
- tributary flow
- upstream sum
- downstream-point flow
- chosen lower flow
- source used

The text report now records the same information using these fields:

- `main`
- `tributary`
- `upstream_sum`
- `downstream_point`
- `lower_source`
- `lower`

Selection logic:

- if a run computes successfully and stays in bank, that becomes the maximum
  safe run and screening stops early
- if no run stays in bank but some runs compute, the lowest successful run is
  kept as the review case
- if no runs compute successfully, the workflow still writes the report and may
  still leave generated files behind

## Important Naming Behavior In Junction Mode

The script tries to produce a stable junction output name in this order:

1. use the reference geometry stem from `3 Hecras/self_example/*.g01`
2. otherwise infer a project stem from the source river names
3. otherwise fall back to `<main>__<tributary>`

This is why the current merged junction run produced an output folder named
`Ataturk1T`.

## Verification Utilities

`hecras.py` also includes:

- `compute_project(...)`
  - runs the generated plan through `ras-commander`

- `smoke_test(...)`
  - verifies that the generated project can be parsed and reports counts for
    cross sections, bridges, and culverts

## Current Known Environment Issue

The workflow can generate the HEC-RAS files correctly even if HEC-RAS itself is
not reachable.

At the moment, the main runtime blocker is the executable path:

```text
C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe
```

If that path is not valid from the runtime environment, the code will:

- still create model files
- still create run folders and reports
- fail during actual HEC-RAS computation

Typical symptoms:

- `Ras.exe was not found`
- `HEC-RAS compute did not produce steady results`

## Practical Run Checklist

Before running:

1. confirm the folder layout matches this README
2. confirm each model folder has one CSV under `KESIT_TESLIM`
3. confirm each model folder has one SHP under `SEV_USTU`
4. confirm `2 Hydrology/Burdur Points.kmz` exists
5. confirm `0 Proj/TUREF_CM30_projection.prj` exists
6. for junction runs, confirm `3 Hecras/self_example/*.g01` exists
7. if structures are needed, confirm the structure CSV is present or pass it on
   the command line
8. confirm `RAS_EXE_PATH` points to a valid HEC-RAS installation if you want
   actual computation, not just file generation

## Example From This Merged Folder

The current standalone merged folder was run with:

```bash
python TURFM.py \
  --main-folder BUR-BUR-MER-ATATURK-Rev-V1 \
  --tributary-folder BUR-BUR-MER-ATATURK-T
```

That created the junction output here:

```text
3 Hecras/code_generated/Ataturk1T/
```

and individual screening trial folders here:

```text
3 Hecras/code_generated/Ataturk1T/runs/
```

In the current example data set, a distinct downstream main-reach point named
`BUR-BUR-MER-ATATURK` is available below the junction, so the report shows both
the downstream point flow and the upstream-sum flow for every return period.

## Summary

Use `TURFM.py` when you want the full automated workflow.

Use `hecras.py` when you want programmatic access to:

- geometry preparation
- model writing
- screening logic
- compute calls
- smoke testing

This merged folder is intended to be self-contained. If you keep the internal
folder layout intact and point `RAS_EXE_PATH` to a valid HEC-RAS installation,
the same folder can be moved and run as a complete project package.
