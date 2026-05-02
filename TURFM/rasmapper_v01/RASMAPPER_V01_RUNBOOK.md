# RAS Mapper V01 Runbook

This runbook explains how to use `rasmapper_v01.py` from the standalone
folder `F:\codex_rascommander\rasmapper_v01`.

It reflects the current workflow that was validated against the Ataturk case:

- prepare the HEC-RAS project shell
- build terrain and landcover products
- clone and rewrite a reference 2D geometry
- regenerate the mesh in HEC-RAS / RAS Mapper
- enforce breaklines
- fix mesh issues
- associate `LandCover` to `Manning's n` through `Manage Geometry Associations`
- verify final Manning values
- create/register a 2D unsteady `.u##` file
- create/register a 2D unsteady plan `.p##`
- set `Current Plan=p##` in the HEC-RAS project

## 1. Prerequisites

- Python environment for this repo is installed
- HEC-RAS 6.6 is installed
- GDAL tools bundled with HEC-RAS are available
- `F:\codex_rascommander\rasmapper_v01\rasmapper_v01.py` exists and runs

Default executable paths used by the workflow:

```powershell
C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe
C:\Program Files (x86)\HEC\HEC-RAS\6.6\GDAL\bin64\gdal_grid.exe
```

## 2. Required Inputs

Create one source folder for the new site and point `files_root` at it.

Required files:

- projection `.prj`
- DTM `.tif`
- perimeter polygon shapefile
- breakline polyline shapefile
- boundary DSS `.dss`
- DSS catalog `.dsc.h5` with the same stem as the DSS
- cross-section CSV
- landcover polygon shapefile
- reference geometry `.g01`
- reference geometry `.g01.hdf`
- template unsteady file `.u##`
- template unsteady HDF `.u##.hdf`
- template plan file `.p##`

Recommended files:

- native HEC-RAS landcover `.tif`
- native HEC-RAS landcover `.hdf`

The native HEC-RAS landcover pair is the cleanest path for:

- showing `LandCover` under `Map Layers`
- using `Manage Geometry Associations`
- setting `Manning's n = LandCover`

## 3. Required Input Structure

### Cross-section CSV

The cross-section CSV must contain these columns:

- `Station`
- `X`
- `Y`
- `Z`

Notes:

- each row is one surveyed point
- rows are grouped by `Station`
- the script uses the first and last cross-sections to build upstream and downstream boundary-condition lines

### Landcover shapefile

The landcover shapefile must contain these attributes:

- `KodText`
- `Adi`
- `Manningn`

Notes:

- `KodText` is rasterized into the landcover class raster
- `Manningn` is used to build the Manning lookup and final Manning values

### DSS files

Both of these must exist:

- `<name>.dss`
- `<name>.dsc.h5`

### Reference geometry

The reference geometry must already be a valid 2D HEC-RAS geometry that opens without errors.

Important:

- `prepare` does not require a reference geometry
- `install-geometry` and `sync-geometry` do require `reference_geom_name` and `reference_geom_hdf_name`

### Unsteady and plan templates

The unsteady-plan step can create files from internal defaults, but the
recommended production path is to provide real HEC-RAS templates:

- `inputs\UnsteadyTemplate\template.u01`
- `inputs\UnsteadyTemplate\template.u01.hdf`
- `inputs\PlanTemplate\template.p01`

For each model, set these JSON fields:

- `template_unsteady_name`
- `template_unsteady_hdf_name`
- `template_plan_name`

## 4. Create The Study-Area JSON

Start from:

- `model_template_v01.json`

Create a site config such as:

- `new_site_v01.json`

Use the Ataturk example as reference:

- `ataturk_v01.json`

### Key JSON fields

