from configProject import Config
from Automation.callDTM import DTM


PROJECTS_TO_RUN = None
TARGET_RES = 0.1
BUFFER_M = 20.0
JUNCTION_TOLERANCE = 50.0
PERIMETER_OFFSET_M = 500.0
WRITE_INTERMEDIATE_TIFS = True


if __name__ == "__main__":
    DTM(Config()).process_structure_projects(
        projects=PROJECTS_TO_RUN,
        target_res=TARGET_RES,
        buffer_m=BUFFER_M,
        junction_tolerance=JUNCTION_TOLERANCE,
        perimeter_offset_m=PERIMETER_OFFSET_M,
        write_intermediate=WRITE_INTERMEDIATE_TIFS,
    )
