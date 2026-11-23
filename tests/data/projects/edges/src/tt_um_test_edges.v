// SPDX-License-Identifier: Apache-2.0
//
// Test design for the reference dump's sampling point: `rise` counts on the
// rising edge, `fall` copies `rise` on the falling edge, and
// uo_out = {fall[3:0], rise[3:0]}.

`default_nettype none

module tt_um_test_edges (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [3:0] rise;
  reg [3:0] fall;

  always @(posedge clk) begin
    if (!rst_n) rise <= 4'd0;
    else rise <= rise + 4'd1;
  end

  always @(negedge clk) begin
    if (!rst_n) fall <= 4'd0;
    else fall <= rise;
  end

  assign uo_out  = {fall, rise};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
