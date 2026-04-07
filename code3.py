import numpy as np
from scipy.optimize import minimize
import matplotlib.pyplot as plt

# --- 1. Your Given Data ---
crest_masl = 910.0
TWL = np.array([911.31, 912.41, 913.39, 914.11])
Q_target = np.array([12.5, 25.3, 50.7, 76.0])
H_target = TWL - crest_masl  # Head (m) = [1.31, 2.41, 3.39, 4.11]

# --- 2. Hydraulic Parameters ---
# Change this if your spillway type is different!
# 0.60 = Standard Sharp/Ogee crest
# 0.45 = Broad-crested/Rough crest (will make the width much wider)
Cd = 0.60  
g = 9.81   

# --- 3. Define Smooth Shape Function ---
# We use a 3rd-degree polynomial to guarantee a perfectly smooth, CAD-ready curve.
# Width(y) = p[0] + p[1]*y + p[2]*y^2 + p[3]*y^3
def width_curve(y, p):
    return p[0] + p[1]*y + p[2]*(y**2) + p[3]*(y**3)

def calc_discharge(H, p):
    """Integrates to find Q for a given water head H using the smooth curve."""
    y = np.linspace(0, H, 200)
    w_y = width_curve(y, p)
    # Ensure width doesn't mathematically go negative during optimization
    w_y = np.maximum(w_y, 0.1) 
    integrand = Cd * w_y * np.sqrt(2 * g * (H - y))
    return np.trapz(integrand, y)

def objective(p):
    """Calculates the error between the curved shape and your 4 target Qs."""
    error = 0
    for i, H in enumerate(H_target):
        Q_calc = calc_discharge(H, p)
        # Minimize the squared percentage error
        error += ((Q_calc - Q_target[i]) / Q_target[i])**2
    return error

# --- 4. Run Optimization ---
# Initial guess: A constant 5-meter wide rectangular weir
initial_guess = [5.0, 0.0, 0.0, 0.0]
print("Calculating ideal smooth geometry...")
res = minimize(objective, initial_guess, method='Nelder-Mead', options={'maxiter': 5000})
p_opt = res.x

# --- 5. Generate Plotting Data ---
y_plot = np.linspace(0, np.max(H_target), 500)
w_plot = width_curve(y_plot, p_opt)
masl_plot = y_plot + crest_masl

# --- 6. Visualization ---
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))

# --- Plot 1: Elevation View ---
# Plot weir plate structure (Gray)
ax1.fill_betweenx(masl_plot, -10, 10, color='slategray', alpha=0.3)
ax1.fill_betweenx(masl_plot, -w_plot/2, w_plot/2, color='white') # Carve out notch

# Plot water flowing through the notch (Blue)
ax1.fill_betweenx(masl_plot, -w_plot/2, w_plot/2, color='deepskyblue', alpha=0.5)

# Plot solid edges
ax1.plot(w_plot / 2, masl_plot, 'k-', lw=3)
ax1.plot(-w_plot / 2, masl_plot, 'k-', lw=3)
ax1.plot([-p_opt[0]/2, p_opt[0]/2], [crest_masl, crest_masl], 'k-', lw=3)

# Add width dimension lines and annotations at each specific TWL
labels = ['Minimum', '1 Unit', '2 Units', '3 Units']
for i, h in enumerate(H_target):
    twl = TWL[i]
    w = width_curve(h, p_opt)
    ax1.axhline(twl, color='blue', linestyle='-.', alpha=0.6)
    
    # Draw dimension line for width
    ax1.plot([-w/2, w/2], [twl, twl], 'k|--', alpha=0.5)
    ax1.text(0, twl + 0.05, f'{labels[i]}: {Q_target[i]} m³/s\nWidth = {w:.2f}m', 
             color='darkblue', fontweight='bold', ha='center', fontsize=9)

ax1.set_title(f"Optimized Weir Notch Elevation\n(Using Cd = {Cd})", fontsize=14, fontweight='bold')
ax1.set_xlabel("Notch Width (meters)", fontsize=12)
ax1.set_ylabel("Elevation (masl)", fontsize=12)
ax1.set_xlim(-6, 6)
ax1.set_ylim(crest_masl - 0.5, np.max(TWL) + 0.5)
ax1.grid(True, linestyle=':', alpha=0.6)

# --- Plot 2: Verification Curve ---
Q_curve = [calc_discharge(h, p_opt) for h in y_plot]
ax2.plot(Q_curve, masl_plot, 'b-', lw=2, label='Performance of Designed Shape')
ax2.plot(Q_target, TWL, 'ro', markersize=8, zorder=5, label='Your Target Requirements')

ax2.set_title("Head vs. Discharge Verification", fontsize=14, fontweight='bold')
ax2.set_xlabel("Discharge Q (m³/s)", fontsize=12)
ax2.set_ylabel("Water Elevation (masl)", fontsize=12)
ax2.legend(loc='lower right')
ax2.grid(True, linestyle=':', alpha=0.6)

plt.tight_layout()
plt.show()

# --- Print Dimensions for CAD ---
print("\n--- EXACT DIMENSIONS FOR CAD EXPORT ---")
print("Equation for width: W(y) = {:.3f} + {:.3f}y + {:.3f}y² + {:.3f}y³".format(*p_opt))
print(f"Base Crest (910.00 masl): Width = {p_opt[0]:.2f} m")
for i, h in enumerate(H_target):
    w = width_curve(h, p_opt)
    print(f"Elevation {TWL[i]:.2f} masl: Width = {w:.2f} m (Q = {calc_discharge(h, p_opt):.1f} m³/s)")