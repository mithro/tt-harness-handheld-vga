# Constraints the FPGA build must apply

The demo build has two clock domains, `sys` and `project`, which meet only in
[`tt_harness/core/slot.py`](../tt_harness/core/slot.py) and
[`tt_harness/core/atspeed.py`](../tt_harness/core/atspeed.py), through the blocks of
[`tt_harness/core/cdc.py`](../tt_harness/core/cdc.py). `hold`, `dst`, `req` and `ack_o` are the
registers of each `MultiBitCrossing`; `rst_req` and the two reset flops are in the slot; the gate
is [`tt_harness/input_gate.py`](../tt_harness/input_gate.py). The same list is in the module
docstring of [`cdc.py`](../tt_harness/core/cdc.py). The two flows are
[Vivado](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html) and
[openXC7](https://github.com/openXC7) (synthesis by [Yosys](https://github.com/YosysHQ/yosys));
installing them is in [`docs/toolchains.md`](toolchains.md).

- `set_max_delay -datapath_only` from each `hold` register to its `dst` register:
  - the sys -> project buses (`ctl_x`, `buttons_x`, `cmd_x`): at most the project period
    (15.15 ns at 66 MHz);
  - `rep_x` (project -> sys): at most 20 ns.
- The same bound from each `req`, `ack_o`, single-bit synchroniser source and `LOCKED` to its
  first synchroniser flop.
- `rst_req` to the `FDCE` `CLR` pins: max-delay, or a false path on those pins only.
- Never `set_clock_groups` or a blanket false path: they override max-delay.
- `ASYNC_REG` on every synchroniser pair and on both reset flops.
- No `SRL16` / `SRLC32` on any crossing, in either flow
  ([Vivado](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html),
  [openXC7](https://github.com/openXC7)).
- The checks to run:
  - the gate ([`tt_harness/input_gate.py`](../tt_harness/input_gate.py)) on the
    [Yosys](https://github.com/YosysHQ/yosys) netlist of the [openXC7](https://github.com/openXC7)
    flow, with the `BUFGCE` output net as `expected`, `("BUFGCE",)` as `allowed_trace` and
    `("MMCME2_ADV", "CLKOUT0")` as `clock_sources` and `allowed_source`;
  - [`vivado_tcl`](../tt_harness/input_gate.py) on the design routed by
    [Vivado](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html), read
    by [`parse_vivado`](../tt_harness/input_gate.py);
  - two negative builds per flow that must be rejected: `pin_ena` from a `sys` register, and
    a LUT on `rst_req` -> `CLR`;
  - the cross-domain path report lists only the held buses and the synchroniser inputs.
