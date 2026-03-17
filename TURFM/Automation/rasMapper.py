class RASMapper:
    """
    A Python class for interfacing with HEC-RAS Mapper through .NET automation.
    Handles projections, terrains, and spatial data.
    """

    def __init__(self, hecras_install_path: str = r"C:\Program Files\HEC\HEC-RAS\6.4"):
        self.hecras_install_path = hecras_install_path
        self.ras_mapper = None
        self.RASMapperLib = None
        self.project_path = None
        self.prj_file = None

    def open_project(self, prj_file_path: str) -> bool:
        """Initialize RAS Mapper and open a specific project."""
        if not PYTHONNET_AVAILABLE:
            print("Cannot initialize RAS Mapper: pythonnet is missing.")
            return False

        if not os.path.exists(prj_file_path):
            print(f"Project file not found: {prj_file_path}")
            return False

        try:
            # 1. Load the DLLs
            if self.hecras_install_path not in sys.path:
                sys.path.append(self.hecras_install_path)

            clr.AddReference("RASMapperLib")
            import RASMapperLib

            self.RASMapperLib = RASMapperLib

            # 2. Instantiate and open project
            self.ras_mapper = RASMapperLib.RASMapper()
            self.ras_mapper.OpenProject(prj_file_path)

            # 3. Store paths for later use (like creating Terrain folders)
            self.prj_file = prj_file_path
            self.project_path = os.path.dirname(prj_file_path)

            print(f"Project successfully opened in RAS Mapper: {prj_file_path}")
            return True

        except Exception as e:
            print(f"Failed to initialize RAS Mapper: {e}")
            return False

    def set_projection(self, prj_projection_file: str) -> bool:
        """Assign a projection file to the RAS Mapper project."""
        if not self.ras_mapper:
            print("RAS Mapper not initialized. Call open_project() first.")
            return False

        if not os.path.exists(prj_projection_file):
            print(f"Projection file not found: {prj_projection_file}")
            return False

        try:
            self.ras_mapper.Project.ProjectionFilePath = prj_projection_file
            self.ras_mapper.SaveProject()
            print(f"Projection successfully set to: {prj_projection_file}")
            return True
        except Exception as e:
            print(f"Failed to set projection: {e}")
            return False

    def create_terrain(self, terrain_name: str, tif_path: str) -> bool:
        """Import a DEM (.tif) and compile it into a HEC-RAS Terrain (.hdf)."""
        if not self.ras_mapper or not self.RASMapperLib:
            print("RAS Mapper not initialized. Call open_project() first.")
            return False

        if not os.path.exists(tif_path):
            print(f"DEM file not found: {tif_path}")
            return False

        try:
            print(f"Starting terrain creation for '{terrain_name}' using {tif_path}...")

            importer = self.RASMapperLib.Utilities.TerrainImporter()
            importer.TerrainName = terrain_name

            # Create Terrain folder inside the project directory
            terrain_folder = os.path.join(self.project_path, "Terrain")
            if not os.path.exists(terrain_folder):
                os.makedirs(terrain_folder)

            output_hdf_path = os.path.join(terrain_folder, f"{terrain_name}.hdf")
            importer.Filename = output_hdf_path
            importer.InputFiles.Add(tif_path)

            print("Compiling Terrain (this may take a few moments)...")
            success = importer.Compute()

            if success:
                terrain_layer = self.RASMapperLib.TerrainLayer(
                    terrain_name, output_hdf_path
                )
                self.ras_mapper.MapManager.Terrains.Add(terrain_layer)
                self.ras_mapper.SaveProject()
                print(
                    f"Terrain '{terrain_name}' successfully created and added to Mapper."
                )
                return True
            else:
                print("Terrain Importer failed to compute.")
                return False

        except Exception as e:
            print(f"Exception occurred while creating terrain: {e}")
            return False

    def close(self):
        """Close the RAS Mapper instance."""
        if self.ras_mapper:
            try:
                self.ras_mapper.CloseProject()
                self.ras_mapper = None
                print("RAS Mapper closed.")
            except Exception as e:
                print(f"Error closing RAS Mapper: {e}")
