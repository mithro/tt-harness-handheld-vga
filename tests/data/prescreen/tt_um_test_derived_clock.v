// SPDX-License-Identifier: Apache-2.0
//
// Pre-screen fixture: `slow` is clocked by `div`, another flop's output, not by `clk`.
// The divider sits in a submodule so the check must see through the hierarchy.

`default_nettype none

module test_divider (
    input  wire clk,
    input  wire rst_n,
    output reg  div
);
  always @(posedge clk) begin
    if (!rst_n) div <= 1'b0;
    else div <= ~div;
  end
endmodule

module tt_um_test_derived_clock (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  wire div;
  reg [7:0] slow;

  test_divider divider (.clk(clk), .rst_n(rst_n), .div(div));

  always @(posedge div) slow <= slow + 8'd1;

  assign uo_out  = slow;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
