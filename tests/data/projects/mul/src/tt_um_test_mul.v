// SPDX-License-Identifier: Apache-2.0
//
// Test design with two small signed multiplications, the shape that made Yosys infer
// DSP48E1 cells in two pool designs (`psubx*psubx + psuby*psuby`). x and y are made by
// logic from a free-running counter, and uo_out is a registered fold of x*x + y*y: with
// logic on both sides of a multiplier no register can be packed into its DSP48E1, so the
// cell is unregistered and Yosys ties its CLK and cascade inputs to constants.

`default_nettype none

module tt_um_test_mul (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [9:0] count;
  reg [7:0] out;

  wire signed [ 4:0] x = count[4:0] ^ count[9:5];
  wire signed [ 4:0] y = count[4:0] + count[9:5];
  wire signed [10:0] product = x * x + y * y;

  always @(posedge clk) begin
    if (!rst_n) count <= 10'd0;
    else count <= count + 10'd1;
    out <= product[7:0] ^ {5'b00000, product[10:8]};
  end

  assign uo_out  = out;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