- `files_root`: folder containing the source inputs
- `working_root`: folder where the generated HEC-RAS project will be created
- `project_name`: output project folder and `.prj` base name
- `projection_name`: projection file name under `files_root`
- `dtm_name`: DTM file name under `files_root`
- `perimeter_name`: perimeter shapefile name under `files_root`
- `breakline_name`: breakline shapefile name under `files_root`
- `dss_name`: DSS file name under `files_root`
- `cross_section_name`: cross-section CSV name under `files_root`; for Case A junction models this may be a two-item list of CSVs where one branch continues downstream
- `junction_bc_csv_name`: optional future junction BC CSV under `files_root` with explicit junction line points
- `landcover_name`: landcover shapefile name under `files_root`
- `existing_landcover_tif_name`: optional native HEC-RAS landcover TIFF
- `existing_landcover_hdf_name`: optional native HEC-RAS landcover HDF
- `reference_geom_name`: reference `.g01`
- `reference_geom_hdf_name`: reference `.g01.hdf`
- `terrain_layer_name`: terrain layer name in RAS Mapper
- `flow_area_name`: external study-area flow-area label
- `geometry_title`: geometry title shown in HEC-RAS
- `storage_area_name`: internal 2D area/storage area name in the geometry
- `hdf_2d_area_name`: exact 2D area group name in the geometry HDF
- `mesh_cell_size`: base cell size, usually `10.0`
- `breakline_near_spacing`: near spacing, usually `0.5`
- `breakline_near_repeats`: near repeats, usually `5`
- `breakline_far_spacing`: far spacing, currently `3.0`
- `landcover_cell_size`: rasterization size for landcover, usually `2.0`
- `landcover_nodata_manning`: Manning value for class `0`
- `region_default_manning`: default `Manning's Region 1` value for all IDs
- `boundary_offset_distance`: offset for BC lines outside the perimeter
- `downstream_bc_length_multiplier`: downstream BC length as a multiple of downstream cross-section length
- `preferred_dss_a_part`: preferred DSS A-part
- `preferred_dss_f_part`: preferred DSS F-part
- `downstream_bc_method`: currently `Normal Depth`
- `junction_bc_name`: HEC-RAS BC line name to use when a junction BC CSV is provided
- `junction_snap_tolerance`: distance tolerance for future junction snapping/validation
- `branch_connectivity_threshold`: cosine threshold for Case A downstream-continuing branch detection, usually `0.25`
- `unsteady_number`: output unsteady number, usually `01`
- `plan_number`: output plan number, usually `01`
- `template_unsteady_name`: template `.u##` path under `files_root` or an absolute path
- `template_unsteady_hdf_name`: template `.u##.hdf` path under `files_root` or an absolute path
- `template_plan_name`: template `.p##` path under `files_root` or an absolute path
- `unsteady_title`: title written to the `.u##`
- `plan_title`: title written to the `.p##`
- `plan_short_identifier`: HEC-RAS plan short ID, max 24 characters
- `plan_flow_regime`: normally `Mixed Flow` for 2D unsteady plans
- `plan_simulation_date`: manual HEC-RAS simulation date line, used only when `auto_plan_simulation_date` is `false`
- `auto_plan_simulation_date`: when true, derive the plan window from the selected upstream DSS pathname
- `simulation_start_time`: HHMM start time applied to the DSS D-part date
- `simulation_duration_hours`: simulation duration after start time
- `simulation_start_offset_hours`: optional offset applied to the derived start time
- `simulation_end_offset_hours`: optional offset applied to the derived end time
- `plan_computation_interval`: computation interval, for example `6SEC`
- `plan_hydrograph_output_interval`: hydrograph output interval, for example `5MIN`
- `plan_output_interval`: legacy alias for hydrograph output interval
- `plan_detailed_output_interval`: detailed output interval, for example `5MIN`
- `plan_instantaneous_interval`: legacy alias for detailed output interval
- `plan_mapping_interval`: mapping output interval, for example `5MIN`
- `plan_use_courant_timestep`: enables Advanced Time Step Control > Adjust Time Step Based on Courant
- `plan_use_time_series_timestep`: enables time-series based time step control when true
- `plan_max_courant`: maximum Courant value, for example `1.0`
- `plan_min_courant`: minimum Courant value, for example `0.45`
- `plan_steps_below_min_before_doubling`: steps below minimum before doubling, for example `4`
- `plan_max_doubling_base_timestep`: maximum number of doubling base time step, for example `2`
- `plan_max_halving_base_timestep`: maximum number of halving base time step, for example `2`
- `plan_residence_courant`: residence Courant value, usually `0.0`
- `plan_run_htab`: write `Run HTab=-1` when true
- `plan_run_unet`: write `Run UNet=-1` when true
- `plan_run_postprocess`: write `Run PostProcess=-1` when true
- `plan_run_rasmapper`: write `Run RASMapper=-1` when true
- `plan_num_cores`: core count written to plan solver settings
- `upstream_bc_name`: generated upstream BC line name in geometry
- `downstream_bc_name`: generated downstream BC line name in geometry
- `upstream_flow_interval`: interval line for upstream flow hydrograph
- `upstream_flow_hydrograph_slope`: flow hydrograph slope value
- `downstream_friction_slope`: optional override for normal-depth slope
- `unsteady_dss_file_relative`: optional override for DSS path inside `.u##`
- `copy_compute_results_to_project`: copy successful plan result files back to the source project for RAS Mapper review

