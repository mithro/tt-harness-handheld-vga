# gamepad_pmod.v

`gamepad_pmod.v` is the Tiny Tapeout Gamepad Pmod's receiver, unmodified: the modules
`gamepad_pmod_driver`, `gamepad_pmod_decoder`, `gamepad_pmod_single` and `gamepad_pmod_dual`
that the games in the shuttles include. `tests/test_gamepad.py` simulates it, with
Verilator, against `tt_harness.core.gamepad.GamepadEmulator`.

- Repository: https://github.com/psychogenic/gamepad-pmod
- Path in the repository: `verilog/gamepad_pmod.v`
- Commit: `9b1408b2f5184922372ce332506f01c5c1e75acb` (13 November 2025), the latest commit
  that changed the file; the repository head when fetched (25 November 2026) was
  `adf8c61d34e542d037c53f95a5313515ece7aa32`. Fetched with `gh api` from the head, so the
  file is the head's.
- SHA-256: `1b02f969841da6f07f96e726000d64ccf2621e453db4c237247c231a16506a80`
- Licence: the file's header says `SPDX-License-Identifier: Apache-2.0`, Copyright (c) 2025
  Pat Deegan (https://psychogenic.com). (The repository as a whole carries a GPL-3.0
  `LICENSE`, which covers the firmware; the Verilog file states its own licence.)

A second copy exists in `vga-playground/src/examples/common/gamepad_pmod.v` (header
`Version: 1.0.1`). It differs from this file in comments only (the header and the comment on
reset values); the receiver logic is the same. This upstream file was used.
