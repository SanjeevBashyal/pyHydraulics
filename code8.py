import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import PchipInterpolator

# ==========================================
# 1. USER PARAMETERS & SAFETY FACTORS
# ==========================================
crest_level_masl = 910.5
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

# --- STEP A: Calculate the Rectangular Base ---
H_1 = H_eff_design[0]
Q_1 = Q_design[0]

# Standard Rectangular Weir Equation: Q = (2/3) * Cd * sqrt(2g) * L * H^(3/2)
# Rearranging to find Required Length (Width) L:
W_rect = Q_1 / ((2/3) * Cd * np.sqrt(2 * g) * (H_1**1.5))

# --- STEP B: Define Linear Targets Above the Base ---
# We only linearly interpolate between the 1st and 4th data point
H_dense = np.linspace(H_1 + 0.01, H_eff_design[-1], 40)
Q_dense_target = np.interp(H_dense, H_eff_design, Q_design)

# ==========================================
# 3. SETUP PIECEWISE OPTIMIZATION MATH
# ==========================================
# Vertical nodes for the curved portion above the rectangular base
num_opt_nodes = 15
y_opt_nodes = np.linspace(H_1, H_eff_design[-1], num_opt_nodes)

def get_piecewise_width_func(w_opt):
    """Creates a function that returns W_rect for the base, and curves above it."""
    # Combine the fixed base width with the optimized upper widths
    y_curve = y_opt_nodes
    w_curve = np.concatenate(([W_rect], w_opt))
    pchip = PchipInterpolator(y_curve, w_curve)
    
    def w_func(y_array):
        y_array = np.atleast_1d(y_array)
        w = np.zeros_like(y_array)
        
        # Rectangular Base logic
        mask_rect = y_array <= H_1
        w[mask_rect] = W_rect
        
        # Curved Upper logic
        mask_curve = y_array > H_1
        if np.any(mask_curve):
            w[mask_curve] = pchip(y_array[mask_curve])
        return w
    return w_func

def objective_function(w_opt):
    w_func = get_piecewise_width_func(w_opt)
    error = 0.0
    
    # 1. Integration Error (Check against linear targets)
    for i, H in enumerate(H_dense):
        y_strips = np.linspace(0, H, 150)
        dy = y_strips[1] - y_strips[0]
        w_strips = np.maximum(w_func(y_strips), 0.1) # Prevent negative width
        
        integrand = Cd * w_strips * np.sqrt(2 * g * (H - y_strips))
        Q_calc = np.trapz(integrand, y_strips)
        error += ((Q_calc - Q_dense_target[i]) / max(Q_dense_target[i], 1.0))**2
        
    # 2. Smoothness Penalty on the upper curve
    # We include W_rect to ensure the curve connects smoothly to the rectangular base
    full_w = np.concatenate(([W_rect], w_opt))
    smoothness_penalty = 0.5 * np.sum(np.diff(full_w, n=2)**2)
    return error + smoothness_penalty

# ==========================================
# 4. RUN OPTIMIZATION
# ==========================================
# We optimize (num_opt_nodes - 1) nodes, because the first node is permanently W_rect
bounds = [(0.1, 40.0) for _ in range(num_opt_nodes - 1)]
initial_widths = np.linspace(W_rect, W_rect * 0.5, num_opt_nodes - 1)

print("Calculating Rectangular Base...")
print(f"Base Width required: {W_rect:.2f} m up to Elevation {crest_level_masl + H_1:.2f} masl.")
print("Optimizing upper proportional curve...")
result = minimize(objective_function, initial_widths, bounds=bounds, method='L-BFGS-B', options={'maxiter': 2000})
optimized_upper_widths = result.x

# ==========================================
# 5. POST-CALCULATION & PERFORMANCE PROFILES
# ==========================================
smooth_y = np.linspace(0, H_eff_design[-1], 500)
final_w_func = get_piecewise_width_func(optimized_upper_widths)
smooth_w = final_w_func(smooth_y)
smooth_masl = smooth_y + crest_level_masl

# Calculate actual continuous discharge over the entire shape
Q_achieved_continuous = []
for H in smooth_y:
    if H == 0: 
        Q_achieved_continuous.append(0.0)
        continue
    y_strp = np.linspace(0, H, 200)
    w_strp = np.maximum(final_w_func(y_strp), 0.1)
    integ = Cd * w_strp * np.sqrt(2 * g * (H - y_strp))
    Q_achieved_continuous.append(np.trapz(integ, y_strp))

# Interpolate to find actual operational water levels (Without FoS limits)
H_actual = np.interp(Q_target, Q_achieved_continuous, smooth_y)
WL_spillway_actual = crest_level_masl + H_actual
TWL_inlet_actual = WL_spillway_actual + Head_loss

# Create mathematical target curve for Plot 2 (H^1.5 for base, Linear for upper)
H_target_plot = np.concatenate((np.linspace(0, H_1, 50), H_dense))
Q_target_plot = np.concatenate(( (2/3)*Cd*np.sqrt(2*g)*W_rect*(np.linspace(0, H_1, 50)**1.5), Q_dense_target ))

