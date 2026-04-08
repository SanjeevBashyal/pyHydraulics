import os
from dataclasses import dataclass, field

@dataclass
class Config:
    """
    Configuration class containing all necessary path settings and variables
    for the DTM processing workflow.
    """
    PROJECT_FOLDER: str = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"
    PROJECT_SHORT_NAME: str = "ATATURK-T"
    
    # Directories and files - generated dynamically
    PROJECT_LONG_NAME: str = field(init=False)
    PROJ_PATH: str = field(init=False)
    BUR_BUR_PATH: str = field(init=False)
    HYDRO_PATH: str = field(init=False)
    HEC_PATH: str = field(init=False)
    GIS_PATH: str = field(init=False)
    OUTPUT_PATH: str = field(init=False)
    
    DEM_PATH: str = field(init=False)
    CROSS_SECTION_PATH: str = field(init=False)
    CROSS_SECTION_FILE_PATH: str = field(init=False)
    BANK_LINE_PATH: str = field(init=False)
    BANK_LINE_FILE_PATH: str = field(init=False)
    
    # Processing Config
    BLEND_TYPE: str = 'linear'
    HECRAS_VERSION: str = "6.7"
    RAS_EXE_PATH: str = r"C:\Program Files (x86)\HEC\HEC-RAS\6.7 Beta 4\Ras.exe"
    
    def __post_init__(self):
        self.PROJECT_LONG_NAME = f"BUR-BUR-MER-{self.PROJECT_SHORT_NAME}"
        
        self.PROJ_PATH = os.path.join(self.PROJECT_FOLDER, '0 Proj')
        self.BUR_BUR_PATH = os.path.join(self.PROJECT_FOLDER, '1 Bur-Bur')
        self.HYDRO_PATH = os.path.join(self.PROJECT_FOLDER, '2 Hydrology')
        self.HEC_PATH = os.path.join(self.PROJECT_FOLDER, '3 Hecras')
        self.GIS_PATH = os.path.join(self.PROJECT_FOLDER, '4 GIS')
        self.OUTPUT_PATH = os.path.join(self.PROJECT_FOLDER, '5 Outputs')
        
        self.DEM_PATH = os.path.join(self.PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')
        self.CROSS_SECTION_PATH = os.path.join(self.BUR_BUR_PATH, self.PROJECT_LONG_NAME, 'KESIT_TESLIM')
        self.CROSS_SECTION_FILE_PATH = os.path.join(self.CROSS_SECTION_PATH, f"{self.PROJECT_LONG_NAME}_KESIT_TESLIM.csv")
        
        self.BANK_LINE_PATH = os.path.join(self.BUR_BUR_PATH, self.PROJECT_LONG_NAME, 'SEV_USTU')
        self.BANK_LINE_FILE_PATH = os.path.join(self.BANK_LINE_PATH, f"{self.PROJECT_LONG_NAME}_SEV_USTU.shp")
        
    def set_project_folder(self, folder_path: str, short_name: str = None):
        """Updates the project folder dynamically and optionally renames the short name."""
        self.PROJECT_FOLDER = folder_path
        if short_name is not None:
            self.PROJECT_SHORT_NAME = short_name
        # Re-initialize paths
        self.__post_init__()

    def setup_directories(self):
        """Creates the necessary project folders if they don't already exist."""
        directories = [
            self.PROJECT_FOLDER, self.PROJ_PATH, self.BUR_BUR_PATH, 
            self.HYDRO_PATH, self.HEC_PATH, self.GIS_PATH, self.OUTPUT_PATH
        ]
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            print(f"Ensured directory exists: {directory}")
