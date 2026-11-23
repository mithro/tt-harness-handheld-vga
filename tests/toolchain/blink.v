// SPDX-License-Identifier: Apache-2.0
// Toolchain smoke test: divide the board clock down to a visible blink.
`default_nettype none

module blink (
    input  wire clk,
    output wire led
);
    reg [25:0] counter = 26'd0;

    always @(posedge clk) begin
        counter <= counter + 26'd1;
    end

    assign led = counter[25];
endmodule
