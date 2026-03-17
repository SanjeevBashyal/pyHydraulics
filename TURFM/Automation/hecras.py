import os
import sys
import time
import win32com.client
from typing import Tuple

# Try importing pythonnet for RAS Mapper automation
try:
    import clr

    PYTHONNET_AVAILABLE = True
except ImportError:
    PYTHONNET_AVAILABLE = False
    print("Warning: 'pythonnet' is not installed. RAS Mapper functions will not work.")
    print("Please install it using: pip install pythonnet")


# ==============================================================================
# 1. HECRAS CLASS (COM Automation)
# ==============================================================================


class HECRAS:
    """
    A Python class for interfacing with HEC-RAS through COM automation.
    Handles project creation, opening, saving, and computing simulations.
    """

    def __init__(self, hecras_version: str = "RAS67.HECRASController"):
        self.hecras_version = hecras_version
        self.hec = None
        self.project_path = None
        self.project_name = None

    def connect(self) -> bool:
        """Establish connection to HEC-RAS through COM."""
        try:
            self.hec = win32com.client.Dispatch(self.hecras_version)
            print("Successfully connected to HEC-RAS Controller.")
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
                print("HEC-RAS Controller connection closed.")
            except Exception as e:
                print(f"Error while closing HEC-RAS: {e}")
            finally:
                self.hec = None

    def create_project(
        self, project_path: str, project_name: str, project_title: str = None
    ) -> bool:
        """Create a brand new HEC-RAS project."""
        if not self.hec:
            if not self.connect():
                return False

        if project_title is None:
            project_title = project_name

        try:
            if not os.path.exists(project_path):
                os.makedirs(project_path)
                print(f"Created project directory: {project_path}")

            prj_file = os.path.join(project_path, f"{project_name}.prj")

            if os.path.exists(prj_file):
                print(f"Warning: A project already exists at {prj_file}.")
                print("Opening the existing project instead of creating a new one.")
                return self.open_project(project_path, project_name)

            print(f"Creating new HEC-RAS project: '{project_title}'...")
            self.hec.Project_New(project_title, prj_file)

            self.project_path = project_path
            self.project_name = project_name

            self.hec.Project_Save()
            print(f"Successfully created and saved new project at: {prj_file}")
            return True

        except Exception as e:
            print(f"Failed to create new HEC-RAS project: {e}")
            return False

    def open_project(self, project_path: str, project_name: str) -> bool:
        """Open an existing HEC-RAS project."""
        if not self.hec:
            if not self.connect():
                return False

        try:
            prj_file = os.path.join(project_path, f"{project_name}.prj")
            self.hec.Project_Open(prj_file)
            self.project_path = project_path
            self.project_name = project_name
            print(f"Project '{prj_file}' opened in Controller.")
            return True
        except Exception as e:
            print(f"Failed to open project: {e}")
            return False

    def save_project(self):
        """Save the current HEC-RAS project."""
        if self.hec:
            try:
                self.hec.Project_Save()
                print("Project saved successfully.")
            except Exception as e:
                print(f"Error saving project: {e}")

    def run_simulation(self) -> Tuple[bool, str]:
        """Run the current HEC-RAS plan."""
        if not self.hec:
            return False, "HEC-RAS not connected"
        try:
            print("Computing plan...")
            results = self.hec.Compute_CurrentPlan()

            # Using basic fallback structure for result checking
            if isinstance(results, tuple):
                if results[0]:
                    return True, "SIMULATION COMPLETED SUCCESSFULLY"
                else:
                    return False, f"SIMULATION FAILED. Errors/Messages: {results}"
            else:
                return True, f"SIMULATION COMPLETED: {results}"
        except Exception as e:
            return False, f"An error occurred: {e}"

    def show_window(self, delay_seconds: int = 3):
        """Show the HEC-RAS main window and pause."""
        if self.hec:
            try:
                self.hec.ShowRAS()
                time.sleep(delay_seconds)
            except Exception as e:
                print(f"Error showing window: {e}")
