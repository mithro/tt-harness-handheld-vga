// SPDX-License-Identifier: Apache-2.0
//
// Pre-screen fixture: SystemVerilog syntax (`+=`) in a `.v` file, which Yosys reads only
// with `read_verilog -sv`. Otherwise a clean synchronous counter.

`default_nettype none

module tt_um_test_sv_syntax (
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

  always @(posedge clk) begin
    if (!rst_n) count <= 8'd0;
    else count += 8'd1;
  end

  assign uo_out  = count;
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
