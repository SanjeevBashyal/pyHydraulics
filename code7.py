import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import PchipInterpolator

# ==========================================
# 1. USER PARAMETERS & SAFETY FACTORS
# ==========================================
crest_level_masl = 910.25
FoS = 1.2  # Factor of Safety on Discharge

# Given Data (Upstream Conditions)
TWL_inlet = np.array([911.31, 912.41, 913.39, 914.11])
Q_target  = np.array([12.5, 25.3, 50.7, 76.0])
Head_loss = np.array([0.10, 0.15, 0.20, 0.25])

# Hydraulic Constants
Cd = 0.60  
g = 9.81   

# ==========================================
# 2. CALCULATE DESIGN SPILLWAY CONDITIONS
# ==========================================
# Apply FoS to get Design Discharges
Q_design = Q_target * FoS

# Subtract Head Loss to get the required design water level at the spillway crest
WL_spillway_design = TWL_inlet - Head_loss
H_eff_design = WL_spillway_design - crest_level_masl

# Add the (0,0) baseline for interpolation
H_eff_full = np.insert(H_eff_design, 0, 0.0)
Q_design_full = np.insert(Q_design, 0, 0.0)

# Create 50 dense points by linearly interpolating the new Design target values
H_dense = np.linspace(0.01, np.max(H_eff_full), 50)
Q_dense_target = np.interp(H_dense, H_eff_full, Q_design_full)

# ==========================================
# 3. SETUP OPTIMIZATION MATH
# ==========================================
num_nodes = 20
y_nodes = np.linspace(0, np.max(H_eff_full), num_nodes)

def objective_function(w_nodes):
    w_func = PchipInterpolator(y_nodes, w_nodes)
    error = 0.0
    
    for i, H in enumerate(H_dense):
        y_strips = np.linspace(0, H, 150)
        dy = y_strips[1] - y_strips[0]
        w_strips = np.maximum(w_func(y_strips), 0.1)
        
        integrand = Cd * w_strips * np.sqrt(2 * g * (H - y_strips))
        Q_calc = np.trapz(integrand, y_strips)
        error += ((Q_calc - Q_dense_target[i]) / max(Q_dense_target[i], 1.0))**2
        
    second_derivative = np.diff(w_nodes, n=2)
    smoothness_penalty = 0.5 * np.sum(second_derivative**2)
    return error + smoothness_penalty

# ==========================================
# 4. RUN OPTIMIZATION
# ==========================================
bounds = [(0.1, 40.0) for _ in range(num_nodes)]
initial_widths = np.linspace(15.0, 3.0, num_nodes)

print("Optimizing weir profile for FoS=1.2 Design Capacity...")
result = minimize(objective_function, initial_widths, bounds=bounds, method='L-BFGS-B', options={'maxiter': 2000})
optimized_w_nodes = result.x

# ==========================================
# 5. POST-CALCULATION: FIND ACTUAL FLOW LEVELS
# ==========================================
smooth_y = np.linspace(0, np.max(H_eff_full), 500)
w_func_final = PchipInterpolator(y_nodes, optimized_w_nodes)
smooth_w = w_func_final(smooth_y)
smooth_masl = smooth_y + crest_level_masl

# Calculate actual continuous discharge of the designed notch
Q_achieved_continuous = []
for H in smooth_y:
    if H == 0: 
        Q_achieved_continuous.append(0.0)
        continue
    y_strp = np.linspace(0, H, 200)
    w_strp = np.maximum(w_func_final(y_strp), 0.1)
    integ = Cd * w_strp * np.sqrt(2 * g * (H - y_strp))
    Q_achieved_continuous.append(np.trapz(integ, y_strp))

# Interpolate to find EXACTLY where the water sits for Actual Target Qs
H_actual = np.interp(Q_target, Q_achieved_continuous, smooth_y)
WL_spillway_actual = crest_level_masl + H_actual

# Actual Upstream TWL = Actual Spillway WL + Given Head Loss
TWL_inlet_actual = WL_spillway_actual + Head_loss

max_w = np.max(smooth_w)
max_masl = np.max(WL_spillway_design)

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))

# --- PLOT 1: ELEVATION VIEW ---
ax1.fill_betweenx(smooth_masl, -max_w*1.2, max_w*1.2, color='slategray', alpha=0.3)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='white')

