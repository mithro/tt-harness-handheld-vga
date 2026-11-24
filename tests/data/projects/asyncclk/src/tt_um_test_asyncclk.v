// SPDX-License-Identifier: Apache-2.0
//
// Test design for a project clock made by a register with an asynchronous reset, the usual
// Tiny Tapeout reset idiom: `adiv` toggles on every rising edge of clk and is cleared
// asynchronously by rst_n; `cap` takes the clk-domain counter on each rising edge of
// `adiv`. uo_out = {cap, count}.

`default_nettype none

module tt_um_test_asyncclk (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg       adiv;
  reg [3:0] count;
  reg [3:0] cap;

  always @(posedge clk) begin
    if (!rst_n) count <= 4'd0;
    else count <= count + 4'd1;
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) adiv <= 1'b0;
    else adiv <= ~adiv;
  end

  always @(posedge adiv) cap <= count;

  assign uo_out  = {cap, count};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
