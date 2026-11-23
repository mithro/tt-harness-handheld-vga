// SPDX-License-Identifier: Apache-2.0
//
// Test design for the reference dump: while rst_n is low, uo_out holds ui_in;
// afterwards it counts up by one on every rising edge with ena high.

`default_nettype none

module tt_um_test_counter (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [7:0] count;

  always @(posedge clk) begin
    if (!rst_n) count <= ui_in;
    else if (ena) count <= count + 8'd1;
  end

  assign uo_out  = count;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
