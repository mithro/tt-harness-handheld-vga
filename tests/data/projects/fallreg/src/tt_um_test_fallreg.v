// SPDX-License-Identifier: Apache-2.0
//
// Test design for a project clock made on the falling edge of clk: `nq` is a register on
// the falling edge, used as a clock (`capn`). (tt_um_test_fallclk without its latch, which
// nextpnr-xilinx cannot place.)
//
// uo_out = {capn, count}.

`default_nettype none

module tt_um_test_fallreg (
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
  reg [3:0] count;
  reg [3:0] capn;

  always @(posedge clk) begin
    if (!rst_n) count <= 4'd0;
    else count <= count + 4'd1;
  end

  always @(negedge clk) begin
    if (!rst_n) nq <= 1'b0;
    else nq <= ~nq;
  end

  always @(posedge nq) capn <= count;

  assign uo_out  = {capn, count};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
