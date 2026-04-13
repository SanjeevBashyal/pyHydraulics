import numpy as np
from scipy.optimize import minimize
import matplotlib.pyplot as plt

# --- 1. Define the Given Data ---
crest_masl = 910.25
TWL = np.array([911.31, 912.41, 913.39, 914.11])
Q_target = np.array([12.5, 25.3, 50.7, 76.0])
H_target = TWL - crest_masl  # Water head above crest

# Hydraulic constants
Cd = 0.60  # Typical Discharge Coefficient for sharp/stepped crests
g = 9.81   # Gravity (m/s^2)

# Define vertical calculation nodes: Crest + the 4 target water levels
y_knots = np.concatenate(([0], H_target)) 

# --- 2. Define Hydraulic Integration ---
def weir_discharge(H, w_nodes):
    """Calculates discharge Q for a given water head H and a notch width profile."""
    # Create 200 horizontal strips to numerically integrate the flow
    y = np.linspace(0, H, 200)
    # Interpolate the width of the notch at each vertical strip
    w_y = np.interp(y, y_knots, w_nodes)
    
    # Q = Integral( Cd * width * sqrt(2g(H-y)) dy )
    integrand = Cd * w_y * np.sqrt(2 * g * (H - y))
    return np.trapz(integrand, y)

# --- 3. Optimization Algorithm ---
def objective_function(w_nodes):
    """Calculates the error between the generated notch shape and your target Qs."""
    error = 0
    for i, H in enumerate(H_target):
        Q_calc = weir_discharge(H, w_nodes)
        # Minimize the squared percentage error
        error += ((Q_calc - Q_target[i]) / Q_target[i])**2
    
    # Add a small smoothing penalty to prevent zigzagging shapes
    error += 0.02 * np.sum(np.diff(w_nodes)**2)
    return error

# Initial guess: A constant 5-meter wide rectangular weir
w_initial = np.ones(len(y_knots)) * 5.0
# Constraints: The notch cannot be narrower than 0.5m or wider than 20m
bounds = [(0.5, 20.0) for _ in range(len(y_knots))]

print("Optimizing weir shape to match your exact discharge points...")
result = minimize(objective_function, w_initial, bounds=bounds, method='SLSQP')
w_opt = result.x  # The optimally calculated widths

# --- 4. Plotting the Results ---
# Generate dense points for a smooth visual curve
y_dense = np.linspace(0, np.max(H_target), 500)
w_dense = np.interp(y_dense, y_knots, w_opt)
masl_dense = y_dense + crest_masl

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

# Plot 1: Elevation View of the Notch
ax1.plot(w_dense / 2, masl_dense, 'k-', lw=3)   # Right edge
ax1.plot(-w_dense / 2, masl_dense, 'k-', lw=3)  # Left edge
ax1.plot([-w_opt[0]/2, w_opt[0]/2], [crest_masl, crest_masl], 'k-', lw=3) # Crest bottom
ax1.fill_betweenx(masl_dense, -w_dense/2, w_dense/2, color='deepskyblue', alpha=0.4)

# Draw Target Water Levels
labels = ['Minimum', '1 Unit', '2 Units', '3 Units']
for i, twl in enumerate(TWL):
    ax1.axhline(twl, color='blue', linestyle='-.', alpha=0.6)
    ax1.text(0, twl + 0.05, f'{labels[i]}: {Q_target[i]} m³/s', 
             color='darkblue', fontweight='bold', ha='center')

ax1.set_title("Elevation View of Optimized Notch", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (m)", fontsize=12)
ax1.set_ylabel("Elevation (masl)", fontsize=12)
ax1.set_xlim(-np.max(w_opt), np.max(w_opt))
ax1.set_ylim(crest_masl - 0.5, np.max(TWL) + 0.5)
ax1.grid(True, linestyle=':', alpha=0.6)

# Plot 2: Performance Verification (Q vs Elevation)
Q_curve = [weir_discharge(h, w_opt) for h in y_dense]
ax2.plot(Q_curve, masl_dense, 'b-', lw=2, label='Actual Performance of this Notch')
ax2.plot(Q_target, TWL, 'ro', markersize=8, label='Your Target Operating Points')

ax2.set_title("Performance Verification Curve", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge $Q$ (m³/s)", fontsize=12)
ax2.set_ylabel("Total Water Level (masl)", fontsize=12)
ax2.grid(True, linestyle=':', alpha=0.6)
ax2.legend()

plt.tight_layout()
plt.show()

# Print the exact width dimensions required at each elevation
print("\n--- Required Notch Dimensions ---")
for masl, width in zip(y_knots + crest_masl, w_opt):
    print(f"Elevation: {masl:.2f} masl --> Required Width: {width:.2f} meters")