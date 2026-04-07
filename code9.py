import numpy as np
import matplotlib.pyplot as plt

# Handle NumPy 2.0 deprecation gracefully
try:
    from numpy import trapezoid
except ImportError:
    from numpy import trapz as trapezoid

# ==========================================
# 1. USER PARAMETERS & SAFETY FACTORS
# ==========================================
crest_level_masl = 910.5
FoS = 1.2  

# Given Data (Upstream Conditions)
TWL_inlet = np.array([911.31, 912.41, 913.39, 914.11])
Q_target  = np.array([12.5, 25.3, 50.7, 76.0])
Head_loss = np.array([0.10, 0.15, 0.20, 0.25])

Cd = 0.60  
g = 9.81   

# ==========================================
# 2. CALCULATE LOCAL SPILLWAY CONDITIONS
# ==========================================
Q_design = Q_target * FoS
WL_spillway_design = TWL_inlet - Head_loss
H_eff = WL_spillway_design - crest_level_masl

H_1 = H_eff[0]
Q_1 = Q_design[0]

# ==========================================
# 3. EXACT RECTANGULAR BASE CALCULATION
# ==========================================
W_rect = Q_1 / ((2/3) * Cd * np.sqrt(2 * g) * (H_1**1.5))

def Q_from_base(h):
    if h <= H_1:
        return (2/3) * Cd * np.sqrt(2 * g) * W_rect * (h**1.5)
    else:
        return (2/3) * Cd * np.sqrt(2 * g) * W_rect * (h**1.5 - (h - H_1)**1.5)

# ==========================================
# 4. VOLTERRA INTEGRAL EXACT DIRECT SOLVER
# ==========================================
num_nodes = 200
y_nodes = np.linspace(H_1, H_eff[-1], num_nodes)

Q_target_nodes = np.interp(y_nodes, H_eff, Q_design)
Q_required_from_upper = Q_target_nodes - np.array([Q_from_base(h) for h in y_nodes])
B_nodes = Q_required_from_upper / (Cd * np.sqrt(2 * g))

x_upper = np.zeros(num_nodes)
x_upper[0] = 0.0  

for i in range(1, num_nodes):
    hi = y_nodes[i]
    known_integral = 0.0
    
    for j in range(1, i):
        dy_j = y_nodes[j] - y_nodes[j-1]
        u0 = hi - y_nodes[j]
        u1 = hi - y_nodes[j-1]
        
        IL = (0.4 * (u1**2.5 - u0**2.5) - (2/3) * u0 * (u1**1.5 - u0**1.5)) / dy_j
        IR = ((2/3) * u1 * (u1**1.5 - u0**1.5) - 0.4 * (u1**2.5 - u0**2.5)) / dy_j
        
        known_integral += x_upper[j-1] * IL + x_upper[j] * IR
        
    dy_i = y_nodes[i] - y_nodes[i-1]
    IL_ii = 0.4 * (dy_i**1.5)
    IR_ii = (4/15) * (dy_i**1.5)
    
    known_integral += x_upper[i-1] * IL_ii
    
    x_exact = (B_nodes[i] - known_integral) / IR_ii
    x_upper[i] = max(x_exact, 0.0)

# ==========================================
# 5. POST-CALCULATION & UNIFIED ARRAYS
# ==========================================
# Create a single, unified set of arrays for perfectly matched plotting
y_base = np.linspace(0, H_1, 50)
y_upper = y_nodes[1:]  # Skip the first element to avoid duplicating H_1

full_y = np.concatenate((y_base, y_upper))
full_w = np.concatenate((np.full_like(y_base, W_rect), x_upper[1:]))

Q_achieved = np.zeros_like(full_y)
Q_target_plot = np.zeros_like(full_y)

