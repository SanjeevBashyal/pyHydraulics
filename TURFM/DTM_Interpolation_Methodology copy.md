# DTM Interpolation Methodology for 2D HEC-RAS Terrain Preparation

## Purpose

This note summarizes the computational procedure used in the DTM preprocessing workflow implemented in `DTM.py` and called through `callDTM.py`.

The objective is to modify the raw terrain model so that:

1. surveyed channel cross-sections are honored inside the river corridor,
2. the modified channel transitions smoothly back to the original terrain near the banks,
3. tributaries and junctions can be handled consistently on a common terrain window, and
4. the final terrain is suitable for 2D hydraulic modeling in HEC-RAS.

---

## Notation

| Symbol | Meaning |
| --- | --- |
| $Z_{\text{DTM}}(x,y)$ | original terrain elevation at location $(x,y)$ |
| $Z_{\text{final}}(x,y)$ | modified terrain elevation written to output raster |
| $B_L,\ B_R$ | left and right bank polylines |
| $C(s)$ | centerline parameterized by chainage $s$ |
| $S_k$ | surveyed cross-section at station $k$ |
| $L_k$ | total length of surveyed cross-section $S_k$ |
| $d(\cdot,\cdot)$ | Euclidean distance between geometries |
| $\operatorname{proj}_{\Gamma}(p)$ | distance along geometry $\Gamma$ to the orthogonal projection of point $p$ |
| $b$ | processing buffer around surveyed data |
| $\Delta$ | target raster resolution |
| $w(s)$ | channel width at centerline chainage $s$ |
| $r(p)$ | transverse distance of cell $p$ from the centerline |
| $\tau_J$ | junction detection tolerance |

---

## 1. Terrain Window Extraction and Resampling

For a channel, the surveyed cross-section coordinates define the processing window.

If the set of all surveyed points is $\{(x_i,y_i,z_i)\}_{i=1}^N$, then the terrain window is:

$$
\Omega
=
\left[
\min_i(x_i)-b,\;
\min_i(y_i)-b,\;
\max_i(x_i)+b,\;
\max_i(y_i)+b
\right]
$$

The corresponding raster subset is cropped from the master DTM and resampled to the target resolution $\Delta$.

If the physical window dimensions are $W$ and $H$, then:

$$
n_x = \left\lfloor \frac{W}{\Delta} \right\rfloor,
\qquad
n_y = \left\lfloor \frac{H}{\Delta} \right\rfloor
$$

and bilinear resampling is used to generate the working terrain grid.

---

## 2. Centerline Generation from the Bank Lines

The workflow does not assume a predefined centerline. Instead, the centerline is constructed geometrically from the two banks.

Let the two bank lines be $B_1$ and $B_2$. A point is sampled along the first bank:

$$
p_A(s) \in B_1
$$

and the nearest point on the opposite bank is found:

$$
p_B(s) = \operatorname{nearest}\!\left(p_A(s), B_2\right)
$$

The centerline point is then solved as the point on the segment joining $p_A$ and $p_B$ that is equidistant from both banks:

$$
c(s,t) = p_A(s) + t\left(p_B(s)-p_A(s)\right), \qquad t \in [0,1]
$$

with

$$
t^\ast
=
\arg\min_{t \in [0,1]}
\left|
d\!\left(c(s,t), B_1\right) - d\!\left(c(s,t), B_2\right)
\right|
$$

Therefore, the centerline point is:

$$
C(s) = c(s,t^\ast)
$$

In implementation, $t^\ast$ is solved by binary search.

---

## 3. Channel Width Along the Centerline

At each centerline vertex $C_j$, the local bank-to-bank width is computed as:

$$
w_j = d(C_j, B_L) + d(C_j, B_R)
$$

These widths are then interpolated along centerline chainage.

For any cell $p$, let

$$
s(p) = \operatorname{proj}_{C}(p)
$$

Then the nearest centerline point is:

$$
C_p = C\!\left(s(p)\right)
$$

and the local channel width is:

$$
w(p) = \operatorname{interp}\!\left(s(p);\; \{(s_j,w_j)\}\right)
$$