max_w = np.max(smooth_w)
max_masl = np.max(WL_spillway_design)

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))

# --- PLOT 1: ELEVATION VIEW ---
ax1.fill_betweenx(smooth_masl, -max_w*1.2, max_w*1.2, color='slategray', alpha=0.3)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='white')

# Fill Light Blue (Safety Margin / Design Max)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='lightblue', alpha=0.4, label='Safety Margin (FoS)')
# Fill Deep Blue (Actual Operations)
actual_max_y = np.linspace(0, np.max(H_actual), 200)
actual_max_w = final_w_func(actual_max_y)
ax1.fill_betweenx(actual_max_y + crest_level_masl, -actual_max_w/2, actual_max_w/2, color='deepskyblue', alpha=0.7, label='Actual Max Flow Area')

# Draw geometric edges
ax1.plot(smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot(-smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot([-W_rect/2, W_rect/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)
# Highlight the rectangular transition line
ax1.plot([-W_rect/2, W_rect/2], [crest_level_masl + H_1, crest_level_masl + H_1], 'k--', lw=2, alpha=0.5)
ax1.text(0, crest_level_masl + H_1/2, "RECTANGULAR\nBASE", ha='center', va='center', color='black', alpha=0.5, fontweight='bold')

labels = ['Min', '1 Unit', '2 Units', '3 Units']
for i in range(len(WL_spillway_design)):
    ax1.axhline(WL_spillway_actual[i], color='green', linestyle='--', alpha=0.8)
    ax1.text(-max_w*0.55, WL_spillway_actual[i] + 0.03, 
             f"ACTUAL: {Q_target[i]} m³/s", color='green', fontweight='bold', ha='left', fontsize=8)
    
    ax1.axhline(WL_spillway_design[i], color='red', linestyle=':', alpha=0.6)
    ax1.text(max_w*0.55, WL_spillway_design[i] + 0.03, 
             f"DESIGN: {Q_design[i]:.1f} m³/s", color='darkred', fontweight='bold', ha='right', fontsize=8)

ax1.set_title(f"Elevation Profile\nRectangular Base + Optimized Upper Curve", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Spillway Elevation (masl)", fontsize=12)
ax1.set_xlim(-max_w * 0.6, max_w * 0.6)
ax1.set_ylim(crest_level_masl - 0.2, max_masl + 0.3)
ax1.legend(loc='lower center')
ax1.grid(True, linestyle=':', alpha=0.6)

# --- PLOT 2: PERFORMANCE CURVE ---
# Plot Required Targets
ax2.plot(Q_target_plot, H_target_plot + crest_level_masl, 'r--', lw=3, alpha=0.6, label='Required Q Curve (H^1.5 then Linear)')
# Plot Achieved Performance
ax2.plot(Q_achieved_continuous, smooth_masl, 'b-', lw=2, label='Achieved Physical Capacity')

# Operational points
ax2.plot(Q_design, WL_spillway_design, 'ro', markersize=8, markeredgecolor='black', label='Design Limits (w/ FoS 1.2)')
ax2.plot(Q_target, WL_spillway_actual, 'go', markersize=8, markeredgecolor='black', label='Actual Operations')

for i in range(len(Q_target)):
    ax2.plot([Q_target[i], Q_design[i]], [WL_spillway_actual[i], WL_spillway_design[i]], 'k--', alpha=0.3)

ax2.set_title("Spillway Performance Verification", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Effective Spillway Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT TABULAR REPORT
# ==========================================
print("\n" + "="*80)
print(" "*25 + "ENGINEERING REPORT")
print("="*80)
print(f"Crest Elevation: {crest_level_masl} masl")
print(f"Factor of Safety: {FoS}")

print("\n--- 1. RECTANGULAR BASE SPECIFICATIONS ---")
print(f"To ensure exact compliance for the first data point (Min flow):")
print(f" - Width (L): {W_rect:.3f} meters")
print(f" - Height   : From {crest_level_masl:.2f} to {crest_level_masl + H_1:.2f} masl")
print(f" - Design Q : {Q_1:.2f} m³/s at {crest_level_masl + H_1:.2f} masl")

print("\n--- 2. ACTUAL EXPECTED OPERATIONS ---")
print(f"{'Condition':<10} | {'Actual TWL':<12} | {'Head Loss':<10} | {'Actual Spill WL':<15} | {'Actual Flow Q':<15}")
print("-" * 75)
for i in range(len(TWL_inlet)):
    print(f"{labels[i]:<10} | {TWL_inlet_actual[i]:<12.2f} | {Head_loss[i]:<10.2f} | {WL_spillway_actual[i]:<15.2f} | {Q_target[i]:<15.2f}")

print("\n--- 3. OPTIMIZED UPPER CAD PROFILE (Above the Base) ---")
print(f"{'Spillway Elev (masl)':<22} | {'Required Width (m)':<15}")
print("-" * 40)
print(f"{crest_level_masl + H_1:<22.2f} | {W_rect:<15.2f} (Start of Curve)")
for i, w in enumerate(optimized_upper_widths):
    elev = crest_level_masl + y_opt_nodes[i+1]
    print(f"{elev:<22.2f} | {w:<15.2f}")