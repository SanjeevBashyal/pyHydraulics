import numpy as np

def solve_st_venant(
    L=1000.0, B=10.0, n_manning=0.03, S0=0.001,
    Nx=101, T=3600, dt=1.0,
    Q_inflow=50.0, h_downstream_final=1.0,
    t_change_duration=300.0
):
    """
    Solves the 1D St. Venant equations using the MacCormack scheme.

    Args:
        L (float): Channel length [m]
        B (float): Channel width [m]
        n_manning (float): Manning's roughness coefficient
        S0 (float): Bed slope [m/m]
        Nx (int): Number of spatial nodes
        T (float): Total simulation time [s]
        dt (float): Time step [s]
        Q_inflow (float): Constant upstream inflow [m^3/s]
        h_downstream_final (float): Final downstream water depth [m]
        t_change_duration (float): Duration over which downstream depth changes [s]

    Returns:
        tuple: A tuple containing (t_results, x_grid, h_results, Q_results)
               - t_results: List of time points for the stored results.
               - x_grid: The spatial grid coordinates.
               - h_results: List of water depth arrays at each time point.
               - Q_results: List of discharge arrays at each time point.
    """
    # --- Setup ---
    g = 9.81
    dx = L / (Nx - 1)
    x = np.linspace(0, L, Nx)
    Nt = int(T / dt)

    # --- Initial Conditions ---
    h_normal = (Q_inflow * n_manning / (B * np.sqrt(S0)))**(3/5)
    h = np.full(Nx, h_normal)
    Q = np.full(Nx, Q_inflow)

    # --- Boundary Condition Function ---
    def get_downstream_h(t):
        if t < t_change_duration:
            return h_normal - (h_normal - h_downstream_final) * (t / t_change_duration)
        else:
            return h_downstream_final

    # --- Helper Function ---
    def update_hydraulics(h, Q):
        # Suppress warnings for this internal function
        with np.errstate(divide='ignore', invalid='ignore'):
            A = B * h
            P = B + 2 * h
            R = A / P
            u = Q / A
            Sf = np.zeros_like(h)
            non_zero_A = A > 1e-6
            Sf[non_zero_A] = (n_manning**2 * Q[non_zero_A]**2) / (A[non_zero_A]**2 * R[non_zero_A]**(4/3))
        return A, u, Sf

    # --- Result Storage ---
    h_results, Q_results, t_results = [h.copy()], [Q.copy()], [0.0]

    # --- Main Time Loop ---
    for n in range(Nt):
        t = (n + 1) * dt
        
        A, u, _ = update_hydraulics(h, Q)
        c = np.sqrt(g * h)
        Cr = (np.abs(u) + c) * dt / dx
        if np.max(Cr) >= 1.0:
            print(f"CFL condition violated at t={t:.2f} s. Max Cr = {np.max(Cr):.2f}. Halting.")
            break

        U1, U2 = A, Q
        A, u, Sf = update_hydraulics(h, Q)
        F1 = Q
        F2 = Q**2 / A + g * A**2 / (2 * B)
        S2 = g * A * (S0 - Sf)

        U1_star, U2_star = np.zeros_like(U1), np.zeros_like(U2)
        for i in range(1, Nx - 1):
            U1_star[i] = U1[i] - (dt / dx) * (F1[i+1] - F1[i])
            U2_star[i] = U2[i] - (dt / dx) * (F2[i+1] - F2[i]) + dt * S2[i]

        h_star, Q_star = U1_star / B, U2_star
        A_star, u_star, Sf_star = update_hydraulics(h_star, Q_star)
        F1_star = Q_star
        F2_star = Q_star**2 / A_star + g * A_star**2 / (2 * B)
        S2_star = g * A_star * (S0 - Sf_star)

        U1_new, U2_new = np.zeros_like(U1), np.zeros_like(U2)
        for i in range(1, Nx - 1):
            U1_new[i] = 0.5 * (U1[i] + U1_star[i] - (dt / dx) * (F1_star[i] - F1_star[i-1]))
            U2_new[i] = 0.5 * (U2[i] + U2_star[i] - (dt / dx) * (F2_star[i] - F2_star[i-1]) + dt * S2_star[i])

        h_new = U1_new / B
        Q_new = U2_new
        Q_new[0] = Q_inflow
        h_new[0] = h_new[1]
        h_new[-1] = get_downstream_h(t)
        Q_new[-1] = Q_new[-2]
        
        h, Q = h_new.copy(), Q_new.copy()

        if n % 100 == 0:
            h_results.append(h.copy())
            Q_results.append(Q.copy())
            t_results.append(t)

    return t_results, x, h_results, Q_results