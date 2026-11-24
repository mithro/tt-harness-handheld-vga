// SPDX-License-Identifier: Apache-2.0
//
// Test design for clocks a project makes from its own logic, all with edges only on rising
// edges of clk:
//
// - `gclk`, a gate of two clk registers, used on its falling edge (`capg`);
// - `div`, a register, used on its rising edge (`div2`, `a`);
// - `div2`, a ripple stage clocked by `div`, used on its falling edge (`capr`);
// - `r2`, a second register that toggles independently of `div`; `capx` on it takes `a`
//   from the `div` domain, a path between two register-made clocks;
// - `nf`, on the falling edge of clk, takes `a` from the `div` domain.
//
// uo_out = {capg, capr, capx, nf}.

`default_nettype none

module tt_um_test_clocks (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg       div, en, r2, div2;
  reg [3:0] count;
  reg [1:0] capg, capr, capx, nf;
  reg [3:0] a;

  always @(posedge clk) begin
    if (!rst_n) begin
      div   <= 1'b0;
      en    <= 1'b0;
      r2    <= 1'b0;
      count <= 4'd0;
    end else begin
      div   <= ~div;
      en    <= count[2];
      count <= count + 4'd1;
      if (count[1:0] == 2'd3) r2 <= ~r2;
    end
  end

  wire gclk = div & en;
  always @(negedge gclk) capg <= count[1:0];
  always @(posedge div) div2 <= ~div2;
  always @(negedge div2) capr <= count[1:0];
  always @(posedge div) a <= count;
  always @(posedge r2) capx <= a[1:0];
  always @(negedge clk) nf <= a[3:2];

  assign uo_out  = {capg, capr, capx, nf};
  assign uio_out = 8'h00;
  assign uio_oe  = 8'h00;
endmodule
