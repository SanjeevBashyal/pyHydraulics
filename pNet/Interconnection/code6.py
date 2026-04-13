import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import PchipInterpolator

# ==========================================
# 1. USER PARAMETERS & SAFETY FACTORS
# ==========================================
crest_level_masl = 910.0
FoS = 1.2  # Factor of Safety on Discharge

# Given Data (Upstream Conditions)
TWL_inlet = np.array([911.31, 912.41, 913.39, 914.11])
Q_target  = np.array([12.5, 25.3, 50.7, 76.0])
Head_loss = np.array([0.10, 0.15, 0.20, 0.25])

# Hydraulic Constants
Cd = 0.60  # Discharge Coefficient
g = 9.81   # Gravity (m/s^2)

# ==========================================
# 2. CALCULATE LOCAL SPILLWAY CONDITIONS
# ==========================================
# Apply FoS to get Design Discharges
Q_design = Q_target * FoS

# Subtract Head Loss to get the actual water level at the spillway crest
WL_spillway = TWL_inlet - Head_loss

# Calculate the Effective Head over the crest
H_eff = WL_spillway - crest_level_masl

# Add the (0,0) baseline for interpolation
H_eff_full = np.insert(H_eff, 0, 0.0)
Q_design_full = np.insert(Q_design, 0, 0.0)

# Create 50 dense points by linearly interpolating the new Design target values
H_dense = np.linspace(0.01, np.max(H_eff_full), 50)
Q_dense_target = np.interp(H_dense, H_eff_full, Q_design_full)

# ==========================================
# 3. SETUP OPTIMIZATION MATH
# ==========================================
num_nodes = 20  # Vertical control points for the CAD curve
y_nodes = np.linspace(0, np.max(H_eff_full), num_nodes)

def objective_function(w_nodes):
    """Evaluates the weir shape against the required Design Q and Local Head."""
    # Create the smooth width function for this iteration
    w_func = PchipInterpolator(y_nodes, w_nodes)
    error = 0.0
    
    # 1. Calculate Error against dense target profile via strip integration
    for i, H in enumerate(H_dense):
        # Generate strips for integration
        y_strips = np.linspace(0, H, 150)
        dy = y_strips[1] - y_strips[0]
        
        # Ensure widths don't dip below 0.1m during math checks
        w_strips = np.maximum(w_func(y_strips), 0.1)
        
        # dQ = Cd * Width * dy * sqrt(2*g*head)
        integrand = Cd * w_strips * np.sqrt(2 * g * (H - y_strips))
        Q_calc = np.trapz(integrand, y_strips)
        
        # Relative squared error
        error += ((Q_calc - Q_dense_target[i]) / max(Q_dense_target[i], 1.0))**2
        
    # 2. Smoothness Penalty (Tikhonov Regularization) to ensure a clean CAD curve
    second_derivative = np.diff(w_nodes, n=2)
    smoothness_penalty = 0.5 * np.sum(second_derivative**2)
    
    return error + smoothness_penalty

# ==========================================
# 4. RUN OPTIMIZATION
# ==========================================
# Bounds: Width between 0.1m and 40m (Linear Q requires a very wide base)
bounds = [(0.1, 40.0) for _ in range(num_nodes)]
initial_widths = np.linspace(15.0, 3.0, num_nodes)

print("Optimizing weir profile for FoS=1.2 and Local Spillway Head...")
result = minimize(objective_function, initial_widths, bounds=bounds, method='L-BFGS-B', options={'maxiter': 2000})
optimized_w_nodes = result.x

# ==========================================
# 5. GENERATE PLOTTING DATA
# ==========================================
smooth_y = np.linspace(0, np.max(H_eff_full), 500)
w_func_final = PchipInterpolator(y_nodes, optimized_w_nodes)
smooth_w = w_func_final(smooth_y)
smooth_masl = smooth_y + crest_level_masl

