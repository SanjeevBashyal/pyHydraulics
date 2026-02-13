"""
HEC-RAS Automation Script using pyHydraulics Package

This script demonstrates how to use the HECRAS class to create and run HEC-RAS models.
It creates a simple canal model with two cross-sections and runs a steady flow simulation.
"""

import os
import numpy as np
from pyhydraulics import HECRAS

# --- CONFIGURATION PARAMETERS ---
# Students can modify these values to change the model

# 1. Project Setup
PROJECT_FOLDER = r"E:\0 Python\pyHydraulics\1 Data and Models/HEC-RAS/test"  # Base folder for the project
PROJECT_NAME = "tutorial"      # Name of the project
HECRAS_VERSION = "RAS67.HECRASController" # HEC-RAS COM identifier

# 2. Geometric Parameters
RIVER_NAME = "Canal"
REACH_NAME = "Reach_1"
XS_COORDINATES = np.array([
    # Station (m), Elevation (m)
    [0, 10.0], [20, 10.0], [20, 5.0], [30, 5.0], [35, 2.0],
    [45, 2.0], [50, 5.0], [60, 5.0], [60, 10.0], [80, 10.0]
])
MANNINGS_N = [0.05, 0.03, 0.05]  # LOB, Channel, ROB
BANK_STATIONS = [30, 50]         # Left Bank, Right Bank
DOWNSTREAM_REACH_LENGTHS = [1000, 1000, 1000] # LOB, Channel, ROB distance to next XS
UPSTREAM_ELEVATION_ADJUST = 1.0  # Vertical shift for the upstream XS (creates slope)
NUM_INTERPOLATED_XS = 9          # Number of cross-sections to interpolate between the two main ones

# 3. Flow & Boundary Conditions
FLOW_RATE = 150.0                # Flood discharge in m^3/s
PROFILE_NAME = "PF1"
DOWNSTREAM_SLOPE = 0.001         # Normal Depth friction slope

def main():
    """Main function to create and run the HEC-RAS model."""
    
    # Initialize HEC-RAS interface
    hecras = HECRAS(HECRAS_VERSION)
    
    try:
        # 1. Create project structure
        print("=== Creating Project Structure ===")
        proj_path = hecras.create_project_structure(PROJECT_FOLDER, PROJECT_NAME)
        
        # 2. Create geometry file
        print("\n=== Creating Geometry File ===")
        hecras.create_geometry_file_text(
            project_path=proj_path,
            project_name=PROJECT_NAME,
            river_name=RIVER_NAME,
            reach_name=REACH_NAME,
            xs_coordinates=XS_COORDINATES,
            mannings_n=MANNINGS_N,
            bank_stations=BANK_STATIONS,
            downstream_reach_lengths=DOWNSTREAM_REACH_LENGTHS,
            upstream_elevation_adjust=UPSTREAM_ELEVATION_ADJUST
        )
        
        # 3. Create flow file
        print("\n=== Creating Flow File ===")
        hecras.create_flow_file_text(
            project_path=proj_path,
            project_name=PROJECT_NAME,
            river_name=RIVER_NAME,
            reach_name=REACH_NAME,
            flow_rate=FLOW_RATE,
            profile_name=PROFILE_NAME,
            downstream_slope=DOWNSTREAM_SLOPE
        )
        
        # 4. Create plan file
        print("\n=== Creating Plan File ===")
        hecras.create_plan_file(
            project_path=proj_path,
            project_name=PROJECT_NAME,
            num_interpolated_xs=NUM_INTERPOLATED_XS,
            downstream_reach_lengths=DOWNSTREAM_REACH_LENGTHS
        )

        
        # return
        
        # 5. Connect to HEC-RAS and run simulation
        print("\n=== Running HEC-RAS Simulation ===")
        if hecras.connect():
            # Open the project
            if hecras.open_project(proj_path, PROJECT_NAME):
                # Run the simulation
                hecras.show_window()
                # return
                success, message = hecras.run_simulation()
                
                if success:
                    print("✓ Simulation completed successfully!")
                else:
                    print(f"✗ Simulation failed: {message}")
                
                # Save the project
                hecras.save_project()
            else:
                print("✗ Failed to open project")
        else:
            print("✗ Failed to connect to HEC-RAS")
            
    except Exception as e:
        print(f"An error occurred: {e}")
        
    finally:
        # Clean up
        # hecras.disconnect()
        print("\n=== HEC-RAS Process Finished ===")
    
    # 6. Final instructions for manual steps in RAS Mapper
    print("\n\n--- AUTOMATION COMPLETE ---")
    print("The HEC-RAS model has been built and the simulation has been run.")
    print("The final step is to visualize the results in RAS Mapper.")
    print("\n--- MANUAL STEPS FOR HAZARD MAPPING ---")
    print(f"1. Open the HEC-RAS project: '{os.path.join(proj_path, PROJECT_NAME)}.prj'")
    print("2. Click the 'RAS Mapper' button.")
    print("3. In RAS Mapper, right-click on 'Terrains' -> 'Create a New RAS Terrain'.")
    print("   - Use the geometry file as the source to create a terrain from your cross-sections.")
    print("4. Expand 'Results' -> 'Plan01' in the left panel.")
    print("5. Right-click on the result -> 'Create New Results Map Layer'.")
    print("6. Create three maps: 'Depth', 'Velocity', and 'Depth * Velocity'.")
    print("7. For the 'Depth * Velocity' map, right-click it -> 'Layer Properties' -> 'Symbology'.")
    print("8. Set up user-defined ranges for hazard classification (e.g., <0.5, 0.5-1.5, >1.5).")
    print("   - Assign colors (e.g., Blue, Yellow, Red) to represent low, medium, and high hazard.")
    print("--------------------------------------------------")

def create_simple_example():
    """Create a simple example using the basic geometry file creator."""
    print("=== Creating Simple Example ===")
    
    hecras = HECRAS(HECRAS_VERSION)
    
    try:
        # Create a simple geometry file
        simple_geo_path = hecras.create_simple_geometry_file(
            base_path=PROJECT_FOLDER,
            file_path="simple_example.g01",
            river_name="SimpleRiver",
            reach_name="MainReach",
            rs="100.0",
            sta_elev=[(0, 15), (10, 12), (20, 8), (30, 12), (40, 15)],
            mann_values=[(0, 0.04, 0), (10, 0.03, 0), (40, 0.04, 0)],
            bank_sta=(10, 30),
            reach_lengths=(50, 100, 50)
        )
        print(f"Simple geometry file created at: {simple_geo_path}")
        
    except Exception as e:
        print(f"Error creating simple example: {e}")
    finally:
        hecras.disconnect()

if __name__ == "__main__":
    # Run the main simulation
    main()
    
    # Optionally create a simple example
    # create_simple_example()