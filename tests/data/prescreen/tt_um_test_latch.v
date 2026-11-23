// SPDX-License-Identifier: Apache-2.0
//
// Pre-screen fixture: an incomplete combinational `if` infers a latch on `held`.

`default_nettype none

module tt_um_test_latch (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [7:0] held;

  always @(*) begin
    if (ui_in[0]) held = uio_in;
  end

  assign uo_out  = held;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
