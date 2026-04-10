import math
import pandas as pd

def calculate_rated_head(installed_capacity_mw, total_discharge, turbine_efficiency=0.92, generator_efficiency=0.97):
    """
    Calculates the Rated Net Head based on capacity, discharge, and specific efficiencies.
    
    installed_capacity_mw: Total plant capacity in Megawatts (MW)
    total_discharge: Total design discharge in m^3/s
    turbine_efficiency: Mechanical/Hydraulic efficiency of the Francis turbine (default 92%)
    generator_efficiency: Electrical efficiency of the generator (default 97%)
    """
    # 1. Convert MW to kW
    capacity_kw = installed_capacity_mw * 1000
    
    # 2. Calculate overall efficiency
    overall_efficiency = turbine_efficiency * generator_efficiency
    
    # 3. Gravity constant
    gravity = 9.81
    
    # 4. Formula: H = P(kW) / (9.81 * Q * overall_efficiency)
    rated_head = capacity_kw / (gravity * total_discharge * overall_efficiency)
    
    # (Optional) Print the breakdown for verification
    print(f"--- RATED HEAD CALCULATION ---")
    print(f"Installed Capacity : {installed_capacity_mw} MW ({capacity_kw} kW)")
    print(f"Design Discharge   : {total_discharge} m³/s")
    print(f"Turbine Efficiency : {turbine_efficiency * 100}%")
    print(f"Generator Eff.     : {generator_efficiency * 100}%")
    print(f"Overall Efficiency : {overall_efficiency * 100:.1f}%")
    print(f"Rated Head : {rated_head:.2f}")
    print("")
    
    return rated_head

class FrancisHydropowerSystem:
    def __init__(self, discharge, kinematic_viscosity=1.004e-6):
        """
        discharge: Flow rate in m^3/s
        kinematic_viscosity: Water viscosity in m^2/s (default is water at 20 deg C)
        """
        self.Q = discharge
        self.nu = kinematic_viscosity
        self.g = 9.81
        self.segments = []

    def load_segments_from_excel(self, filepath):
        """
        Reads pipe segments from an Excel file and adds them to the system.
        """
        try:
            df = pd.read_excel(filepath)
            
            for index, row in df.iterrows():
                self.segments.append({
                    'name': str(row['Segment Name']),
                    'L': float(row['Length (m)']),
                    'D': float(row['Diameter (m)']),
                    'roughness': float(row['Roughness (mm)']) / 1000.0, # convert mm to m
                    'K_minor': float(row['Sum Minor K'])
                })
            print(f"Successfully loaded {len(self.segments)} segments from {filepath}\n")
            
        except FileNotFoundError:
            print(f"Error: The file '{filepath}' was not found.")
            exit()
        except KeyError as e:
            print(f"Error: Missing expected column in Excel file: {e}")
            exit()

    def calculate_losses(self):
        """
        Calculates major and minor losses for all loaded segments (HRT, Penstock).
        Note: Draft tube losses are typically accounted for inside the turbine's guaranteed Net Head.
        """
        total_head_loss = 0
        print(f"{'Segment':<15} | {'Vel (m/s)':<10} | {'Friction (m)':<15} | {'Minor Loss (m)':<15}")
        print("-" * 65)
        
        for seg in self.segments:
            # 1. Calculate Velocity
            Area = math.pi * (seg['D']**2) / 4.0
            Velocity = self.Q / Area
            Velocity_head = (Velocity**2) / (2 * self.g)
            
            # 2. Calculate Reynolds Number
            Re = (Velocity * seg['D']) / self.nu
            
            # 3. Calculate Friction Factor (f) using Swamee-Jain equation
            if Re < 2000:
                f = 64.0 / Re  # Laminar flow
            else:
                f = 0.25 / (math.log10((seg['roughness'] / (3.7 * seg['D'])) + (5.74 / (Re**0.9))))**2
            
            # 4. Calculate Major (Friction) Loss
            hf = f * (seg['L'] / seg['D']) * Velocity_head
            
            # 5. Calculate Minor Losses
            hm = seg['K_minor'] * Velocity_head
            
            segment_loss = hf + hm
            total_head_loss += segment_loss
            
            print(f"{seg['name'][:14]:<15} | {Velocity:<10.2f} | {hf:<15.2f} | {hm:<15.2f}")
            
        print("-" * 65)
        print(f"Total Head Loss (Pipe/Penstock): {total_head_loss:.2f} m\n")
        return total_head_loss

    def calculate_forebay_level(self, tailwater_level, required_net_head):
        """
        For a Reaction Turbine (Francis), Gross Head = Forebay Level - Tailwater Level.
        Since Net Head = Gross Head - Losses:
        Forebay Level = Tailwater Level + Net Head + Losses
        """
        total_loss = self.calculate_losses()
        forebay_level = tailwater_level + required_net_head + total_loss
        return forebay_level

# ==========================================
# HOW TO USE THE SCRIPT FOR A FRANCIS TURBINE
# ==========================================

if __name__ == "__main__":
    # 1. Operational Parameters for Francis Turbine
    DESIGN_DISCHARGE = 51.0          # Flow rate (m^3/s)
    TAILWATER_LEVEL = 602.9         # Normal operating water level in the tailrace (m ASL)
    REQUIRED_NET_HEAD = 150.0        # Net Head required by the Francis Turbine manufacturer (m)
    CAPACITY_MW = 15.625
    
    EXCEL_FILE_PATH = r"E:\0_Python\pyHydraulics\forebay.xlsx" 

    calculated_head = calculate_rated_head(
        installed_capacity_mw=CAPACITY_MW,
        total_discharge=DESIGN_DISCHARGE,
        turbine_efficiency=0.92,   # e.g., 92%
        generator_efficiency=0.97  # e.g., 97%
    )

    # 2. Initialize the system
    hydro_plant = FrancisHydropowerSystem(discharge=DESIGN_DISCHARGE)

    # 3. Load segments from Excel
    hydro_plant.load_segments_from_excel(EXCEL_FILE_PATH)

    # 4. Calculate Forebay Level
    required_forebay_level = hydro_plant.calculate_forebay_level(
        tailwater_level=TAILWATER_LEVEL, 
        required_net_head=REQUIRED_NET_HEAD
    )

    print(f"--> TARGET FOREBAY WATER LEVEL: {required_forebay_level:.2f} meters above sea level")
    print(f"--> GROSS HEAD: {(required_forebay_level - TAILWATER_LEVEL):.2f} m")