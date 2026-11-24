# Spike A after Task 9b: timing per domain, 2026-11-24

Companion to `2026-11-spike-a-compare-build.md`, section "After Task 9b": every domain and
clock pair of the sixteen builds made at the default settle of 3 system cycles, as
`BuildResult.timing` has it (`uv run python scripts/pool_report.py timing`, from
`work/pool/<shuttle>/<macro>/build-<toolchain>.json`, which is git-ignored and local).

At settle 3 on the Arty: `sys` 10 ns; `pclk` 80 ns (12.5 MHz); a clock a project makes with
a flip-flop 160 ns in Vivado (both edges on `pclk` rising edges) and 80 ns in openXC7;
`sys` to and from `pclk` or a project clock 30 ns, of which the open flow keeps 10 ns as its
allowance for the clock skew nextpnr does not model, so its budget is 20 ns; between `pclk`
and a rising-edge-only project clock one `pclk` period (openXC7: 70 ns after the allowance).

Columns as in the main document's "Timing per domain". openXC7 has no hold analysis
("n/a"). The same word "met" covers different checks in the two toolchains: see "What
"timing met" means" there.

### `tt07/tt_um_tinytapeout_dvd_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 99.47 | 80.000 | 69.947 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.010 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 186.22 | 10.000 | 4.630 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 14.180 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 92.58 | 80.000 | 69.198 | 0.186 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.232 | 1.197 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 216.73 | 10.000 | 5.386 | 0.178 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 26.950 | 8.179 | met |

### `tt08/tt_um_tinytapeout_logo_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 97.56 | 80.000 | 69.750 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.010 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 183.89 | 10.000 | 4.562 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 14.310 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 88.43 | 80.000 | 68.692 | 0.204 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.553 | 1.013 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 222.37 | 10.000 | 5.503 | 0.165 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 26.677 | 7.909 | met |

### `ttihp26a/tt_um_urish_rings`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 195.58 | 80.000 | 74.887 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 9.120 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 212.59 | 10.000 | 5.296 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 15.300 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 194.17 | 80.000 | 74.850 | 0.225 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 17.852 | 1.060 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 234.58 | 10.000 | 5.737 | 0.147 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 26.129 | 8.784 | met |
| Vivado | `sys->pclk (async_default)` | cross | - | yes | no | - | - | n/a | 27.409 | 8.572 | met |

### `ttihp0p3/tt_um_algofoogle_fun_vga_clock`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 128.04 | 80.000 | 72.190 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 9.060 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 201.21 | 10.000 | 5.030 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 14.310 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 140.10 | 80.000 | 72.862 | 0.178 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 17.898 | 1.128 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 204.92 | 10.000 | 5.120 | 0.097 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 25.025 | 8.681 | met |

### `ttihp25a/tt_um_algofoogle_vga`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 174.55 | 80.000 | 74.271 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 9.740 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 192.98 | 10.000 | 4.818 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 15.160 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 145.10 | 80.000 | 73.108 | 0.187 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 17.796 | 1.048 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 219.97 | 10.000 | 5.454 | 0.090 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 25.866 | 8.368 | met |

### `ttsky26a/tt_um_algofoogle_dottee`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 72.40 | 80.000 | 66.188 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.070 | n/a | met |
| openXC7 | `project:project.uo_out[3]` | project | register | yes | no | 12.50 | 304.14 | 80.000 | 76.712 | n/a | met |
| openXC7 | `project:project.uo_out[3]->pclk` | cross | - | yes | no | - | - | 70.000 | 56.680 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 195.16 | 10.000 | 4.876 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 14.970 | n/a | met |
| openXC7 | `sys->project:project.uo_out[3]` | cross | - | yes | no | - | - | 20.000 | 15.330 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 67.23 | 80.000 | 65.126 | 0.191 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.669 | 0.985 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q` | project | register | yes | no | 6.25 | 447.23 | 160.000 | 157.764 | 0.262 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->pclk` | cross | - | yes | no | - | - | 80.000 | 63.094 | 0.306 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->sys` | cross | - | yes | no | - | - | 30.000 | 25.171 | 1.384 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 205.21 | 10.000 | 5.127 | 0.168 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 25.763 | 8.390 | met |
| Vivado | `sys->project:project/hvsync_gen/vsync_reg/Q (async_default)` | cross | - | yes | no | - | - | n/a | 27.892 | 8.104 | met |

### `ttsky25a/tt_um_gamepad_pmod_demo`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 101.52 | 80.000 | 70.150 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.050 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 179.44 | 10.000 | 4.427 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 14.460 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 183.12 | 80.000 | 74.539 | 0.190 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.255 | 1.114 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 229.10 | 10.000 | 5.635 | 0.060 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 26.583 | 7.868 | met |

### `ttihp25a/tt_um_2048_vga_game`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 12.50 | 69.71 | 80.000 | 65.655 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 19.060 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 195.12 | 10.000 | 4.875 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 11.690 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 12.50 | 170.24 | 80.000 | 74.126 | 0.120 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 30.000 | 25.934 | 0.870 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 206.87 | 10.000 | 5.166 | 0.032 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 30.000 | 25.859 | 8.443 | met |
