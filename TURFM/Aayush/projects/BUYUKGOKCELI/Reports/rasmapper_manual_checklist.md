        # RAS Mapper Manual Checklist

        Project: `BUYUKGOKCELI`

        This script prepared the project shell, terrain, helper GIS layers,
        cross-section analysis, DSS catalog report, landcover products, and
        Manning lookup tables. The remaining items below still require the
        RAS Mapper GUI because ras_commander does not expose a stable file
        API for those dialogs.

        ## Inputs prepared by script

        - Projection: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Terrain\TUREF_CM30_projection.prj`
        - Terrain source DTM: `E:\0_Python\pyHydraulics\TURFM\Aayush\BUYUKGOKCELI_inputs\Terrain\SET4_39_DTM.tif`
        - Terrain HDF target: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Terrain\SET4_39_DTM.hdf`
        - Perimeter source: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Inputs\Perimeter\Perimeter_BUYUKGOKCELI.shp`
        - Breakline source: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Inputs\Breaklines\BUR-ISP-MER-BUYUKGOKCELI_SEV_USTU_V1.shp`
        - Landcover source polygon: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Inputs\LandCover\LC_BUYUKGOKCELI.shp`
        - Landcover 2 m raster from `KodText`: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.tif`
        - Generated Manning-table HDF: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.hdf`
        - Existing native RAS landcover TIFF: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.tif`
- Existing native RAS landcover HDF: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.hdf`
        - Final Manning raster at 2 m: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\LandCover\LC_BUYUKGOKCELI_ManningN_2m.tif`
        - Manning lookup from `Manningn`: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\LandCover\landcover_lookup.csv`
        - Cross-section helper lines: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Helpers\cross_sections.shp`
        - Boundary helper lines: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Helpers\boundary_candidates.shp`
        - DSS catalog report: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Boundary\dss_catalog.csv`
        - Boundary recommendations: `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Boundary\boundary_candidates.csv`

        ## RAS Mapper steps

        1. Open project `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\BUYUKGOKCELI.prj` and open RAS Mapper.
        2. Create a new geometry for 2D and name the 2D flow area
           `BUYUKGOKCELI_2D`.
        3. Verify the project projection is `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Terrain\TUREF_CM30_projection.prj`.
        4. Confirm the terrain layer `BUYUKGOKCELI_Terrain` is present.
        5. Create or import the 2D perimeter from `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Inputs\Perimeter\Perimeter_BUYUKGOKCELI.shp`.
        6. Generate computation points with cell size
           `10.0 m x 10.0 m`.
        7. Import the breaklines from `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Inputs\Breaklines\BUR-ISP-MER-BUYUKGOKCELI_SEV_USTU_V1.shp`.
        8. Edit breakline properties:
           near spacing = `0.5 m`,
           near repeats = `5`.
        9. Enforce all breaklines.
        10. Run the mesh error repair / fix-all workflow in the perimeter editor.
        11. Add upstream and downstream boundary conditions using the helper
            lines in `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Helpers\boundary_candidates.shp`.
            The helper lines are already offset `1.0 m`
            outside the perimeter.
        12. Upstream recommended DSS pathname:
            `/BUR-ISP-MER-BUYUKGOKCELI/BURDUR/DEBI/01May2025/30Minute/Q1000/`
        13. Downstream recommended method:
            `Normal Depth` with slope `0.028262`.
        14. Review boundary line orientation. The helper CSV reports whether the
            2D area falls on the line's left or right side and includes reverse
            flags for either interior-right or interior-left conventions.
        15. Confirm the `LandCover` map layer points to
    `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.hdf` and uses
    `E:\0_Python\pyHydraulics\TURFM\Aayush\projects\BUYUKGOKCELI\Land Classification\LandCover.tif`.
        16. Confirm the geometry `Manning's n` / `Final n Value` layers render.
        17. If you edit Manning values in the GUI, re-run:
            `python working/rasmapper_v01.py apply-mannings`
        18. Then run:
            `python working/rasmapper_v01.py check-mannings`

        ## Notes

        - The helper files are review artifacts, not hidden state.
        - Re-running `prepare` updates helper files without deleting your
          existing project geometry or plan files.