# Calculate actual continuous discharge achieved
Q_achieved_continuous = []
for H in smooth_y:
    if H == 0: 
        Q_achieved_continuous.append(0.0)
        continue
    y_strp = np.linspace(0, H, 200)
    w_strp = np.maximum(w_func_final(y_strp), 0.1)
    integ = Cd * w_strp * np.sqrt(2 * g * (H - y_strp))
    Q_achieved_continuous.append(np.trapz(integ, y_strp))

max_w = np.max(smooth_w)
max_masl = np.max(WL_spillway)

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7.5))

# --- PLOT 1: ELEVATION VIEW (Physical geometry) ---
ax1.fill_betweenx(smooth_masl, -max_w*1.2, max_w*1.2, color='slategray', alpha=0.3)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='white')
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='deepskyblue', alpha=0.5)

ax1.plot(smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot(-smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot([-optimized_w_nodes[0]/2, optimized_w_nodes[0]/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)

# Annotate Target Levels (Showing Local Spillway WL)
labels = ['Min', '1 Unit', '2 Units', '3 Units']
for i in range(len(WL_spillway)):
    ax1.axhline(WL_spillway[i], color='blue', linestyle='-.', alpha=0.6)
    w_at_target = w_func_final(H_eff[i])
    ax1.plot([-w_at_target/2, w_at_target/2], [WL_spillway[i], WL_spillway[i]], 'k|--', alpha=0.4)
    
    text_str = (f'{labels[i]}: Q_des={Q_design[i]:.1f}\n'
                f'Spill WL={WL_spillway[i]:.2f}m\n'
                f'(TWL={TWL_inlet[i]:.2f}) | W={w_at_target:.2f}m')
    ax1.text(0, WL_spillway[i] + 0.06, text_str, color='darkblue', fontweight='bold', ha='center', fontsize=8.5)

ax1.set_title(f"Elevation Profile (Designed for Effective Spillway WL)\nFoS = {FoS}", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Spillway Local Elevation (masl)", fontsize=12)
ax1.set_xlim(-max_w * 0.6, max_w * 0.6)
ax1.set_ylim(crest_level_masl - 0.5, max_masl + 0.5)
ax1.grid(True, linestyle=':', alpha=0.6)

# --- PLOT 2: PERFORMANCE CURVE ---
# Plot target linear profile
ax2.plot(Q_design_full, H_eff_full + crest_level_masl, 'r--', lw=3, alpha=0.6, label='Required Linear Profile (FoS applied)')
# Plot achieved profile
ax2.plot(Q_achieved_continuous, smooth_masl, 'b-', lw=2, label='Actual Achieved Profile')
# Plot explicit target points
ax2.plot(Q_design, WL_spillway, 'ro', markersize=8, markeredgecolor='black', zorder=5, label='Design Operating Points')

ax2.set_title("Q (Design) vs Effective Spillway Elevation", fontsize=14, fontweight='bold')
ax2.set_xlabel("Design Discharge Q (m³/s) [Includes FoS]", fontsize=12)
ax2.set_ylabel("Effective Spillway Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT FINAL DIMENSIONS
# ==========================================
print("\n--- FINAL DESIGN PARAMETERS ---")
print(f"Crest Elevation: {crest_level_masl} masl")
print(f"{'Condition':<10} | {'TWL (Inlet)':<12} | {'Head Loss':<10} | {'Spillway WL':<12} | {'Q Target':<10} | {'Q Design (FoS)':<15}")
print("-" * 80)
for i in range(len(TWL_inlet)):
    print(f"{labels[i]:<10} | {TWL_inlet[i]:<12.2f} | {Head_loss[i]:<10.2f} | {WL_spillway[i]:<12.2f} | {Q_target[i]:<10.2f} | {Q_design[i]:<15.2f}")

print("\n--- OPTIMIZED CAD PROFILE DIMENSIONS ---")
print(f"{'Spillway Elev (masl)':<22} | {'Required Width (m)':<15}")
print("-" * 40)
for i in range(num_nodes):
    print(f"{crest_level_masl + y_nodes[i]:<22.2f} | {optimized_w_nodes[i]:<15.2f}")