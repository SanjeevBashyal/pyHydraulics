"""
Demonstration script for the fixed HECRAS class.
This script shows how to create and run a HEC-RAS model using the HECRAS class.
"""

import os
import numpy as np
from pyhydraulics import HECRAS

def main():
    """Main function to demonstrate HECRAS class usage."""
    
    # Configuration parameters
    PROJECT_FOLDER = r"E:\0 Python\pyHydraulics\1 Data and Models/HEC-RAS/test"
    PROJECT_NAME = "demo_model"
    HECRAS_VERSION = "RAS67.HECRASController"
    
    # Geometric parameters
    RIVER_NAME = "DemoRiver"
    REACH_NAME = "MainReach"
    XS_COORDINATES = np.array([
        # Station (m), Elevation (m)
        [0, 12.0], [15, 12.0], [15, 8.0], [25, 8.0], [30, 5.0],
        [40, 5.0], [45, 8.0], [55, 8.0], [55, 12.0], [70, 12.0]
    ])
    MANNINGS_N = [0.04, 0.03, 0.04]  # LOB, Channel, ROB
    BANK_STATIONS = [15, 55]           # Left Bank, Right Bank
    DOWNSTREAM_REACH_LENGTHS = [800, 800, 800]  # LOB, Channel, ROB distances
    UPSTREAM_ELEVATION_ADJUST = 0.5    # Vertical shift for upstream XS
    
    # Flow parameters
    FLOW_RATE = 120.0                  # m³/s
    PROFILE_NAME = "Q120"
    DOWNSTREAM_SLOPE = 0.0015          # Normal depth slope
    
    print("=== HECRAS Class Demonstration ===")
    
    # Initialize HEC-RAS interface
    hecras = HECRAS(HECRAS_VERSION)
    
    try:
        # 1. Create project structure
        print("\n1. Creating project structure...")
        proj_path = hecras.create_project_structure(PROJECT_FOLDER, PROJECT_NAME)
        
        # 2. Create geometry file
        print("\n2. Creating geometry file...")
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
        print("\n3. Creating flow file...")
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
        print("\n4. Creating plan file...")
        hecras.create_plan_file(
            project_path=proj_path,
            project_name=PROJECT_NAME,
            num_interpolated_xs=7,
            downstream_reach_lengths=DOWNSTREAM_REACH_LENGTHS
        )
        
        print(f"\n✓ Model files created successfully in: {proj_path}")
        
        # 5. Optional: Connect to HEC-RAS and run simulation
        print("\n5. Connecting to HEC-RAS...")
        if hecras.connect():
            print("✓ Connected to HEC-RAS")
            
            # Open the project
            if hecras.open_project(proj_path, PROJECT_NAME):
                print("✓ Project opened successfully")
                
                # Run the simulation
                print("\n6. Running simulation...")
                success, message = hecras.run_simulation()
                
                if success:
                    print("✓ Simulation completed successfully!")
                else:
                    print(f"✗ Simulation failed: {message}")
                
                # Save the project
                hecras.save_project()
                print("✓ Project saved")
                
            else:
                print("✗ Failed to open project")
        else:
            print("✗ Failed to connect to HEC-RAS")
            print("Note: This is normal if HEC-RAS is not installed or running")
            
    except Exception as e:
        print(f"✗ An error occurred: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # Clean up
        hecras.disconnect()
        print("\n=== Demonstration Complete ===")
    
    # Show final instructions
    print(f"\n--- MANUAL STEPS ---")
    print(f"1. Open the HEC-RAS project: '{os.path.join(proj_path, PROJECT_NAME)}.prj'")
    print("2. Review the geometry, flow, and plan files")
    print("3. Run the simulation manually if needed")
    print("4. Use RAS Mapper for visualization")

if __name__ == "__main__":
    main()
