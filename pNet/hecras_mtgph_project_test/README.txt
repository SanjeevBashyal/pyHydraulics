This project is auto-generated from MTGHP-core and steady_1d_python.py.

Important approximations:
1. Split discharges and drain inflow are prescribed from the offline solver results.
2. 'None', 'Spill End', and 'Spill Zero' master BCs are translated into solved-equivalent HEC-RAS placeholder stages so the steady model has runnable external boundaries.
3. Spill locations are exported in spill_inventory.csv for manual refinement into true lateral structures if you want native HEC-RAS spill mechanics instead of prescribed-flow equivalence.
