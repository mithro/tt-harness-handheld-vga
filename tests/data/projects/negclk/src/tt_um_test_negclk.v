// SPDX-License-Identifier: Apache-2.0
//
// Test design for a clock made by a flip-flop and used on its falling edge: `slow` toggles
// on every rising edge of clk once out of reset, and `cap` takes the clk-domain counter on
// each falling edge of `slow`. uo_out = {cap, count}.

`default_nettype none

module tt_um_test_negclk (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg       slow;
  reg [3:0] count;
  reg [3:0] cap;

  always @(posedge clk) begin
    if (!rst_n) begin
      slow  <= 1'b0;
      count <= 4'd0;
    end else begin
      slow  <= ~slow;
      count <= count + 4'd1;
    end
  end

  always @(negedge slow) cap <= count;

  assign uo_out  = {cap, count};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
