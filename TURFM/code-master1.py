import os
import numpy as np

PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

PROJ_PATH = os.path.join(PROJECT_FOLDER, '0 Proj')
BUR_BUR_PATH = os.path.join(PROJECT_FOLDER, '1 Bur-Bur')
HYDRO_PATH = os.path.join(PROJECT_FOLDER, '2 Hydrology')
HEC_PATH = os.path.join(PROJECT_FOLDER, '3 Hecras')
GIS_PATH = os.path.join(PROJECT_FOLDER, '4 GIS')
OUTPUT_PATH = os.path.join(PROJECT_FOLDER,'5 Outputs')

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')
CROSS_SECTION_PATH = os.path.join(BUR_BUR_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME,'KESIT_TESLIM')
CROSS_SECTION_FILE_PATH = os.path.join(CROSS_SECTION_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_KESIT_TESLIM.csv')
BANK_LINE_PATH = os.path.join(BUR_BUR_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME,'SEV_USTU')
BANK_LINE_FILE_PATH = os.path.join(BANK_LINE_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_SEV_USTU.shp')

if not os.path.exists(PROJECT_FOLDER):
    os.makedirs(PROJECT_FOLDER)
    os.makedirs(PROJ_PATH)
    os.makedirs(BUR_BUR_PATH)
    os.makedirs(HYDRO_PATH)
    os.makedirs(HEC_PATH)
    os.makedirs(GIS_PATH)
    os.makedirs(OUTPUT_PATH)
    print(f"Project folder created at: {PROJECT_FOLDER} and now add BUR-BUR Data")

HECRAS_VERSION = "RAS67.HECRASController" # HEC-RAS COM identifier

HEC_PROJECT_NAME = PROJECT_SHORT_NAME
# HEC_GEOMETRY_NAME = 
