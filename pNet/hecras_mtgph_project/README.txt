This project is auto-generated from MTGHP-core and steady_1d_python.py.

Important approximations:
1. Split discharges and drain inflow are prescribed from the offline solver results.
2. 'None', 'Spill End', and 'Spill Zero' master BCs are translated into solved-equivalent HEC-RAS placeholder stages so the steady model has runnable external boundaries.
3. Cross sections are exported on approximately 5.00 m spacing, with internal junction ends held back where needed to reduce crossing and bow-tie issues.
4. River centerlines are rebuilt from the MTGHP-core CSV coordinates plus every exported cross-section intersection at no more than 1.00 m spacing, so bends remain curved in RAS Mapper.
5. GIS cut lines and station/elevation templates are limited to the bed width plus 0.20 m offset on each side.
6. Spill locations are exported in spill_inventory.csv for manual refinement into true lateral structures if you want native HEC-RAS spill mechanics instead of prescribed-flow equivalence.