Notes:

- leave `storage_area_name` and `hdf_2d_area_name` as `null` if autodetect works
- if autodetect fails, set both explicitly
- CLI flags override the JSON values
- with `auto_plan_simulation_date=true`, the workflow uses the selected DSS
  pathname D-part, for example `01May2025`, plus `simulation_start_time`
  and `simulation_duration_hours`
- for Case A junction models, set `cross_section_name` to a two-item list:
  `["CrossSections/main_stem.csv", "CrossSections/tributary.csv"]`
- Case A assumes one branch continues downstream past the junction; if both
  CSVs are upstream tributaries only, provide a downstream BC input instead of
  relying on automatic connectivity
- for two CSVs, the workflow attempts branch-specific DSS selection by matching
  each CSV file name/stem against DSS A-parts; if no match is found, it falls
  back to the global `preferred_dss_a_part`/`preferred_dss_f_part` path

## 5. Recommended Run Order

### Step 1: Dry-run the project shell

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json prepare --skip-terrain
```

Use this to catch bad paths and missing inputs quickly.

### Step 2: Full prepare

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json prepare
```

What `prepare` does:

- creates the project folder and `.prj`
- creates the `.rasmap`
- copies GIS, DSS, and helper inputs into the working project
- copies the perimeter shapefile into the working project
- builds the terrain HDF
- creates the landcover raster from `KodText`
- creates the Manning raster from `Manningn`
- creates landcover table artifacts
- copies or registers the native RAS landcover pair if provided
- creates helper shapefiles and reports

### Step 3: Build the working geometry

Recommended command:

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json install-geometry --timeout 420
```

What `install-geometry` does:

- clones the reference geometry into the working project
- rewrites the geometry using the new perimeter, breaklines, BCs, and Manning data
- regenerates the geometry HDF in HEC-RAS / RAS Mapper
- enforces all breaklines
- runs `Try to Fix All Meshes`
- applies `Geometries > Manage Geometry Associations > Manning's n = LandCover`
- saves the project

### Step 4: Create the unsteady flow and plan files

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json create-unsteady-plan
```

What `create-unsteady-plan` does:

- creates `<project_name>.u01`
- creates or copies `<project_name>.u01.hdf`
- creates `<project_name>.p01`
- links `Flow File=u01` and `Geom File=g01` inside the plan
- adds `Unsteady File=u01` and `Plan File=p01` to the project
- sets `Current Plan=p01`
- writes `Reports\unsteady_plan_summary.json`

### Step 5: Run the 2D unsteady plan

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json compute-plan --overwrite --timeout 1800
```

What `compute-plan` does:

- copies the project to `<results_root>\<project_name>_plan##_run`
- runs HEC-RAS with quoted project and plan filenames
- automatically accepts the first-run geometry preprocessor prompt
- adjusts the copied plan simulation window from DSS validation messages if needed
- writes `Reports\compute_summary.json`
- writes the final discovered `Simulation Date` back to the source plan
- copies successful `.p##.hdf`, compute logs, and updated geometry HDF back to the source project

### Step 6: Verify Manning values

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json check-mannings --geom g01
```

### Step 7: Open the project for review

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json open --timeout 300
```

## 6. Alternative Update Path

