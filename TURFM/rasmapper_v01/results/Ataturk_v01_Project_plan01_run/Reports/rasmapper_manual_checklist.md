        # RAS Mapper Manual Checklist

        Project: `Ataturk_v01_Project`

        This script prepared the project shell, terrain, helper GIS layers,
        cross-section analysis, DSS catalog report, landcover products, and
        Manning lookup tables. The remaining items below still require the
        RAS Mapper GUI because ras_commander does not expose a stable file
        API for those dialogs.

        ## Inputs prepared by script

        - Projection: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Terrain\TUREF_CM30_projection.prj`
        - Terrain source DTM: `F:\codex_rascommander\rasmapper_v01\inputs\Terrain\BUR-BUR-MER-ATATURK-T_channel.tif`
        - Terrain HDF target: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Terrain\BUR-BUR-MER-ATATURK-T_channel.hdf`
        - Perimeter source: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Inputs\Perimeter\Ataturk.shp`
        - Breakline source: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Inputs\Breaklines\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp`
        - Landcover source polygon: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Inputs\LandCover\Ataturk_LC.shp`
        - Landcover 2 m raster from `KodText`: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LandCover.tif`
        - Generated Manning-table HDF: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LandCoverTable.hdf`
        - Existing native RAS landcover TIFF: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LC_At_clip_V1.tif`
- Existing native RAS landcover HDF: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LC_At_clip_V1.hdf`
        - Final Manning raster at 2 m: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\LandCover\Ataturk_LC_ManningN_2m.tif`
        - Manning lookup from `Manningn`: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\LandCover\landcover_lookup.csv`
        - Cross-section helper lines: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Helpers\cross_sections.shp`
        - Boundary helper lines: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Helpers\boundary_candidates.shp`
        - DSS catalog report: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Boundary\dss_catalog.csv`
        - Boundary recommendations: `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Boundary\boundary_candidates.csv`

        ## RAS Mapper steps

        1. Open project `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Ataturk_v01_Project.prj` and open RAS Mapper.
        2. Create a new geometry for 2D and name the 2D flow area
           `Ataturk_2D`.
        3. Verify the project projection is `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Terrain\TUREF_CM30_projection.prj`.
        4. Confirm the terrain layer `Terrain` is present.
        5. Create or import the 2D perimeter from `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Inputs\Perimeter\Ataturk.shp`.
        6. Generate computation points with cell size
           `10.0 m x 10.0 m`.
        7. Import the breaklines from `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Inputs\Breaklines\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp`.
        8. Edit breakline properties:
           near spacing = `0.5 m`,
           near repeats = `5`.
        9. Enforce all breaklines.
        10. Run the mesh error repair / fix-all workflow in the perimeter editor.
        11. Add upstream and downstream boundary conditions using the helper
            lines in `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Helpers\boundary_candidates.shp`.
            The helper lines are already offset `1.0 m`
            outside the perimeter.
        12. Upstream recommended DSS pathname:
            `/BUR-BUR-MER-ATATURK-T/BURDUR2/DEBI/01May2025/30Minute/Q100/`
        13. Downstream recommended method:
            `Normal Depth` with slope `0.197895`.
        14. Review boundary line orientation. The helper CSV reports whether the
            2D area falls on the line's left or right side and includes reverse
            flags for either interior-right or interior-left conventions.
        15. Confirm the `LandCover` map layer points to
    `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LC_At_clip_V1.hdf` and uses
    `F:\codex_rascommander\rasmapper_v01\projects\Ataturk_v01_Project\Land Classification\LC_At_clip_V1.tif`.
        16. Confirm the geometry `Manning's n` / `Final n Value` layers render.
        17. If you edit Manning values in the GUI, re-run:
            `python working/rasmapper_v01.py apply-mannings`
        18. Then run:
            `python working/rasmapper_v01.py check-mannings`

        ## Notes

        - The helper files are review artifacts, not hidden state.
        - Re-running `prepare` updates helper files without deleting your
          existing project geometry or plan files.