The transverse distance from the cell to the centerline is:

$$
r(p) = \|p - C_p\|
$$

---

## 4. Cross-Section Descriptors

Each surveyed cross-section $S_k$ is represented as a 3D polyline with chainage-dependent elevation:

$$
S_k(\xi) = \left(x_k(\xi), y_k(\xi), z_k(\xi)\right),
\qquad
\xi \in [0,L_k]
$$

The cross-section is anchored to the centerline by the closest point between the section and the centerline:

$$
q_k = \operatorname{nearest}(S_k, C)
$$

The centerline chainage of that section is:

$$
s_k = \operatorname{proj}_{C}(q_k)
$$

The corresponding distance along the section is:

$$
d_k^C = \operatorname{proj}_{S_k}(q_k)
$$

This splits the cross-section into left and right half-widths:

$$
\ell_k = d_k^C,
\qquad
r_k = L_k - d_k^C
$$

The elevation along the section is interpolated linearly from the surveyed points:

$$
z_k(\xi) = \operatorname{interp}\!\left(\xi;\; \text{surveyed vertices of }S_k\right)
$$

The bank-to-bank width of the section, evaluated at its centerline intersection, is:

$$
w_k^\ast = w(s_k)
$$

---

## 5. Dynamic Cross-Section Envelope Polygon

The outer interpolation envelope is not taken as the bank polygon itself. Instead, a dynamic polygon is created from interpolated cross-section widths.

For any centerline chainage $s$:

$$
\ell(s) = \operatorname{interp}\!\left(s;\{(s_k,\ell_k)\}\right),
\qquad
r(s) = \operatorname{interp}\!\left(s;\{(s_k,r_k)\}\right)
$$

Let $\mathbf{t}(s)$ be the local unit tangent and $\mathbf{n}(s)$ the corresponding unit normal:

$$
\mathbf{n}(s)
=
\frac{1}{\|\mathbf{t}(s)\|}
\begin{bmatrix}
-t_y(s) \\
t_x(s)
\end{bmatrix}
$$

Then the left and right envelope boundaries are:

$$
P_L(s) = C(s) + \ell(s)\,\mathbf{n}(s)
$$

$$
P_R(s) = C(s) - r(s)\,\mathbf{n}(s)
$$

The interpolation polygon is the closed boundary formed by:

$$
\partial \Omega_{\text{XS}}
=
\left\{P_L(s)\right\}_{s=0}^{L_C}
\cup
\left\{P_R(s)\right\}_{s=L_C}^{0}
$$

where $L_C$ is total centerline length.

This polygon defines the outer zone over which terrain blending is allowed.

---

## 6. Core Cellwise Interpolation for a Single Channel

For each raster cell $p=(x,y)$ inside the interpolation envelope:

1. project the cell to the centerline,
2. identify the two surrounding surveyed sections,
3. interpolate the channel elevation from those sections,
4. blend that interpolated elevation with the original terrain.

### 6.1 Bracketing Cross-Sections

For each cell, compute centerline chainage:

$$
s(p) = \operatorname{proj}_{C}(p)
$$

Then select the upstream and downstream surveyed sections such that:

$$
s_k \le s(p) < s_{k+1}
$$

### 6.2 Distance from the Cell to the Bracketing Sections

For the bracketing sections $S_k$ and $S_{k+1}$:

$$
\delta_k(p) = d\!\left(p, S_k\right),
\qquad
\delta_{k+1}(p) = d\!\left(p, S_{k+1}\right)
$$

These distances control the longitudinal interpolation weight.

### 6.3 Mapping the Cell Offset onto Each Cross-Section

The cell is first measured relative to the centerline:

$$
r(p) = \|p - C_p\|
$$

Because the local channel width may vary, this transverse offset is scaled to each section:

$$
m_k(p) = r(p)\,\frac{w_k^\ast}{w(p)}
$$

$$
m_{k+1}(p) = r(p)\,\frac{w_{k+1}^\ast}{w(p)}
$$

The side of the section is determined from the projected cell location on the section:

