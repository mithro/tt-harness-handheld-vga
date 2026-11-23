# SPDX-License-Identifier: Apache-2.0
#
# Put the open-source toolchains installed by scripts/setup_toolchains.py on
# PATH.  Source it (bash or zsh) from anywhere:
#
#     source scripts/env.sh
#
# Sets:
#   PATH                      openXC7 wrappers, RISC-V GCC, Yosys (OSS CAD Suite)
#   CHIPDB                    directory of nextpnr-xilinx <part>.bin chip databases
#   PRJXRAY_DB_DIR            Project X-Ray database root (holds artix7/, ...)
#   NEXTPNR_XILINX_PYTHON_DIR nextpnr-xilinx bbaexport.py scripts
#   TMPDIR                    <repo>/tmp, so tools keep temporary files in the repo
#
# CHIPDB, PRJXRAY_DB_DIR and NEXTPNR_XILINX_PYTHON_DIR are the names LiteX's
# openxc7 backend reads.
#
# Vivado is not put on PATH here.  Use it from a subshell so its settings do
# not leak into this environment:
#     (source /opt/xilinx/Vivado/2025.2/settings64.sh && vivado -mode batch -source build.tcl)

if [ -n "${BASH_SOURCE:-}" ]; then
    _tth_self="${BASH_SOURCE[0]}"
else
    _tth_self="${(%):-%x}"
fi
_tth_repo="$(cd "$(dirname "$_tth_self")/.." && pwd)"
_tth_tc="$_tth_repo/.toolchains"

if [ ! -d "$_tth_tc/openxc7/bin" ]; then
    echo "env.sh: $_tth_tc is not set up; run: uv run scripts/setup_toolchains.py" >&2
else
    # The OSS CAD Suite goes last: its bin/ holds about 150 tools, and they
    # should not shadow anything already installed on the system.
    export PATH="$_tth_tc/openxc7/bin:$_tth_tc/riscv-gcc/bin:$PATH:$_tth_tc/oss-cad-suite/bin"
    export CHIPDB="$_tth_tc/chipdb"
    export PRJXRAY_DB_DIR="$_tth_tc/openxc7/squashfs-root/opt/nextpnr-xilinx/external/prjxray-db"
    export NEXTPNR_XILINX_PYTHON_DIR="$_tth_tc/openxc7/squashfs-root/opt/nextpnr-xilinx/python"
    mkdir -p "$_tth_repo/tmp"
    export TMPDIR="$_tth_repo/tmp"
fi
unset _tth_self _tth_repo _tth_tc
