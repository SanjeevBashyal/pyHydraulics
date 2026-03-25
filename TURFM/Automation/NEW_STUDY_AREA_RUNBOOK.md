# New Study Area Runbook

This runbook explains how to use `working/rasmapper.py` for a new study area.

It reflects the current workflow that was validated against the Ataturk case:

- prepare the HEC-RAS project shell
- build terrain and landcover products
- clone and rewrite a reference 2D geometry
- regenerate the mesh in HEC-RAS / RAS Mapper
- enforce breaklines
- fix mesh issues
- associate `LandCover` to `Manning's n` through `Manage Geometry Associations`
- verify final Manning values

## 1. Prerequisites

- Python environment for this repo is installed
- HEC-RAS 6.6 is installed
- GDAL tools bundled with HEC-RAS are available
- `working/rasmapper.py` exists and runs

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

## 4. Create The Study-Area JSON

Start from:

- `working/study_areas/template.json`

Create a site config such as:

- `working/study_areas/new_site.json`

Use the Ataturk example as reference:

- `working/study_areas/ataturk.json`

### Key JSON fields

- `files_root`: folder containing the source inputs
- `working_root`: folder where the generated HEC-RAS project will be created
- `project_name`: output project folder and `.prj` base name
- `projection_name`: projection file name under `files_root`
- `dtm_name`: DTM file name under `files_root`
- `perimeter_name`: perimeter shapefile name under `files_root`
- `breakline_name`: breakline shapefile name under `files_root`
- `dss_name`: DSS file name under `files_root`
- `cross_section_name`: cross-section CSV name under `files_root`
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
- `boundary_offset_distance`: offset for BC lines outside the perimeter
- `preferred_dss_a_part`: preferred DSS A-part
- `preferred_dss_f_part`: preferred DSS F-part
- `downstream_bc_method`: currently `Normal Depth`

Notes:

- leave `storage_area_name` and `hdf_2d_area_name` as `null` if autodetect works
- if autodetect fails, set both explicitly
- CLI flags override the JSON values

## 5. Recommended Run Order

### Step 1: Dry-run the project shell

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json prepare --skip-terrain
```

Use this to catch bad paths and missing inputs quickly.

### Step 2: Full prepare

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json prepare
```

What `prepare` does:

- creates the project folder and `.prj`
- creates the `.rasmap`
- copies GIS, DSS, and helper inputs into the working project
- builds the terrain HDF
- creates the landcover raster from `KodText`
- creates the Manning raster from `Manningn`
- creates landcover table artifacts
- copies or registers the native RAS landcover pair if provided
- creates helper shapefiles and reports

### Step 3: Build the working geometry

Recommended command:

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json install-geometry --timeout 420
```

What `install-geometry` does:

- clones the reference geometry into the working project
- rewrites the geometry using the new perimeter, breaklines, BCs, and Manning data
- regenerates the geometry HDF in HEC-RAS / RAS Mapper
- enforces all breaklines
- runs `Try to Fix All Meshes`
- applies `Geometries > Manage Geometry Associations > Manning's n = LandCover`
- saves the project

### Step 4: Verify Manning values

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json check-mannings --geom g01
```

### Step 5: Open the project for review

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json open --timeout 300
```

## 6. Alternative Update Path

Use this when the project geometry already exists and you only want to rewrite it from updated inputs:

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json sync-geometry
python working\rasmapper.py --config-json working\study_areas\new_site.json regenerate-geometry --timeout 420
python working\rasmapper.py --config-json working\study_areas\new_site.json check-mannings --geom g01
```

Important:

- if you run `sync-geometry`, you must run `regenerate-geometry` afterward
- otherwise the `.g01.hdf` will not reflect the rewritten geometry

## 7. Output Locations

The project is created under:

```text
<working_root>\<project_name>\
```

Main outputs:

- `<project_name>.prj`
- `<project_name>.rasmap`
- `<project_name>.g01`
- `<project_name>.g01.hdf`

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
python working\rasmapper.py --config-json working\study_areas\new_site.json regenerate-geometry --timeout 420
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
python working\rasmapper.py --config-json <json> prepare
python working\rasmapper.py --config-json <json> prepare --skip-terrain
python working\rasmapper.py --config-json <json> install-geometry --timeout 420
python working\rasmapper.py --config-json <json> sync-geometry
python working\rasmapper.py --config-json <json> regenerate-geometry --timeout 420
python working\rasmapper.py --config-json <json> check-mannings --geom g01
python working\rasmapper.py --config-json <json> open --timeout 300
```

## 13. Practical Recommendation

For a new site, use this exact sequence first:

```powershell
python working\rasmapper.py --config-json working\study_areas\new_site.json prepare --skip-terrain
python working\rasmapper.py --config-json working\study_areas\new_site.json prepare
python working\rasmapper.py --config-json working\study_areas\new_site.json install-geometry --timeout 420
python working\rasmapper.py --config-json working\study_areas\new_site.json check-mannings --geom g01
python working\rasmapper.py --config-json working\study_areas\new_site.json open --timeout 300
```

That is the cleanest path for a new study area.
