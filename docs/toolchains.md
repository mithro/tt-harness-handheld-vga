# Toolchains

How the FPGA toolchains for this repository are installed and used. Verified on
2026-11-23 on Debian 13 (x86_64) without root.

## Install

```sh
uv run scripts/setup_toolchains.py
```

Everything goes into `.toolchains/` inside the repository (gitignored, about 6.4 GB
including 1.2 GB of cached downloads in `.toolchains/.cache/`). Nothing is written to the
home directory, `/tmp` or system paths. The only host requirements are `uv` and
`unsquashfs` (Debian package `squashfs-tools`). Re-running the script is a no-op; delete a
component's `.installed` marker, or a chip database `.bin`, to redo that part.
`--only <component>...` installs a subset.

| Component | Version | Where | Download check |
|---|---|---|---|
| nextpnr-xilinx, Project X-Ray tools (`fasm2frames`, `xc7frames2bit`, `bit2fasm`, `bbasm`) and `prjxray-db` | openXC7 snap 0.8.2 | `.toolchains/openxc7/` | SHA-256 recorded here (none published upstream) |
| nextpnr-xilinx chip databases for `xc7a35tcsg324-1` and `xc7a100tcsg324-1` | built locally | `.toolchains/chipdb/<part>.bin` | n/a |
| Yosys 0.71+77, openFPGALoader 1.2.0 | OSS CAD Suite 2026-11-22 | `.toolchains/oss-cad-suite/` | SHA-256 published upstream |
| `riscv-none-elf-gcc` 14.2.0 (also aliased as `riscv64-unknown-elf-*`) | xPack 14.2.0-3 | `.toolchains/riscv-gcc/` | SHA-256 published upstream |

Notes on the openXC7 snap:

- It is unpacked with `unsquashfs`; snapd is not needed.
- It contains no Yosys, which is why the OSS CAD Suite is installed.
- Its binaries name a loader under `/snap/core20/` and its Python entry points have
  `/snap/openxc7/current` paths, so neither runs as unpacked. The setup script writes
  wrappers in `.toolchains/openxc7/bin/` that start the binaries through the host loader
  and rebuild the Python path. Use the wrappers, not the files under `squashfs-root/`.
- It ships no nextpnr chip databases. The setup script builds them with the snap's
  `bbaexport.py` and `bbasm` (about a minute per part, 88 MiB and 151 MiB). To add a part,
  append it to `CHIPDB_PARTS` in `scripts/setup_toolchains.py`. A chip database is specific
  to the speed grade in its name.

## Use

```sh
source scripts/env.sh
```

This sets `PATH`, `CHIPDB`, `PRJXRAY_DB_DIR` and `NEXTPNR_XILINX_PYTHON_DIR` (the names
LiteX's openXC7 backend reads) and points `TMPDIR` at `<repo>/tmp`.

The flow for one part, as run by the smoke test:

```sh
PART=xc7a100tcsg324-1
yosys -p "synth_xilinx -flatten -abc9 -arch xc7 -top blink; write_json blink.json" blink.v
nextpnr-xilinx --chipdb $CHIPDB/$PART.bin --xdc gamebub.xdc --freq 50 \
    --json blink.json --fasm blink.fasm
fasm2frames --part $PART --db-root $PRJXRAY_DB_DIR/artix7 blink.fasm > blink.frames
xc7frames2bit --part_file $PRJXRAY_DB_DIR/artix7/$PART/part.yaml --part_name $PART \
    --frm_file blink.frames --output_file blink.bit
```

### Clock constraints in nextpnr-xilinx

nextpnr-xilinx 0.8.2 accepts `create_clock` in the XDC without complaint but does not
apply it to the clock net behind the input buffer and BUFG: with `create_clock -period 10`
on the port (tried as `get_ports`, `get_nets` and with `-name`), the blink design was still
checked against the 12 MHz default. Pass `--freq <MHz>` as well, and check the
`PASS at <target> MHz` figure in the log rather than trusting the word PASS.

### Vivado

Vivado 2025.2 is installed system-wide and is not managed by this repository. Use it from
a subshell so its environment does not mix with the open-source tools:

```sh
(source /opt/xilinx/Vivado/2025.2/settings64.sh && vivado -mode batch -source build.tcl)
```

## Smoke test

```sh
uv run pytest tests/toolchain -v -s
```

Builds `tests/toolchain/blink.v` for the Arty A7-35T (`xc7a35tcsg324-1`, 100 MHz) and the
Game Bub (`xc7a100tcsg324-1`, 50 MHz) through Yosys, nextpnr-xilinx, `fasm2frames` and
`xc7frames2bit`, then checks that timing passed at the intended frequency and that the
`.bit` has the standard header, the sync word and the full-device size. It skips when
`.toolchains/` is absent or incomplete. Outputs and one log per step are left in
`build/toolchain/<part>/`. The bitstreams have not been loaded onto hardware.

Result on 2026-11-23:

| Part | Bitstream | Achieved | Target |
|---|---|---|---|
| `xc7a35tcsg324-1` | 2 192 115 bytes | 249.00 MHz | 100 MHz |
| `xc7a100tcsg324-1` | 3 825 892 bytes | 259.74 MHz | 50 MHz |

## F4PGA: not installed

F4PGA for xc7 could not be installed. `uv run scripts/setup_toolchains.py --only f4pga`
reproduces the attempt; it is not part of the default install.

The script follows the upstream procedure (`chipsalliance/f4pga-examples` at `13f1119`,
`docs/getting.rst`): create the conda environment from `xc7/environment.yml`, then unpack
the arch-defs tarballs (`20220920-124259`, `007d1c1`: `install-xc7`, `xc7a50t_test` which
also covers the xc7a35t, and `xc7a100t_test`). It uses micromamba 2.10.0-0 with all conda
state under `.toolchains/f4pga/`. The environment cannot be solved:

1. With `environment.yml` as published (its only channel is `litex-hub`): `make`, `git`,
   `pip`, `lxml`, `simplejson`, `intervaltree` and `libgcc-ng >=11.2.0` do not exist.
   Upstream relies on the `defaults` channel that Miniconda adds implicitly.
2. With `conda-forge` added: `gcc-riscv64-elf-newlib 10.1.0` needs `cloog`, which needs
   `gmp ==5.1.2`, which does not exist; and `yosys 0.27_29` needs `zlib >=1.2.13,<1.3`,
   which conflicts with what `vtr-optimized` pulls in through `tbb`.
3. A dry run with the RISC-V GCC line removed still fails on the `yosys`/`zlib` conflict.

The arch-defs tarballs themselves are still downloadable (HTTP 200 on 2026-11-23). What
was not tried: Miniconda with Anaconda's `defaults` channel (its terms of service restrict
organisational use, and `gmp 5.1.2` may not be there either), hand-editing the pins, or
building VTR and the Yosys plugins from source. The solver output is kept in
`.toolchains/logs/f4pga-conda.log`.

## LiteX and Migen (Python)

`pyproject.toml` pins these from their Git repositories (recorded in `uv.lock`), as of
2026-11-23:

| Package | Pin |
|---|---|
| `litex`, `litex-boards` | tag `2026.08`, the newest LiteX release |
| `migen` | `m-labs/migen` master at `e19524c963a8342952840983047557707fbe0b6a` |

`pythondata-*` packages are only needed for SoC builds and are not installed yet.