Use this when the project geometry already exists and you only want to rewrite it from updated inputs:

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json sync-geometry
python .\rasmapper_v01.py --config-json .\new_site_v01.json regenerate-geometry --timeout 420
python .\rasmapper_v01.py --config-json .\new_site_v01.json create-unsteady-plan
python .\rasmapper_v01.py --config-json .\new_site_v01.json compute-plan --overwrite --timeout 1800
python .\rasmapper_v01.py --config-json .\new_site_v01.json check-mannings --geom g01
```

Important:

- if you run `sync-geometry`, you must run `regenerate-geometry` afterward
- otherwise the `.g01.hdf` will not reflect the rewritten geometry

## 7. Output Locations

The project is created under:

```text
<working_root>\<project_name>\
```

With the default template, this resolves to:

```text
F:\codex_rascommander\rasmapper_v01\projects\<project_name>\
```

Main outputs:

- `<project_name>.prj`
- `<project_name>.rasmap`
- `<project_name>.g01`
- `<project_name>.g01.hdf`
- `<project_name>.u01`
- `<project_name>.u01.hdf`
- `<project_name>.p01`

Compute outputs are saved under:

```text
<results_root>\<project_name>_plan##_run\
```

For the standalone Ataturk test, this resolves to:

```text
F:\codex_rascommander\rasmapper_v01\results\Ataturk_v01_Project_plan01_run\
```

Typical saved locations inside the project:

- `Terrain\` for the terrain HDF and projection copy
- `Inputs\Perimeter\` for the copied perimeter shapefile family
- `Inputs\Breaklines\` for the copied breakline shapefile family
- `Inputs\LandCover\` for the copied landcover shapefile family
- `Boundary\` for DSS, DSS catalog, cross-section CSV, and BC helpers
- `Land Classification\` for the active map-layer landcover files
- `LandCover\` for Manning raster products
- `Helpers\` for review shapefiles such as cross-sections and centerlines
- `Reports\` for JSON and CSV audit outputs

Common folders:

- `Terrain`
- `Inputs`
- `Helpers`
- `Boundary`
- `LandCover`
- `Land Classification`
- `Reports`

## 8. Key Reports To Check

After a run, check these files:

- `Reports\prepare_summary.json`
- `Reports\geometry_install_summary.json`
- `Reports\geometry_sync_summary.json`
- `Reports\mesh_enforcement_summary.json`
- `Reports\landcover_layer_status.json`
- `Reports\compute_summary.json`
- `Reports\region_mannings_check_report.csv`
- `Boundary\boundary_candidates.csv`

## 9. What To Verify In HEC-RAS

Open the generated project in a fresh HEC-RAS session and confirm:

- the 2D mesh exists
- the breaklines are enforced
- the mesh has been fixed successfully
- `Map Layers` shows `LandCover`
- `Geometries > Manage Geometry Associations` shows `Manning's n = LandCover`
- the geometry contains final Manning values

## 10. Default Settings That Usually Work

For a normal production-style run:

- `mesh_cell_size = 10.0`
- `breakline_near_spacing = 0.5`
- `breakline_near_repeats = 5`
- `breakline_far_spacing = 3.0`
- `landcover_cell_size = 2.0`

For a faster coarse test:

- `mesh_cell_size = 20.0`

## 11. Troubleshooting

### Mesh missing after `sync-geometry`

Cause:

- `sync-geometry` only rewrites the `.g01`

Fix:

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json regenerate-geometry --timeout 420
```

### `LandCover` is missing or not usable in geometry association

Check:

- the native landcover `.tif/.hdf` pair exists
- `existing_landcover_tif_name` and `existing_landcover_hdf_name` are set correctly
- the `LandCover` layer appears in `Map Layers`

### File-copy or lock errors

Close:

- HEC-RAS
- RAS Mapper
- `PipeServer.exe`

Then rerun the command.

### Wrong DSS pathname selected

Adjust:

- `preferred_dss_a_part`
- `preferred_dss_f_part`

Then rerun `prepare`.

### Wrong 2D area name in geometry or HDF

Set these explicitly in the JSON:

- `storage_area_name`
- `hdf_2d_area_name`

### GUI automation behaves inconsistently

Use a clean state:

- close all open HEC-RAS sessions
- close all open RAS Mapper sessions
- rerun the command

## 12. Command Reference

Main commands:

```powershell
python .\rasmapper_v01.py --config-json <json> prepare
python .\rasmapper_v01.py --config-json <json> prepare --skip-terrain
python .\rasmapper_v01.py --config-json <json> install-geometry --timeout 420
python .\rasmapper_v01.py --config-json <json> sync-geometry
python .\rasmapper_v01.py --config-json <json> regenerate-geometry --timeout 420
python .\rasmapper_v01.py --config-json <json> create-unsteady-plan
python .\rasmapper_v01.py --config-json <json> compute-plan --overwrite --timeout 1800
python .\rasmapper_v01.py --config-json <json> check-mannings --geom g01
python .\rasmapper_v01.py --config-json <json> open --timeout 300
```

## 13. Practical Recommendation

For a new site, use this exact sequence first:

```powershell
python .\rasmapper_v01.py --config-json .\new_site_v01.json prepare --skip-terrain
python .\rasmapper_v01.py --config-json .\new_site_v01.json prepare
python .\rasmapper_v01.py --config-json .\new_site_v01.json install-geometry --timeout 420
python .\rasmapper_v01.py --config-json .\new_site_v01.json create-unsteady-plan
python .\rasmapper_v01.py --config-json .\new_site_v01.json compute-plan --overwrite --timeout 1800
python .\rasmapper_v01.py --config-json .\new_site_v01.json check-mannings --geom g01
python .\rasmapper_v01.py --config-json .\new_site_v01.json open --timeout 300
```

That is the cleanest path for a new study area.
