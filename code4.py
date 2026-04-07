import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import PchipInterpolator

# ==========================================
# 1. USER PARAMETERS (Easily Changeable)
# ==========================================
crest_level_masl = 911  # Change this to shift the entire weir up or down

# Target Operating Conditions (Given Data)
TWL_targets = np.array([911.31, 912.41, 913.39, 914.11])
Q_targets = np.array([12.5, 25.3, 50.7, 76.0])

# Hydraulic Constants
Cd = 0.60  # Discharge Coefficient (0.60 for sharp/Ogee, ~0.45 for broad-crested)
g = 9.81   # Gravity (m/s^2)

# ==========================================
# 2. SETUP MATH & INTERPOLATION
# ==========================================
# Calculate Head (H) above the crest for each target
H_targets = TWL_targets - crest_level_masl

# Define the vertical "nodes" where the algorithm will calculate the exact width.
# We include the crest base (H=0) plus the 4 target water levels.
y_nodes = np.insert(H_targets, 0, 0.0)

def get_smooth_width_profile(w_nodes):
    """Creates a smooth curve passing through the width nodes without oscillating."""
    # PCHIP ensures the curve stays smooth and doesn't bulge outward unexpectedly
    return PchipInterpolator(y_nodes, w_nodes)

# ==========================================
# 3. EXPLICIT STRIP INTEGRATION
# ==========================================
def validate_discharge_via_stripes(H, w_nodes, num_stripes=1000):
    """
    Calculates Discharge Q by slicing the water into tiny horizontal stripes,
    calculating flow for each stripe, and summing them up.
    """
    width_function = get_smooth_width_profile(w_nodes)
    dy = H / num_stripes  # Height of each tiny horizontal stripe
    Q_total = 0.0
    
    for i in range(num_stripes):
        # Calculate properties at the center of the current stripe
        y_stripe = (i + 0.5) * dy 
        w_stripe = width_function(y_stripe)
        
        # Ensure the algorithm doesn't test negative widths
        w_stripe = max(w_stripe, 0.01)
        
        # Velocity of water through this specific stripe: v = sqrt(2 * g * Head)
        # Note: Local head for the stripe is (Total H - height of stripe y)
        velocity = np.sqrt(2 * g * (H - y_stripe))
        
        # Discharge for just this tiny stripe: dQ = Cd * Area * Velocity
        dQ = Cd * (w_stripe * dy) * velocity
        
        # Add to total discharge
        Q_total += dQ
        
    return Q_total

# ==========================================
# 4. OPTIMIZATION ALGORITHM
# ==========================================
def objective_function(w_nodes):
    """Finds the widths that minimize the error between calculated Q and Target Q."""
    total_error = 0
    # Test the shape against all 4 operating conditions
    for i, H in enumerate(H_targets):
        Q_calc = validate_discharge_via_stripes(H, w_nodes)
        # Calculate squared percentage error
        total_error += ((Q_calc - Q_targets[i]) / Q_targets[i])**2
        
    # Add a tiny penalty to prevent the bottom of the weir from becoming too jagged
    total_error += 0.0002 * np.sum(np.diff(w_nodes)**2)
    return total_error

# Initial guess: 5 meters wide from bottom to top
initial_widths = np.ones(len(y_nodes)) * 5.0

# Constraints: Width cannot be less than 0.5m or greater than 20m
bounds = [(0.5, 20.0) for _ in range(len(y_nodes))]

print("Optimizing smooth notch profile using explicit strip integration...")
result = minimize(objective_function, initial_widths, bounds=bounds, method='L-BFGS-B')
optimized_w_nodes = result.x

# ==========================================
# 5. GENERATE DATA FOR PLOTTING
# ==========================================
# Create hundreds of points to draw a beautiful, smooth curve
smooth_y = np.linspace(0, np.max(H_targets), 500)
width_curve_func = get_smooth_width_profile(optimized_w_nodes)
smooth_w = width_curve_func(smooth_y)
smooth_masl = smooth_y + crest_level_masl

# Calculate dynamic axis limits
max_width = np.max(smooth_w)
max_masl = np.max(TWL_targets)

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))

# --- Plot 1: Elevation View ---
# Plot solid weir plate (Gray) background
ax1.fill_betweenx(smooth_masl, -max_width*1.5, max_width*1.5, color='slategray', alpha=0.3)
# Carve out the notch (White)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='white')
# Fill the notch with water (Blue) up to max level
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='deepskyblue', alpha=0.5)

# Draw the thick black physical edges of the notch
ax1.plot(smooth_w / 2, smooth_masl, 'k-', lw=3)
ax1.plot(-smooth_w / 2, smooth_masl, 'k-', lw=3)
ax1.plot([-optimized_w_nodes[0]/2, optimized_w_nodes[0]/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)

# Mark the Target Water Levels
labels = ['Minimum', '1 Unit', '2 Units', '3 Units']
for i, H in enumerate(H_targets):
    twl = TWL_targets[i]
    w = width_curve_func(H)
    ax1.axhline(twl, color='blue', linestyle='-.', alpha=0.6)
    # Dimension line
    ax1.plot([-w/2, w/2], [twl, twl], 'k|--', alpha=0.4)
    ax1.text(0, twl + 0.05, f'{labels[i]}: {Q_targets[i]} m³/s | W={w:.2f}m', 
             color='darkblue', fontweight='bold', ha='center', fontsize=9)

ax1.set_title("Elevation View of Smooth Weir Notch", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Elevation (masl)", fontsize=12)

# ADAPTIVE AXES
ax1.set_xlim(-max_width * 0.7, max_width * 0.7)
ax1.set_ylim(crest_level_masl - 0.5, max_masl + 0.5)
ax1.grid(True, linestyle=':', alpha=0.6)

# --- Plot 2: Validation Curve ---
# Calculate continuous performance curve using the strip integration
Q_continuous = [validate_discharge_via_stripes(h, optimized_w_nodes) if h > 0 else 0 for h in smooth_y]

ax2.plot(Q_continuous, smooth_masl, 'b-', lw=2, label='Actual Performance of Notch')
ax2.plot(Q_targets, TWL_targets, 'ro', markersize=8, zorder=5, label='Required Operating Points')

ax2.set_title("Discharge Validation via Strip Integration", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT FINAL DIMENSIONS
# ==========================================
print(f"\n--- VALIDATED NOTCH DIMENSIONS (Crest Level: {crest_level_masl} masl) ---")
print(f"{'Elevation (masl)':<20} | {'Width (m)':<15} | {'Validated Q (m³/s)'}")
print("-" * 60)
# Base of the weir
print(f"{crest_level_masl:<20.2f} | {optimized_w_nodes[0]:<15.2f} | 0.00")
# Target levels
for i, H in enumerate(H_targets):
    w = width_curve_func(H)
    validated_Q = validate_discharge_via_stripes(H, optimized_w_nodes)
    print(f"{TWL_targets[i]:<20.2f} | {w:<15.2f} | {validated_Q:.2f} (Target: {Q_targets[i]:.2f})")