# HECRAS Class Documentation

The `HECRAS` class provides a Python interface to HEC-RAS through COM automation. This class encapsulates all the functionality needed to create, configure, and run HEC-RAS models programmatically.

## Features

- **Project Management**: Create and manage HEC-RAS project structures
- **Geometry Creation**: Generate HEC-RAS geometry files (.g01) with cross-sections
- **Flow Data**: Create steady flow files (.f01) with boundary conditions
- **Plan Files**: Generate plan files (.p01) for simulation control
- **COM Automation**: Direct interface with HEC-RAS through COM
- **Simulation Control**: Run simulations and retrieve results
- **File I/O**: Handle all HEC-RAS project file formats

## Installation Requirements

- Python 3.7+
- HEC-RAS installed on Windows
- `pywin32` package for COM automation
- `numpy` for numerical operations

```bash
pip install pywin32 numpy
```

## Basic Usage

### 1. Import and Initialize

```python
from pyhydraulics import HECRAS

# Create HECRAS instance
hecras = HECRAS("RAS67.HECRASController")  # Specify your HEC-RAS version
```

### 2. Create Project Structure

```python
# Create project directory and .prj file
project_path = hecras.create_project_structure(
    base_path="C:/MyProjects", 
    name="MyRiverModel"
)
```

### 3. Define Geometry

```python
import numpy as np

# Define cross-section coordinates [station, elevation]
xs_coordinates = np.array([
    [0, 10.0], [20, 10.0], [20, 5.0], [30, 5.0], [35, 2.0],
    [45, 2.0], [50, 5.0], [60, 5.0], [60, 10.0], [80, 10.0]
])

# Create geometry file
hecras.create_geometry_file_text(
    project_path=project_path,
    project_name="MyRiverModel",
    river_name="MainRiver",
    reach_name="Reach1",
    xs_coordinates=xs_coordinates,
    mannings_n=[0.05, 0.03, 0.05],  # LOB, Channel, ROB
    bank_stations=[30, 50],           # Left Bank, Right Bank
    downstream_reach_lengths=[1000, 1000, 1000],
    upstream_elevation_adjust=1.0
)
```

### 4. Define Flow Data

```python
# Create flow file
hecras.create_flow_file_text(
    project_path=project_path,
    project_name="MyRiverModel",
    river_name="MainRiver",
    reach_name="Reach1",
    flow_rate=150.0,        # m³/s
    profile_name="Q100",
    downstream_slope=0.001
)
```

### 5. Create Plan File

```python
# Create plan file with interpolation
hecras.create_plan_file(
    project_path=project_path,
    project_name="MyRiverModel",
    num_interpolated_xs=9,
    downstream_reach_lengths=[1000, 1000, 1000]
)
```

### 6. Run Simulation

```python
# Connect to HEC-RAS
if hecras.connect():
    # Open project
    if hecras.open_project(project_path, "MyRiverModel"):
        # Run simulation
        success, message = hecras.run_simulation()
        
        if success:
            print("Simulation completed successfully!")
        else:
            print(f"Simulation failed: {message}")
        
        # Save project
        hecras.save_project()
    
    # Clean up
    hecras.disconnect()
```

## Complete Example

```python
from pyhydraulics import HECRAS
import numpy as np

def create_simple_canal_model():
    # Initialize
    hecras = HECRAS("RAS67.HECRASController")
    
    try:
        # Create project
        proj_path = hecras.create_project_structure("C:/Models", "CanalModel")
        
        # Define geometry
        xs_coords = np.array([[0, 10], [20, 5], [40, 0], [60, 5], [80, 10]])
        
        hecras.create_geometry_file_text(
            proj_path, "CanalModel", "Canal", "MainReach",
            xs_coords, [0.05, 0.03, 0.05], [20, 60], [500, 500, 500]
        )
        
        # Define flow
        hecras.create_flow_file_text(
            proj_path, "CanalModel", "Canal", "MainReach",
            100.0, "Q100", 0.002
        )
        
        # Create plan
        hecras.create_plan_file(proj_path, "CanalModel")
        
        # Run simulation
        if hecras.connect():
            hecras.open_project(proj_path, "CanalModel")
            success, msg = hecras.run_simulation()
            print(f"Simulation: {'Success' if success else 'Failed'}")
            hecras.disconnect()
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    create_simple_canal_model()
```

## Class Methods Reference

### Core Methods

- `connect()`: Establish COM connection to HEC-RAS
- `disconnect()`: Close HEC-RAS connection
- `create_project_structure(base_path, name)`: Create project directory and .prj file
- `open_project(project_path, project_name)`: Open existing project
- `save_project()`: Save current project
- `run_simulation()`: Execute current plan

### File Creation Methods

- `create_geometry_file_text(...)`: Create .g01 geometry file
- `create_flow_file_text(...)`: Create .f01 flow file
- `create_plan_file(...)`: Create .p01 plan file
- `create_simple_geometry_file(...)`: Create basic geometry file

### Utility Methods

- `show_window(delay_seconds)`: Display HEC-RAS window
- Various getter/setter methods for model parameters

## Error Handling

The class includes comprehensive error handling:

```python
try:
    success, message = hecras.run_simulation()
    if success:
        print("✓ Simulation successful")
    else:
        print(f"✗ Simulation failed: {message}")
except Exception as e:
    print(f"Error: {e}")
finally:
    hecras.disconnect()
```

## Troubleshooting

### Common Issues

1. **COM Connection Failed**: Ensure HEC-RAS is installed and the version string is correct
2. **Geometry File Errors**: Check that cross-section coordinates are properly formatted
3. **Flow File Errors**: Verify boundary conditions and flow rates
4. **Simulation Failures**: Check HEC-RAS error messages in the results

### Debug Mode

Enable debug output by checking the console messages:

```python
# The class provides detailed logging of all operations
hecras = HECRAS()
# All operations will print status messages
```

## Advanced Features

- **Cross-section Interpolation**: Automatically generate intermediate cross-sections
- **Multiple Reaches**: Support for complex river networks
- **Custom Boundary Conditions**: Flexible upstream/downstream conditions
- **Batch Processing**: Run multiple simulations programmatically

## Contributing

To extend the HECRAS class:

1. Add new methods to the class
2. Update the documentation
3. Include type hints for all parameters
4. Add error handling for new functionality
5. Test with various HEC-RAS versions

## License

This class is part of the pyHydraulics package and follows the same license terms.
