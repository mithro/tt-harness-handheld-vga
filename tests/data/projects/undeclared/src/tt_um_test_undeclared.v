// SPDX-License-Identifier: Apache-2.0
//
// Test design with a genuinely undeclared net under `default_nettype none`: `held` is
// never declared. Yosys rejects it, so the compare build must stop before any tool runs,
// and Vivado's `default_nettype wire` retry must never be reached.

`default_nettype none

module tt_um_test_undeclared (
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

  assign held = ui_in[0];

  always @(posedge clk) begin
    if (!rst_n) count <= 8'd0;
    else if (held) count <= count + 8'd1;
  end

  assign uo_out  = count;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
