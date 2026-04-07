import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import PchipInterpolator

# ==========================================
# 1. USER PARAMETERS
# ==========================================
crest_level_masl = 910

# Original Operating Conditions (Given Data)
TWL_given = np.array([911.31, 912.41, 913.39, 914.11])
Q_given = np.array([12.5, 25.3, 50.7, 76.0])

# Hydraulic Constants
Cd = 0.60  # Discharge Coefficient
g = 9.81   # Gravity (m/s^2)

# ==========================================
# 2. CREATE DENSE LINEAR TARGET PROFILE
# ==========================================
# Add the 0,0 point (No head = no discharge)
H_given = TWL_given - crest_level_masl
H_full = np.insert(H_given, 0, 0.0)
Q_full = np.insert(Q_given, 0, 0.0)

# Create 50 dense points by linearly interpolating the target values
H_dense = np.linspace(0.01, np.max(H_full), 50) # Start at 0.01 to avoid div-by-zero
Q_target_dense = np.interp(H_dense, H_full, Q_full)

# ==========================================
# 3. SETUP OPTIMIZATION NODES & MATH
# ==========================================
# We use 20 evenly spaced vertical nodes to define the weir shape flexibly
num_nodes = 20
y_nodes = np.linspace(0, np.max(H_full), num_nodes)

def calc_Q_for_H(H, w_nodes, num_stripes=200):
    """Calculates Q for a specific H using explicit horizontal strip integration."""
    if H <= 0: return 0.0
    w_func = PchipInterpolator(y_nodes, w_nodes)
    y = np.linspace(0, H, num_stripes)
    dy = y[1] - y[0]
    
    # Evaluate widths, ensuring they don't mathematically dip below 0.1m
    w = np.maximum(w_func(y), 0.1)
    
    # dQ = Cd * w * dy * sqrt(2*g*head)
    velocities = np.sqrt(2 * g * (H - y))
    integrand = Cd * w * velocities
    return np.trapz(integrand, y)

def objective_function(w_nodes):
    """
    Evaluates how perfectly the current weir shape matches the entirely 
    linear interpolated Q curve, while maintaining physical smoothness.
    """
    error = 0.0
    
    # 1. Calculate Error against the dense linear target profile
    for i in range(len(H_dense)):
        Q_calc = calc_Q_for_H(H_dense[i], w_nodes)
        # Use relative squared error so small Qs and large Qs are weighted equally
        error += ((Q_calc - Q_target_dense[i]) / max(Q_target_dense[i], 1.0))**2
        
    # 2. Smoothness Penalty (Regularization)
    # Penalizes the second derivative (sharp bends/wiggles) to guarantee a smooth CAD curve
    second_derivative = np.diff(w_nodes, n=2)
    smoothness_penalty = 0.5 * np.sum(second_derivative**2)
    
    return error + smoothness_penalty

# ==========================================
# 4. RUN OPTIMIZATION
# ==========================================
# Because forcing linear Q near H=0 requires a very wide base, we allow width up to 30m
bounds = [(0.1, 30.0) for _ in range(num_nodes)]

# Initial guess: A shape that starts wide and narrows (closer to the true physics)
initial_widths = np.linspace(10.0, 3.0, num_nodes)

print("Optimizing weir profile to match linearly interpolated discharge curve...")
result = minimize(objective_function, initial_widths, bounds=bounds, method='L-BFGS-B', options={'maxiter': 2000})
optimized_w_nodes = result.x

# ==========================================
# 5. GENERATE DATA FOR PLOTTING
# ==========================================
smooth_y = np.linspace(0, np.max(H_full), 500)
w_func_final = PchipInterpolator(y_nodes, optimized_w_nodes)
smooth_w = w_func_final(smooth_y)
smooth_masl = smooth_y + crest_level_masl

# Calculate actual achieved continuous discharge to prove it worked
Q_achieved_continuous = [calc_Q_for_H(h, optimized_w_nodes) for h in smooth_y]

# Calculate adaptive bounds for clean plotting
max_w = np.max(smooth_w)
max_masl = np.max(TWL_given)

# ==========================================
# 6. VISUALIZATION
# ==========================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 7))

# --- PLOT 1: ELEVATION VIEW ---
ax1.fill_betweenx(smooth_masl, -max_w*1.2, max_w*1.2, color='slategray', alpha=0.3)
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='white')
ax1.fill_betweenx(smooth_masl, -smooth_w/2, smooth_w/2, color='deepskyblue', alpha=0.5)

# Solid notch edges
ax1.plot(smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot(-smooth_w/2, smooth_masl, 'k-', lw=3)
ax1.plot([-optimized_w_nodes[0]/2, optimized_w_nodes[0]/2], [crest_level_masl, crest_level_masl], 'k-', lw=3)

# Mark the Original Target Points
for i in range(len(TWL_given)):
    ax1.axhline(TWL_given[i], color='blue', linestyle='-.', alpha=0.6)
    w_at_target = w_func_final(H_given[i])
    ax1.plot([-w_at_target/2, w_at_target/2], [TWL_given[i], TWL_given[i]], 'k|--', alpha=0.4)
    ax1.text(0, TWL_given[i] + 0.05, f'Q={Q_given[i]} | W={w_at_target:.2f}m', 
             color='darkblue', fontweight='bold', ha='center', fontsize=9)

ax1.set_title("Elevation Profile (20-Node Interpolation)", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Elevation (masl)", fontsize=12)
ax1.set_xlim(-max_w * 0.6, max_w * 0.6)
ax1.set_ylim(crest_level_masl - 0.5, max_masl + 0.5)
ax1.grid(True, linestyle=':', alpha=0.6)

# --- PLOT 2: DISCHARGE CURVE VERIFICATION ---
# Plot the linear requirement (what the algorithm was trying to trace)
ax2.plot(Q_full, H_full + crest_level_masl, 'r--', lw=3, alpha=0.6, label='Required Linear Profile')
# Plot what the custom shape actually achieved
ax2.plot(Q_achieved_continuous, smooth_masl, 'b-', lw=2, label='Actual Profile Achieved')
# Plot original points
ax2.plot(Q_given, TWL_given, 'ro', markersize=8, markeredgecolor='black', zorder=5, label='Original Data Points')

ax2.set_title("Target vs Achieved Discharge Profile", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# ==========================================
# 7. PRINT KEY DIMENSIONS
# ==========================================
print("\n--- OPTIMIZED PROFILE DIMENSIONS ---")
print("Note: The base is wide to enforce linear Q scaling at very low heads.")
print(f"{'Elevation (masl)':<18} | {'Width (m)':<12}")
print("-" * 35)
for i in range(num_nodes):
    print(f"{crest_level_masl + y_nodes[i]:<18.2f} | {optimized_w_nodes[i]:<12.2f}")