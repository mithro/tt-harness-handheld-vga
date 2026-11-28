# SPDX-License-Identifier: Apache-2.0
# Game Bub rev 4.1: 50 MHz oscillator on P17. The board has no FPGA-driven
# LED, so the blink goes to PMOD[0] on V14 (see docs/gamebub-hardware.md).
set_property LOC P17 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
set_property LOC V14 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports led]
create_clock -period 20.0 [get_ports clk]
