// SPDX-License-Identifier: Apache-2.0
//
// Pre-screen fixture: flops on both edges of `clk`, one through an inverter and a buffer
// in a submodule. All are clocked by the top-level `clk`, so nothing is derived.

`default_nettype none

module test_clock_buffer (
    input  wire i,
    output wire o
);
  assign o = i;
endmodule

module tt_um_test_inverted_clock (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  wire nclk = ~clk;
  wire nclk_buf;
  reg [3:0] rise;
  reg [3:0] fall;
  reg [3:0] fall2;

  test_clock_buffer buffer (.i(nclk), .o(nclk_buf));

  always @(posedge clk) rise <= rst_n ? rise + 4'd1 : 4'd0;
  always @(negedge clk) fall <= rise;
  always @(posedge nclk_buf) fall2 <= rise;

  assign uo_out  = {rise, fall ^ fall2};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