$$
\sigma_k(p)
=
\begin{cases}
+1, & \operatorname{proj}_{S_k}(p) \ge d_k^C \\
-1, & \operatorname{proj}_{S_k}(p) < d_k^C
\end{cases}
$$

$$
\sigma_{k+1}(p)
=
\begin{cases}
+1, & \operatorname{proj}_{S_{k+1}}(p) \ge d_{k+1}^C \\
-1, & \operatorname{proj}_{S_{k+1}}(p) < d_{k+1}^C
\end{cases}
$$

Therefore, the sampled elevations from the two sections are:

$$
\hat z_k(p)
=
z_k\!\left(d_k^C + \sigma_k(p)\,m_k(p)\right)
$$

$$
\hat z_{k+1}(p)
=
z_{k+1}\!\left(d_{k+1}^C + \sigma_{k+1}(p)\,m_{k+1}(p)\right)
$$

### 6.4 Longitudinal Blending Between the Two Sections

The longitudinal interpolation weights are inverse-distance based:

$$
\alpha_k(p)
=
\frac{\delta_{k+1}(p)}{\delta_k(p)+\delta_{k+1}(p)}
$$

$$
\alpha_{k+1}(p)
=
\frac{\delta_k(p)}{\delta_k(p)+\delta_{k+1}(p)}
$$

Hence the cross-section-derived elevation is:

$$
Z_{\text{XS}}(p)
=
\alpha_k(p)\,\hat z_k(p)
+
\alpha_{k+1}(p)\,\hat z_{k+1}(p)
$$

This means the cell is pulled more strongly toward whichever surveyed section is closer.

---

## 7. Blending the Interpolated Channel with the Original Terrain

Two raster masks are used:

1. the **bank polygon mask**, which represents the core channel region,
2. the **cross-section envelope mask**, which represents the outer blending zone.

Let:

- $d_B(p)$ be the distance from cell $p$ to the bank polygon boundary,
- $d_X(p)$ be the distance from cell $p$ to the outer cross-section envelope boundary.

The terrain weight is:

$$
\beta_{\text{terrain}}(p)
=
\frac{d_B(p)}{d_B(p)+d_X(p)}
$$

and the channel-interpolation weight is:

$$
\beta_{\text{XS}}(p) = 1 - \beta_{\text{terrain}}(p)
$$

Hence:

- inside the bank polygon core, $d_B(p)=0$ and the surveyed channel surface dominates,
- near the outer cross-section envelope boundary, $d_X(p)\to 0$ and the raw terrain dominates.

So the final modified elevation for a single channel is:

$$
Z_{\text{final}}(p)
=
\beta_{\text{terrain}}(p)\,Z_{\text{DTM}}(p)
+
\beta_{\text{XS}}(p)\,Z_{\text{XS}}(p)
$$

### Exponential Blend Option

If exponential blending is selected, the terrain weight is transformed as:

$$
\beta_{\text{terrain}}^{(\exp)}(p)
=
\frac{e^{\beta_{\text{terrain}}(p)} - 1}{e - 1}
$$

Then:

$$
Z_{\text{final}}(p)
=
\beta_{\text{terrain}}^{(\exp)}(p)\,Z_{\text{DTM}}(p)
+
\left(1-\beta_{\text{terrain}}^{(\exp)}(p)\right)\,Z_{\text{XS}}(p)
$$

This produces a softer fade from the channel back into the original terrain.

---

## 8. Junction Detection for River Networks

For a project with multiple sub-projects, each sub-project is treated as one channel candidate.

For a tributary centerline endpoint $e$ and a main centerline $C_m$:

$$
s_m(e) = \operatorname{proj}_{C_m}(e)
$$

$$
j(e) = C_m\!\left(s_m(e)\right)
$$

$$
\eta(e) = \frac{s_m(e)}{L_m}
$$

$$
d_J(e) = \|e - j(e)\|
$$

where $L_m$ is the main channel centerline length.

A valid junction candidate must satisfy:

$$
0.05 \le \eta(e) \le 0.95
$$

and

$$
d_J(e) \le \tau_J
$$

