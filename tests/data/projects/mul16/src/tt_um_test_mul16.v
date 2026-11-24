// SPDX-License-Identifier: Apache-2.0
//
// Test design with a 16 x 16 multiplication that has logic on both sides, so that Vivado
// infers a DSP48E1 and cannot pack a register into it: the cell is unregistered and its
// CLK pin is tied to ground. a and b are made from a free-running counter; uo_out is a
// registered fold of the product.

`default_nettype none

module tt_um_test_mul16 (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [19:0] count;
  reg [ 7:0] out;

  wire [15:0] a = count[15:0] ^ {count[3:0], count[15:4]};
  wire [15:0] b = count[19:4] ^ {count[8:4], count[19:9]};
  (* use_dsp = "yes" *) wire [31:0] p = a * b;

  always @(posedge clk) begin
    if (!rst_n) count <= 20'd0;
    else count <= count + 20'd1;
    out <= p[7:0] ^ p[15:8] ^ p[23:16] ^ p[31:24];
  end

  assign uo_out  = out;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