for idx, h in enumerate(full_y):
    # Set the mathematical target at this height
    if h <= H_1:
        Q_target_plot[idx] = Q_from_base(h) # Base follows H^1.5 curve exactly
    else:
        Q_target_plot[idx] = np.interp(h, H_eff, Q_design) # Linear interpolation target

    # Calculate actual achieved Q
    if h == 0:
        Q_achieved[idx] = 0.0
    elif h <= H_1:
        Q_achieved[idx] = Q_from_base(h)
    else:
        y_upper_mask = (y_nodes <= h)
        y_int = y_nodes[y_upper_mask]
        w_int = x_upper[y_upper_mask]
        
        integrand = w_int * np.sqrt(h - y_int)
        if len(y_int) > 1:
            Q_upper = Cd * np.sqrt(2 * g) * trapezoid(integrand, y_int)
        else:
            Q_upper = 0.0
        Q_achieved[idx] = Q_from_base(h) + Q_upper

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))

full_masl = full_y + crest_level_masl
max_w = W_rect

# --- PLOT 1: EXACT ELEVATION GEOMETRY ---
ax1.fill_betweenx(full_masl, -max_w*1.2, max_w*1.2, color='slategray', alpha=0.3)
ax1.fill_betweenx(full_masl, -full_w/2, full_w/2, color='white')
ax1.fill_betweenx(full_masl, -full_w/2, full_w/2, color='deepskyblue', alpha=0.6)

# Physical Structure Edges
ax1.plot(full_w/2, full_masl, 'k-', lw=3)
ax1.plot(-full_w/2, full_masl, 'k-', lw=3)
ax1.plot([-W_rect/2, W_rect/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)
# Rectangular transition line
ax1.plot([-W_rect/2, W_rect/2], [crest_level_masl + H_1, crest_level_masl + H_1], 'k--', lw=2)

ax1.text(0, crest_level_masl + H_1/2, f"RECTANGULAR BASE\nWidth = {W_rect:.2f}m", ha='center', va='center', fontweight='bold')
ax1.text(0, crest_level_masl + H_1 + 1.0, "EXACT WIDTH = 0m\n(Base passes\ntoo much water)", ha='center', va='center', color='red', fontweight='bold')

for i in range(len(WL_spillway_design)):
    ax1.axhline(WL_spillway_design[i], color='red', linestyle=':', alpha=0.6)
    ax1.text(max_w*0.55, WL_spillway_design[i] + 0.03, f"DESIGN: {Q_design[i]:.1f} m³/s", color='darkred', fontweight='bold', ha='right', fontsize=9)

ax1.set_title(f"Exact Analytical Elevation Profile", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Spillway Elevation (masl)", fontsize=12)
ax1.set_xlim(-max_w * 0.7, max_w * 0.7)
ax1.set_ylim(crest_level_masl - 0.2, np.max(WL_spillway_design) + 0.3)
ax1.grid(True, linestyle=':', alpha=0.6)

# --- PLOT 2: PERFORMANCE CURVE CONTRADICTION ---
ax2.plot(Q_target_plot, full_masl, 'r--', lw=3, label='Requested Target (Linear post H1)')
ax2.plot(Q_achieved, full_masl, 'b-', lw=2, label='Achieved (Clamped at 0m upper width)')

ax2.plot(Q_design, WL_spillway_design, 'ro', markersize=8, markeredgecolor='black', label='Design Checkpoints')

# Fill error area showing exactly where and why the physical overshoot occurs
ax2.fill_betweenx(full_masl, Q_target_plot, Q_achieved, color='red', alpha=0.2, label='Physical Overshoot Error')

ax2.set_title("Exact Mathematical Discharge Verification", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Spillway Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT REPORT
# ==========================================
print("\n" + "="*80)
print(" "*25 + "EXACT ANALYTICAL REPORT")
print("="*80)
print(f"Base Width (W_rect) required at {H_1:.2f}m head: {W_rect:.2f} meters")

print("\n--- PERFORMANCE PARADOX DATA ---")
print("Elevation | Target Q req. | Actual Base Q | Required Exact Upper Width")
print("-" * 75)
indices_to_print = [0, 50, 100, 150, 199]
for idx in indices_to_print:
    elev = crest_level_masl + y_nodes[idx]
    req_q = Q_target_nodes[idx]
    base_q = Q_from_base(y_nodes[idx])
    width = x_upper[idx]
    
    if B_nodes[idx] < 0:
        math_status = "(Math required Negative Width)"
    else:
        math_status = ""
        
    print(f"{elev:<9.2f} | {req_q:<13.1f} | {base_q:<13.1f} | {width:.2f}m {math_status}")