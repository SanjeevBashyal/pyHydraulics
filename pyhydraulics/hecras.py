"""
HEC-RAS Python Interface Module

This module provides a Python interface to HEC-RAS through COM automation.
It includes methods for creating HEC-RAS projects, geometry, flow data, and running simulations.
"""

import os
import win32com.client
import numpy as np
import textwrap
import time
from typing import List, Tuple, Optional, Union


class HECRAS:
    """
    A Python class for interfacing with HEC-RAS through COM automation.
    
    This class provides methods to:
    - Create and manage HEC-RAS projects
    - Define geometry (cross-sections, reaches, rivers)
    - Set up flow data and boundary conditions
    - Run simulations and retrieve results
    - Handle file I/O for HEC-RAS project files
    """
    
    def __init__(self, hecras_version: str = "RAS67.HECRASController"):
        """
        Initialize the HECRAS interface.
        
        Args:
            hecras_version (str): HEC-RAS COM identifier (e.g., "RAS67.HECRASController")
        """
        self.hecras_version = hecras_version
        self.hec = None
        self.project_path = None
        self.project_name = None
        
    def connect(self) -> bool:
        """
        Establish connection to HEC-RAS through COM.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            self.hec = win32com.client.Dispatch(self.hecras_version)
            print("Successfully connected to HEC-RAS")
            return True
        except Exception as e:
            print(f"Failed to connect to HEC-RAS: {e}")
            return False
    
    def disconnect(self):
        """Close the HEC-RAS connection."""
        if self.hec:
            try:
                self.hec.Project_Save()
                self.hec.QuitRas()
                print("HEC-RAS connection closed")
            except Exception as e:
                print(f"Error while closing HEC-RAS: {e}")
            finally:
                self.hec = None
    
    def create_project_structure(self, base_path: str, name: str) -> str:
        """
        Create the project folder and the main .prj file.
        
        Args:
            base_path (str): Base directory for the project
            name (str): Project name
            
        Returns:
            str: Path to the created project directory
        """
        project_path = os.path.join(base_path, name)
        if not os.path.exists(project_path):
            os.makedirs(project_path)
        print(f"Project directory created at: {project_path}")
        
        prj_content = textwrap.dedent(f"""\
            Proj Title={name}
            Current Plan=p01
            Default Exp/Contr=0.3,0.1
            SI Units
            Geom File=g01
            Flow File=f01
            Plan File=p01
            Y Axis Title=Elevation
            X Axis Title(PF)=Main Channel Distance
            X Axis Title(XS)=Station
            BEGIN DESCRIPTION:

            END DESCRIPTION:
            DSS Start Date=
            DSS Start Time=
            DSS End Date=
            DSS End Time=
            DSS Export Filename=
            DSS Export Rating Curves= 0 
            DSS Export Rating Curve Sorted= 0 
            DSS Export Volume Flow Curves= 0 
            DXF Filename=
            DXF OffsetX= 0 
            DXF OffsetY= 0 
            DXF ScaleX= 1 
            DXF ScaleY= 10 
            GIS Export Profiles= 0 
            """)
        
        with open(os.path.join(project_path, f"{name}.prj"), "w") as f:
            f.write(prj_content)
        
        self.project_path = project_path
        self.project_name = name
        return project_path
    
    def create_geometry_file_text(self, project_path: str, project_name: str, 
                                 river_name: str, reach_name: str,
                                 xs_coordinates: np.ndarray,
                                 mannings_n: List[float],
                                 bank_stations: List[float],
                                 downstream_reach_lengths: List[float],
                                 upstream_elevation_adjust: float = 1.0) -> str:
        """
        Generate the HEC-RAS ASCII text geometry file (.g01).
        
        Args:
            project_path (str): Path to the project directory
            project_name (str): Name of the project
            river_name (str): Name of the river
            reach_name (str): Name of the reach
            xs_coordinates (np.ndarray): Array of [station, elevation] coordinates
            mannings_n (List[float]): Manning's n values for [LOB, Channel, ROB]
            bank_stations (List[float]): Left and right bank stations
            downstream_reach_lengths (List[float]): LOB, Channel, ROB distances to next XS
            upstream_elevation_adjust (float): Vertical shift for upstream XS
            
        Returns:
            str: Path to the created geometry file
        """
        geo_filename = os.path.join(project_path, f"{project_name}.g01")
        print("Creating ASCII geometry file...")

        # --- Header Information ---
        geo_content = f"Geom Title=Base Geometry\n"
        geo_content += f"Program Version=6.3\n"
        geo_content += f"Viewing Rectangle= 0.0 , 1.0 , 1.0 , 0.0 \n\n"
        
        # --- River Reach Definition ---
        geo_content += f"River Reach={river_name},{reach_name}\n"
        geo_content += f"Reach XY= 3\n"
        geo_content += f"           0.0       1000.0           0.0        500.0           0.0          0.0\n"
        geo_content += f"Rch Text X Y=0.5,0.5\n"
        geo_content += f"Reverse River Text= 0 \n\n"

        # --- Cross Section Data ---
        # Define Upstream Cross Section (RS 2000)
        rs_us = 2000.0
        coords_us = xs_coordinates.copy()
        coords_us[:, 1] += upstream_elevation_adjust
        
        geo_content += f"Type RM Length L Ch R = 1 ,{rs_us:8.1f}     ,{downstream_reach_lengths[0]},{downstream_reach_lengths[1]},{downstream_reach_lengths[2]}\n"
        geo_content += f"BEGIN DESCRIPTION:\n"
        geo_content += f"Upstream Cross Section\n"
        geo_content += f"END DESCRIPTION:\n"
        geo_content += f"#Sta/Elev= {len(coords_us)}\n"
        
        # Format coordinates with proper spacing
        for i, (sta, elev) in enumerate(coords_us):
            geo_content += f"{sta:8.0f}{elev:8.2f}"
            if (i + 1) % 5 == 0: # 5 pairs per line
                geo_content += "\n"
        if len(coords_us) % 5 != 0:
            geo_content += "\n"
        
        geo_content += f"#Mann= 3 , 0 , 0 \n"
        geo_content += f"{bank_stations[0]:8.0f}{mannings_n[0]:8.2f}       0{bank_stations[1]:8.0f}{mannings_n[1]:8.2f}       0{coords_us[-1,0]:8.0f}{mannings_n[2]:8.2f}       0\n"
        geo_content += f"Bank Sta={bank_stations[0]:.0f},{bank_stations[1]:.0f}\n"
        geo_content += f"XS Rating Curve= 0 ,0\n"
        geo_content += f"Exp/Cntr=0.3,0.1\n\n"

        # Define Downstream Cross Section (RS 1000)
        rs_ds = 1000.0
        coords_ds = xs_coordinates.copy()
        
        geo_content += f"Type RM Length L Ch R = 1 ,{rs_ds:8.1f}     ,     0,     0,     0\n"
        geo_content += f"BEGIN DESCRIPTION:\n"
        geo_content += f"Downstream Cross Section\n"
        geo_content += f"END DESCRIPTION:\n"
        geo_content += f"#Sta/Elev= {len(coords_ds)}\n"
        
        # Format coordinates with proper spacing
        for i, (sta, elev) in enumerate(coords_ds):
            geo_content += f"{sta:8.0f}{elev:8.2f}"
            if (i + 1) % 5 == 0:
                geo_content += "\n"
        if len(coords_ds) % 5 != 0:
            geo_content += "\n"
        
        geo_content += f"#Mann= 3 , 0 , 0 \n"
        geo_content += f"{bank_stations[0]:8.0f}{mannings_n[0]:8.2f}       0{bank_stations[1]:8.0f}{mannings_n[1]:8.2f}       0{coords_ds[-1,0]:8.0f}{mannings_n[2]:8.2f}       0\n"
        geo_content += f"Bank Sta={bank_stations[0]:.0f},{bank_stations[1]:.0f}\n"
        geo_content += f"XS Rating Curve= 0 ,0\n"
        geo_content += f"Exp/Cntr=0.3,0.1\n"

        with open(geo_filename, "w") as f:
            f.write(geo_content)
        print("ASCII geometry file created successfully.")
        return geo_filename
    
    def create_flow_file_text(self, project_path: str, project_name: str,
                             river_name: str, reach_name: str,
                             flow_rate: float, profile_name: str,
                             downstream_slope: float) -> str:
        """
        Generate the HEC-RAS ASCII text steady flow file (.f01).
        
        Args:
            project_path (str): Path to the project directory
            project_name (str): Name of the project
            river_name (str): Name of the river
            reach_name (str): Name of the reach
            flow_rate (float): Flow rate in m³/s
            profile_name (str): Name of the flow profile
            downstream_slope (float): Downstream slope for normal depth boundary
            
        Returns:
            str: Path to the created flow file
        """
        flow_filename = os.path.join(project_path, f"{project_name}.f01")
        print("Creating ASCII flow file...")

        # --- Header and Profile Information ---
        flow_content = f"Flow Title=Q100 Flow\n"
        flow_content += f"Number of Profiles= 1\n"
        flow_content += f"Profile Names={profile_name}\n"

        # --- Flow Data ---
        # Flow is defined at the upstream-most river station
        flow_content += f"River Rch & RM={river_name},{reach_name} ,2000.0     \n"
        flow_content += f"     {flow_rate}\n"

        # --- Boundary Conditions ---
        flow_content += f"Boundary for River Rch & Prof#={river_name},{reach_name} , 1 \n"
        flow_content += f"Up Type= 0 \n"
        flow_content += f"Dn Type= 3 \n"
        flow_content += f"Dn Slope={downstream_slope}\n"

        with open(flow_filename, "w") as f:
            f.write(flow_content)
        print("ASCII flow file created successfully.")
        return flow_filename
    
    def create_plan_file(self, project_path: str, project_name: str,
                        num_interpolated_xs: int = 9,
                        downstream_reach_lengths: List[float] = None) -> str:
        """
        Create the plan file (.p01) linking geometry, flow, and interpolation.
        
        Args:
            project_path (str): Path to the project directory
            project_name (str): Name of the project
            num_interpolated_xs (int): Number of cross-sections to interpolate
            downstream_reach_lengths (List[float]): LOB, Channel, ROB distances
            
        Returns:
            str: Path to the created plan file
        """
        plan_filename = os.path.join(project_path, f"{project_name}.p01")
        
        # Calculate the max distance for interpolation
        if downstream_reach_lengths is None:
            downstream_reach_lengths = [1000, 1000, 1000]
        max_interp_dist = downstream_reach_lengths[1] / (num_interpolated_xs + 1)

        plan_content = textwrap.dedent(f"""\
            Plan Title=Plan 01
            File Title=Plan 01
            Program Version=6.3
            Short Identifier=Plan01
            Geom File=g01
            Flow File=f01
            Flow Regime=Mixed
            I.C. XS=Reach, 2000, 1000,{max_interp_dist:.2f}
            """) # The 'I.C. XS' line is crucial for interpolation with text files

        with open(plan_filename, "w") as f:
            f.write(plan_content)
        print("Plan file with interpolation instructions created successfully.")
        return plan_filename
    
    def open_project(self, project_path: str, project_name: str) -> bool:
        """
        Open a HEC-RAS project.
        
        Args:
            project_path (str): Path to the project directory
            project_name (str): Name of the project
            
        Returns:
            bool: True if project opened successfully, False otherwise
        """
        if not self.hec:
            if not self.connect():
                return False
        
        try:
            prj_file = os.path.join(project_path, f"{project_name}.prj")
            self.hec.Project_Open(prj_file)
            print(f"Project '{prj_file}' opened.")
            return True
        except Exception as e:
            print(f"Failed to open project: {e}")
            return False
    
    def run_simulation(self) -> Tuple[bool, str]:
        """
        Run the HEC-RAS simulation.
        
        Returns:
            Tuple[bool, str]: (success, message)
        """
        if not self.hec:
            return False, "HEC-RAS not connected"
        
        try:
            print("Computing plan...")
            results = self.hec.Compute_CurrentPlan()
            
            # Debug: Print the results structure
            print(f"Results type: {type(results)}")
            print(f"Results content: {results}")
            
            # Check if results is a tuple and has the expected structure
            if isinstance(results, tuple) and len(results) >= 4:
                # Based on HEC-RAS COM API, results structure is:
                # results[0]: Boolean success flag
                # results[1]: Number of errors (integer)
                # results[2]: Tuple of messages
                # results[3]: Additional boolean flag
                
                success_flag = results[0]
                error_count = results[1]
                messages = results[2] if len(results) > 2 else ()
                
                if not success_flag or error_count > 0:
                    # Extract error messages
                    if isinstance(messages, tuple) and len(messages) > 0:
                        error_msg = f"SIMULATION FAILED WITH ERRORS: {', '.join(messages)}"
                    else:
                        error_msg = f"SIMULATION FAILED: {error_count} errors occurred"
                    print(f"--- {error_msg} ---")
                    return False, error_msg
                else:
                    success_msg = "SIMULATION COMPLETED SUCCESSFULLY"
                    print(f"--- {success_msg} ---")
                    return True, success_msg
                    
            elif isinstance(results, tuple) and len(results) >= 2:
                # Fallback for different result structures
                if results[0]:  # First element is success flag
                    success_msg = "SIMULATION COMPLETED SUCCESSFULLY"
                    print(f"--- {success_msg} ---")
                    return True, success_msg
                else:
                    error_msg = f"SIMULATION FAILED: {results[1] if len(results) > 1 else 'Unknown error'}"
                    print(f"--- {error_msg} ---")
                    return False, error_msg
            else:
                # Handle unexpected result format
                success_msg = f"SIMULATION COMPLETED: {results}"
                print(f"--- {success_msg} ---")
                return True, success_msg
                
        except Exception as e:
            error_msg = f"An error occurred during HEC-RAS automation: {e}"
            print(error_msg)
            return False, error_msg
    
    def save_project(self):
        """Save the current HEC-RAS project."""
        if self.hec:
            try:
                self.hec.Project_Save()
                print("Project saved successfully.")
            except Exception as e:
                print(f"Error saving project: {e}")
    
    def show_window(self, delay_seconds: int = 3):
        """
        Show the HEC-RAS main window and pause execution.
        
        Args:
            delay_seconds (int): Number of seconds to pause
        """
        if self.hec:
            try:
                print("Showing HEC-RAS window...")
                self.hec.ShowRAS()
                print(f"Pausing for {delay_seconds} seconds to view the window...")
                time.sleep(delay_seconds)
            except Exception as e:
                print(f"Error showing HEC-RAS window: {e}")
    
    def create_simple_geometry_file(self, base_path: str, file_path: str, 
                                   river_name: str = "River", reach_name: str = "Reach1", 
                                   rs: str = "1.0",
                                   sta_elev: List[Tuple[float, float]] = None,
                                   mann_values: List[Tuple[float, float, int]] = None,
                                   bank_sta: Tuple[float, float] = None, 
                                   reach_lengths: Tuple[float, float, float] = None) -> str:
        """
        Create a basic HEC-RAS geometry file with one river reach and one cross-section.
        
        Args:
            base_path (str): Base directory path
            file_path (str): Path to save the .g01 file
            river_name (str): Name of the river
            reach_name (str): Name of the reach
            rs (str): River station for the cross-section
            sta_elev (List[Tuple[float, float]]): List of (station, elevation) tuples
            mann_values (List[Tuple[float, float, int]]): List of (station, mann_n, 0) tuples
            bank_sta (Tuple[float, float]): Tuple of (left_bank, right_bank) stations
            reach_lengths (Tuple[float, float, float]): Tuple of (LOB, channel, ROB) reach lengths
            
        Returns:
            str: Path to the created geometry file
        """
        if sta_elev is None:
            sta_elev = [(0, 10), (20, 5), (40, 0), (60, 5), (80, 10)]
        if mann_values is None:
            mann_values = [(0, 0.05, 0), (20, 0.03, 0), (80, 0.05, 0)]
        if bank_sta is None:
            bank_sta = (20, 60)
        if reach_lengths is None:
            reach_lengths = (100, 100, 100)
            
        geometry_path = os.path.join(base_path, file_path)
        with open(geometry_path, 'w') as f:
            f.write(f"Geom Title=Simple Fluid Flow Simulation\n")
            f.write(f"River Reach={river_name},{reach_name}\n")
            f.write("Type RM Length/Ang=1 ,0 ,0\n")
            f.write(f"Rch=1 RS={rs}\n")
            # Simple GIS cut line for illustration (adjust coordinates as needed)
            f.write(f"XS GIS Cut Line={river_name},{reach_name} ,{rs} ,0,0 ,80,0 ,80,80 ,0,80\n")
            f.write(f"Begin XS: {rs}\n")
            f.write("XS Rating Curve=0 \n")
            f.write("XS HTab Param=0 ,0 ,0 ,0 ,.2 ,0 \n")
            # XS cut line (simple straight line)
            f.write("XS Cut Line=0 ,0 ,80 ,80\n")
            # Station/Elevation data
            f.write(f"#Sta/Elev={len(sta_elev)} \n")
            for sta, elev in sta_elev:
                f.write(f" {sta} {elev}")
            f.write("\n")
            # Manning's n data
            f.write(f"#Mann={len(mann_values)} ,0 ,0 \n")
            for sta, mann, zero in mann_values:
                f.write(f"{sta} {mann} {zero} \n")
            # Bank stations
            f.write(f"Bank Sta={bank_sta[0]},{bank_sta[1]}\n")
            # Reach lengths (for next XS, but minimal here)
            f.write(f"Reach Lengths={reach_lengths[0]} ,{reach_lengths[1]} ,{reach_lengths[2]}\n")
            f.write("End XS\n")
        
        print(f"Simple geometry file created at: {geometry_path}")
        return geometry_path
