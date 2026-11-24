# Spike A after Task 9c: timing per domain, 2026-11-24

Companion to `2026-11-spike-a-compare-build.md`, section "After Task 9c": every domain and
clock pair of the sixteen builds made with `rst_n` from a register, at the default settle
of 3 system cycles, as `BuildResult.timing` has it (`uv run python scripts/pool_report.py
timing`, from `work/pool/<shuttle>/<macro>/build-<toolchain>.json`, which is git-ignored and
local). Periods, requirements and columns are as in
`2026-11-spike-a-compare-build-9b-timing.md`: nothing in the constraints changed.

### `tt07/tt_um_tinytapeout_dvd_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 106.92 | 80.000 | 70.647 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.010 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 140.41 | 10.000 | 2.878 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.870 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 94.91 | 80.000 | 69.464 | 0.061 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.530 | 1.038 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 208.33 | 10.000 | 5.200 | 0.014 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 27.898 | 7.711 | met |

### `tt08/tt_um_tinytapeout_logo_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 90.38 | 80.000 | 68.936 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.010 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 139.74 | 10.000 | 2.844 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.010 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 92.63 | 80.000 | 69.204 | 0.213 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.509 | 1.072 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 207.64 | 10.000 | 5.184 | 0.107 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.953 | 8.016 | met |

### `ttihp26a/tt_um_urish_rings`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 238.32 | 80.000 | 75.804 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 9.430 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 152.58 | 10.000 | 3.446 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.300 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 165.37 | 80.000 | 73.953 | 0.167 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 17.181 | 1.156 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 210.66 | 10.000 | 5.253 | 0.150 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 27.407 | 7.698 | met |
| Vivado | `sys->pclk (async_default)` | cross | - | yes | no | - | - | n/a | 28.795 | 9.404 | met |

### `ttihp0p3/tt_um_algofoogle_fun_vga_clock`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 128.60 | 80.000 | 72.224 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 8.930 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 128.24 | 10.000 | 2.202 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.190 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 134.48 | 80.000 | 72.564 | 0.143 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 16.411 | 1.176 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 214.82 | 10.000 | 5.345 | 0.115 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.287 | 7.867 | met |

### `ttihp25a/tt_um_algofoogle_vga`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 182.55 | 80.000 | 74.522 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 8.570 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 163.93 | 10.000 | 3.900 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.880 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 143.70 | 80.000 | 73.041 | 0.230 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 17.892 | 1.231 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 211.19 | 10.000 | 5.265 | 0.164 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.697 | 7.682 | met |

### `ttsky26a/tt_um_algofoogle_dottee`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 70.48 | 80.000 | 65.812 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.010 | n/a | met |
| openXC7 | `project:project.uo_out[3]` | project | register | yes | no | 12.50 | 303.49 | 80.000 | 76.705 | n/a | met |
| openXC7 | `project:project.uo_out[3]->pclk` | cross | - | yes | no | - | - | 70.000 | 55.980 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 163.35 | 10.000 | 3.878 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.540 | n/a | met |
| openXC7 | `sys->project:project.uo_out[3]` | cross | - | yes | no | - | - | 20.000 | 17.910 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 72.89 | 80.000 | 66.280 | 0.191 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.564 | 1.022 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q` | project | register | yes | no | 6.25 | 261.92 | 160.000 | 156.182 | 0.332 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->pclk` | cross | - | yes | no | - | - | 80.000 | 63.817 | 0.352 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->sys` | cross | - | yes | no | - | - | 30.000 | 24.789 | 1.516 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 184.91 | 10.000 | 4.592 | 0.084 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.613 | 7.911 | met |
| Vivado | `sys->project:project/hvsync_gen/vsync_reg/Q (async_default)` | cross | - | yes | no | - | - | n/a | 29.270 | 8.757 | met |

### `ttsky25a/tt_um_gamepad_pmod_demo`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 98.22 | 80.000 | 69.819 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.090 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 158.45 | 10.000 | 3.689 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.640 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 171.44 | 80.000 | 74.167 | 0.177 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.191 | 1.237 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 207.30 | 10.000 | 5.176 | 0.135 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.719 | 7.198 | met |

### `ttihp25a/tt_um_2048_vga_game`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 69.68 | 80.000 | 65.649 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.070 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 138.26 | 10.000 | 2.767 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 15.760 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 176.65 | 80.000 | 74.339 | 0.067 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.755 | 0.959 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 209.78 | 10.000 | 5.233 | 0.110 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 28.669 | 8.235 | met |