# Fill Light Blue for Safety Margin (Design Max)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='lightblue', alpha=0.4, label='Safety Margin (FoS)')
# Fill Deep Blue for Actual Flow (Up to 3 Units actual max)
actual_max_y = np.linspace(0, np.max(H_actual), 200)
actual_max_w = w_func_final(actual_max_y)
ax1.fill_betweenx(actual_max_y + crest_level_masl, -actual_max_w/2, actual_max_w/2, color='deepskyblue', alpha=0.7, label='Actual Max Flow Area')

ax1.plot(smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot(-smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot([-optimized_w_nodes[0]/2, optimized_w_nodes[0]/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)

labels = ['Min', '1 Unit', '2 Units', '3 Units']
for i in range(len(WL_spillway_design)):
    # Plot Actual Flow Levels (Green, left-aligned text)
    ax1.axhline(WL_spillway_actual[i], color='green', linestyle='--', alpha=0.8)
    ax1.text(-max_w*0.55, WL_spillway_actual[i] + 0.03, 
             f"ACTUAL: {Q_target[i]} m³/s\nSpill WL: {WL_spillway_actual[i]:.2f}", 
             color='green', fontweight='bold', ha='left', fontsize=8)
    
    # Plot Design Limits (Red, right-aligned text)
    ax1.axhline(WL_spillway_design[i], color='red', linestyle=':', alpha=0.6)
    ax1.text(max_w*0.55, WL_spillway_design[i] + 0.03, 
             f"DESIGN: {Q_design[i]:.1f} m³/s\nLimit WL: {WL_spillway_design[i]:.2f}", 
             color='darkred', fontweight='bold', ha='right', fontsize=8)

ax1.set_title(f"Elevation Profile\nComparing Actual Flow vs FoS Design", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Spillway Elevation (masl)", fontsize=12)
ax1.set_xlim(-max_w * 0.6, max_w * 0.6)
ax1.set_ylim(crest_level_masl - 0.2, max_masl + 0.5)
ax1.legend(loc='lower center')
ax1.grid(True, linestyle=':', alpha=0.6)

# --- PLOT 2: PERFORMANCE CURVE ---
ax2.plot(Q_achieved_continuous, smooth_masl, 'b-', lw=2, label='Designed Spillway Capacity Curve')

# Plot explicitly requested Design points (Red)
ax2.plot(Q_design, WL_spillway_design, 'ro', markersize=8, markeredgecolor='black', label='Design Limits (w/ FoS 1.2)')

# Plot Actual Operation points (Green)
ax2.plot(Q_target, WL_spillway_actual, 'go', markersize=8, markeredgecolor='black', label='Actual Operating Flows')

for i in range(len(Q_target)):
    # Draw a line connecting actual operation to its design limit to show the safety margin
    ax2.plot([Q_target[i], Q_design[i]], [WL_spillway_actual[i], WL_spillway_design[i]], 'k--', alpha=0.3)

ax2.set_title("Spillway Performance & Operating Points", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Effective Spillway Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT TABULAR REPORT
# ==========================================
print("\n" + "="*85)
print(" "*30 + "ENGINEERING REPORT")
print("="*85)
print(f"Crest Elevation: {crest_level_masl} masl")
print(f"Factor of Safety: {FoS}")

print("\n--- 1. DESIGN LIMITS (Used to shape the physical concrete/plate) ---")
print(f"{'Condition':<10} | {'Given TWL':<12} | {'Head Loss':<10} | {'Limit Spill WL':<15} | {'Design Q (w/ FoS)':<15}")
print("-" * 75)
for i in range(len(TWL_inlet)):
    print(f"{labels[i]:<10} | {TWL_inlet[i]:<12.2f} | {Head_loss[i]:<10.2f} | {WL_spillway_design[i]:<15.2f} | {Q_design[i]:<15.2f}")

print("\n--- 2. ACTUAL EXPECTED OPERATIONS (Where water will actually sit) ---")
print(f"{'Condition':<10} | {'Actual TWL':<12} | {'Head Loss':<10} | {'Actual Spill WL':<15} | {'Actual Flow Q':<15}")
print("-" * 75)
for i in range(len(TWL_inlet)):
    print(f"{labels[i]:<10} | {TWL_inlet_actual[i]:<12.2f} | {Head_loss[i]:<10.2f} | {WL_spillway_actual[i]:<15.2f} | {Q_target[i]:<15.2f}")

print("\n--- 3. OPTIMIZED CAD PROFILE DIMENSIONS ---")
print(f"{'Spillway Elev (masl)':<22} | {'Required Width (m)':<15}")
print("-" * 40)
for i in range(num_nodes):
    print(f"{crest_level_masl + y_nodes[i]:<22.2f} | {optimized_w_nodes[i]:<15.2f}")