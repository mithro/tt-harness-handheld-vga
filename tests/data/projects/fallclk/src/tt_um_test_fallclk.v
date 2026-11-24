// SPDX-License-Identifier: Apache-2.0
//
// Test design for project clocks whose edges are not all on rising edges of clk:
//
// - `nq`, a register on the falling edge of clk, used as a clock (`capn`);
// - `lq`, a latch output (transparent while clk is low), used as a clock (`capl`).
//
// uo_out = {capl, capn, count}.

`default_nettype none

module tt_um_test_fallclk (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg       nq;
  reg       lq;
  reg [3:0] count;
  reg [1:0] capn, capl;

  always @(posedge clk) begin
    if (!rst_n) count <= 4'd0;
    else count <= count + 4'd1;
  end

  always @(negedge clk) begin
    if (!rst_n) nq <= 1'b0;
    else nq <= ~nq;
  end

  always @* begin
    if (!clk) lq = count[1];
  end

  always @(posedge nq) capn <= count[1:0];
  always @(posedge lq) capl <= count[3:2];

  assign uo_out  = {capl, capn, count};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
