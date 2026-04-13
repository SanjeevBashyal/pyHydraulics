import numpy as np
import matplotlib.pyplot as plt

# --- Parameters for the Notch ---
k = 2.0             # Shape constant (determines how wide the notch is)
y_min = 0.2         # Base truncation to avoid infinite width at y=0
y_max = 4.0         # Total height of the notch opening
plate_width = 12.0  # Total width of the weir plate
plate_height = 5.0  # Total height of the weir plate

# --- Calculate Notch Geometry ---
# Generate vertical y coordinates for the curved part of the notch
y_curve = np.linspace(y_min, y_max, 500)

# Calculate the width x at each height y (x = k / y)
# We divide by 2 because the notch is symmetrical around the y-axis
x_right = (k / y_curve) / 2
x_left = -x_right

# --- Visualization Setup ---
fig, ax = plt.subplots(figsize=(10, 7))

# 1. Draw the solid Weir Plate (Gray area)
# Fill right side of the plate
ax.fill_betweenx(y_curve, x_right, plate_width/2, color='slategray', alpha=0.7, hatch='//')
# Fill left side of the plate
ax.fill_betweenx(y_curve, -plate_width/2, x_left, color='slategray', alpha=0.7, hatch='//')
# Fill the solid base below y_min
ax.fill_betweenx([0, y_min], -plate_width/2, plate_width/2, color='slategray', alpha=0.7, hatch='//')
# Fill the solid area above the notch
ax.fill_betweenx([y_max, plate_height], -plate_width/2, plate_width/2, color='slategray', alpha=0.7, hatch='//')

# 2. Draw the Notch Edges (Thick black lines)
ax.plot(x_right, y_curve, 'k-', linewidth=3, label='Notch Profile ($x \propto 1/y$)')
ax.plot(x_left, y_curve, 'k-', linewidth=3)
ax.plot([x_left[0], x_right[0]], [y_min, y_min], 'k-', linewidth=3) # Bottom edge

# 3. Simulate Water Level
H_water = 2.5  # Current water depth
y_water = np.linspace(y_min, H_water, 200)
x_water_right = (k / y_water) / 2
x_water_left = -x_water_right

# Fill water inside the notch
ax.fill_betweenx(y_water, x_water_left, x_water_right, color='deepskyblue', alpha=0.5, label='Water Flow')

# Draw water surface line
ax.plot([-plate_width/2.2, plate_width/2.2], [H_water, H_water], color='blue', linestyle='-.', linewidth=1.5)
ax.text(plate_width/3, H_water + 0.1, f'Water Level (H={H_water})', color='blue', fontweight='bold')

# --- Formatting the Plot ---
ax.set_title("Elevation View of a Hyperbolic Notch\nDischarge $Q \propto \sqrt{H}$", fontsize=14, fontweight='bold')
ax.set_xlabel("Width ($x$)", fontsize=12)
ax.set_ylabel("Height ($y$)", fontsize=12)

# Set axis limits
ax.set_xlim(-plate_width/2, plate_width/2)
ax.set_ylim(0, plate_height)

# Add grid and legend
ax.grid(True, linestyle=':', alpha=0.6)
ax.legend(loc='upper right')

# Make axes symmetric and clean
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

plt.tight_layout()
plt.show()