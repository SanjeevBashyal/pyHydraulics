Use a study-area JSON to run the same workflow for different sites.

For the full step-by-step runbook for a new site, see:

- `working/study_areas/NEW_STUDY_AREA_RUNBOOK.md`

Example:

```powershell
python working\rasmapper.py --config-json working\study_areas\ataturk.json prepare
python working\rasmapper.py --config-json working\study_areas\ataturk.json install-geometry --timeout 420
```

If you use `sync-geometry` instead of `install-geometry`, run
`regenerate-geometry` afterward so HEC-RAS rebuilds `g01.hdf`:

```powershell
python working\rasmapper.py --config-json working\study_areas\ataturk.json sync-geometry
python working\rasmapper.py --config-json working\study_areas\ataturk.json regenerate-geometry --timeout 420
python working\rasmapper.py --config-json working\study_areas\ataturk.json check-mannings --geom g01
```

Notes:

- CLI flags override the JSON file.
- `prepare` does not require a reference geometry.
- `install-geometry` and `sync-geometry` do require `reference_geom_name`
  and `reference_geom_hdf_name`.
- If you already have a native HEC-RAS landcover pair, set
  `existing_landcover_tif_name` and `existing_landcover_hdf_name`.
- If the geometry/HDF 2D area name differs from the source perimeter stem,
  set `storage_area_name` and `hdf_2d_area_name`. Otherwise the script will
  try to detect them automatically from the geometry and HDF.
