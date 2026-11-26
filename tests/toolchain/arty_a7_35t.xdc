# SPDX-License-Identifier: Apache-2.0
# Digilent Arty A7-35T: 100 MHz oscillator on E3, LD4 on H5.
set_property LOC E3 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
set_property LOC H5 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports led]
create_clock -period 10.0 [get_ports clk]
