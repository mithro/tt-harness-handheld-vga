// SPDX-License-Identifier: Apache-2.0
//
// Pre-screen fixture: a ring of three inverters, a combinational loop (ring oscillator).

`default_nettype none

module tt_um_test_ring (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  wire a, b, c;

  assign a = ~c;
  assign b = ~a;
  assign c = ~b;

  assign uo_out  = {7'b0, c};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