This ensures the tributary connects to the **middle reach** of another river, not simply near its upstream or downstream end.

Among all pairwise candidates, the smallest valid endpoint-to-centerline distance is selected.

---

## 9. Tributary Bank Extension at the Junction

Once a junction point is detected, the tributary bank lines are extended to meet the main channel banks.

Let the tributary bank endpoints nearest the junction be $e_1$ and $e_2$, and let the two main channel banks be $B_1^m$ and $B_2^m$.

Two possible assignments exist:

$$
A_1 = (e_1 \rightarrow B_1^m,\; e_2 \rightarrow B_2^m)
$$

$$
A_2 = (e_1 \rightarrow B_2^m,\; e_2 \rightarrow B_1^m)
$$

The chosen assignment is:

$$
A^\ast
=
\arg\min_{A \in \{A_1,A_2\}}
\sum_{i=1}^{2}
d\!\left(e_i,\; B_{A(i)}^m\right)
$$

After selecting the best assignment, each tributary bank is extended to the nearest point on its assigned main bank.

This creates a more continuous bank geometry through the junction before interpolation is applied.

---

## 10. Shared Raster Window for Multi-Channel Processing

For a project containing multiple channels, all channels are processed on the same cropped terrain window.

If channel $q$ has surveyed coordinate bounds:

$$
\left[x_{\min}^{(q)}, y_{\min}^{(q)}, x_{\max}^{(q)}, y_{\max}^{(q)}\right]
$$

then the shared network window is:

$$
\Omega_{\text{net}}
=
\left[
\min_q x_{\min}^{(q)} - b,\;
\min_q y_{\min}^{(q)} - b,\;
\max_q x_{\max}^{(q)} + b,\;
\max_q y_{\max}^{(q)} + b
\right]
$$

This guarantees that:

1. all channels are interpolated on identical raster alignment,
2. cellwise merging is mathematically valid,
3. junction regions are represented without raster mismatch.

---

## 11. Final Multi-Channel Merge

After each channel $q$ is interpolated independently on the shared raster window, the final channel terrain is obtained by taking the minimum elevation per cell:

$$
Z_{\text{net}}(p)
=
\min_{q=1,\dots,Q} Z_q(p)
$$

where:

- $Q$ is the number of channels in the project,
- $Z_q(p)$ is the single-channel interpolated terrain for channel $q$.

This minimum-elevation merge is physically useful because, in overlap regions near junctions, the hydraulically controlling surface is the deepest locally valid channel representation.

In implementation, `nodata` cells are ignored during the minimum operation.

---

## 12. Final Outputs

For each project, the workflow produces:

1. a final terrain GeoTIFF for the full river system,
2. optional intermediate channel-specific GeoTIFFs,
3. centerline shapefiles,
4. merged bank shapefiles,
5. a study perimeter polygon.

These outputs are written under the project-specific DTM folder inside the HEC-RAS project directory.

---

## 13. Compact Algorithm Summary

### Single Channel

1. Read surveyed cross-sections and bank lines.
2. Crop and resample the base DTM.
3. Generate an equidistant centerline from the banks.
4. Build a dynamic interpolation envelope from cross-section widths.
5. For each raster cell in the envelope:
   1. project to centerline,
   2. identify the two bracketing sections,
   3. map the transverse offset to each section,
   4. interpolate section elevation,
   5. blend with original terrain.

### Multi-Channel / Junction Case

1. Build centerlines for all sub-projects.
2. Detect tributary-to-main junction candidates.
3. Extend tributary banks to the main banks.
4. Compute one shared raster window covering the full river system.
5. Run single-channel interpolation on that shared grid for each sub-project.
6. Merge the resulting rasters by cellwise minimum elevation.

---

## 14. Summary

The method can be summarized conceptually as:

> **Surveyed cross-sections control the channel interior, the original terrain controls the outer floodplain, and a distance-based blending scheme creates a smooth transition between them.**  
>  
> For river systems with tributaries, all channels are first brought into a shared geometric and raster framework, then combined in the junction area using the minimum physically admissible bed elevation.

---