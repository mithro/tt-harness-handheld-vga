#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# Adapted from scripts/setup_toolchains.py in
# https://github.com/fpgas-online/fpgas.online-test-designs (Apache-2.0).
"""Install the open-source Xilinx 7-series toolchains into ``.toolchains/``.

Everything is installed inside the repository (``.toolchains/`` is gitignored);
nothing is written to the home directory, to ``/tmp`` or to system locations.

Components installed by default (``--only`` selects a subset):

  riscv-gcc      xPack riscv-none-elf-gcc, for LiteX VexRiscv firmware
  oss-cad-suite  YosysHQ OSS CAD Suite (Yosys; the openXC7 snap ships none)
  openxc7        openXC7 snap: nextpnr-xilinx, Project X-Ray tools and database
  chipdb         nextpnr-xilinx chip databases built from the X-Ray database

Only installed when named with ``--only``:

  f4pga          F4PGA for xc7 (conda environment plus arch-defs).  As of
                 2026-11-23 this FAILS at the conda solve; see
                 docs/toolchains.md before spending time on it.

Usage:
    uv run scripts/setup_toolchains.py
    uv run scripts/setup_toolchains.py --only openxc7 chipdb
    uv run scripts/setup_toolchains.py --only f4pga
    uv run scripts/setup_toolchains.py --jobs 8

Each component writes a ``.installed`` marker; delete it to force a reinstall.
See docs/toolchains.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLCHAINS = REPO / ".toolchains"
CACHE = TOOLCHAINS / ".cache"
LOGS = TOOLCHAINS / "logs"
SCRATCH = REPO / "tmp"

USER_AGENT = "setup-toolchains"

# ---------------------------------------------------------------------------
# Pinned downloads.
#
# "upstream" checksums are published by the project (GitHub release asset
# digest or a .sha file).  "recorded" checksums are not published upstream;
# they are the SHA-256 of the file as first downloaded on 2026-11-23 and only
# guard against the file changing afterwards.
# ---------------------------------------------------------------------------

RISCV_GCC_VERSION = "14.2.0-3"
RISCV_GCC_URL = (
    "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/"
    f"v{RISCV_GCC_VERSION}/xpack-riscv-none-elf-gcc-{RISCV_GCC_VERSION}-linux-x64.tar.gz"
)
RISCV_GCC_SHA256 = "f574415b63f12b09bdd3475223ab492a465d23810646c90c13a4c3b676c83503"  # upstream

OSS_CAD_SUITE_DATE = "2026-11-22"
OSS_CAD_SUITE_URL = (
    "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/"
    f"{OSS_CAD_SUITE_DATE}/oss-cad-suite-linux-x64-{OSS_CAD_SUITE_DATE.replace('-', '')}.tgz"
)
OSS_CAD_SUITE_SHA256 = (
    "2aad704349a323e3669e3df73ec893d96fd87c1fc7ef70889e0b0c6a841a0097"  # upstream
)

OPENXC7_VERSION = "0.8.2"
OPENXC7_URL = (
    "https://github.com/openXC7/openXC7-snap/releases/download/"
    f"{OPENXC7_VERSION}/openxc7_{OPENXC7_VERSION}_amd64.snap"
)
OPENXC7_SHA256 = "6b2e07ce99ef33d3a4e41e2fd2eb916f26bb0ece34a97216ed840a0032e98587"  # recorded

# Parts to build nextpnr-xilinx chip databases for.
CHIPDB_PARTS = ["xc7a35tcsg324-1", "xc7a100tcsg324-1"]

MICROMAMBA_VERSION = "2.10.0-0"
MICROMAMBA_URL = f"https://github.com/mamba-org/micromamba-releases/releases/download/{MICROMAMBA_VERSION}/micromamba-linux-64"
MICROMAMBA_SHA256 = "701f39c06f8e6a670a0e186c8479b9e5c0502a2f0cda6cef65d83bdd2875e8bd"  # upstream

# F4PGA: the conda environment and arch-defs pins come from f4pga-examples.
F4PGA_EXAMPLES_COMMIT = "13f11197b33dae1cde3bf146f317d63f0134eacf"
F4PGA_EXAMPLES_RAW = (
    f"https://raw.githubusercontent.com/chipsalliance/f4pga-examples/{F4PGA_EXAMPLES_COMMIT}"
)
F4PGA_EXAMPLES_FILES = {  # recorded
    "xc7/environment.yml": "71903fcca045ce27d46e8c07fd915c330ba4e2b05735af3caed71caae133f8b6",
    "xc7/requirements.txt": "a5652d518d6f90251187e019b91ebf22d201290dfdfd315d50f759d61e629ad3",
    "common/requirements.txt": "da09204699a2673d85aad50f6b77a01ba88035ddabb9df6ffc144065e21caa9d",
}
F4PGA_TIMESTAMP = "20220920-124259"
F4PGA_HASH = "007d1c1"
F4PGA_ARCH_DEFS_BASE = (
    "https://storage.googleapis.com/symbiflow-arch-defs/artifacts/prod/foss-fpga-tools/"
    f"symbiflow-arch-defs/continuous/install/{F4PGA_TIMESTAMP}"
)
# xc7a50t_test also covers the xc7a35t (same die).
# Not yet pinned: the install has never got as far as downloading these.
F4PGA_PACKAGES = {
    "install-xc7": None,
    "xc7a50t_test": None,
    "xc7a100t_test": None,
}
F4PGA_ENV_NAME = "xc7"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes: temporary files stay inside the repo."""
    SCRATCH.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(SCRATCH)
    if extra:
        env.update(extra)
    return env


def run(
    cmd: list[str], logname: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> None:
    """Run a command with all output appended to a log file; raise on failure."""
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / logname
    log(f"  $ {' '.join(str(c) for c in cmd)}")
    log(f"    (log: {logfile})")
    with open(logfile, "a") as lf:
        lf.write(f"\n$ {' '.join(str(c) for c in cmd)}\n")
        lf.flush()
        result = subprocess.run(
            cmd, check=False, stdout=lf, stderr=subprocess.STDOUT, env=env or tool_env(), cwd=cwd
        )
    if result.returncode != 0:
        raise SystemExit(f"ERROR: command failed with exit code {result.returncode}; see {logfile}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str | None) -> Path:
    """Download ``url`` to ``dest`` (reusing a cached copy) and check its SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        log(f"  Downloading {url}")
        part = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as response, open(part, "wb") as f:
            shutil.copyfileobj(response, f, 1 << 20)
        part.rename(dest)
    else:
        log(f"  Using cached {dest.name}")
    actual = sha256_of(dest)
    if sha256 is None:
        log(f"    sha256 {actual} (NOT PINNED)")
    elif actual != sha256:
        raise SystemExit(
            f"ERROR: checksum mismatch for {dest}\n  expected {sha256}\n  actual   {actual}"
        )
    else:
        log(f"    sha256 ok ({dest.stat().st_size // (1 << 20)} MiB)")
    return dest


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def installed(install_dir: Path, pin: str) -> bool:
    marker = install_dir / ".installed"
    if marker.exists() and marker.read_text().strip() == pin:
        log(f"  Already installed at {install_dir} (delete {marker.name} to reinstall)")
        return True
    return False


def mark_installed(install_dir: Path, pin: str) -> None:
    (install_dir / ".installed").write_text(pin + "\n")


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# RISC-V GCC
# ---------------------------------------------------------------------------


def install_riscv_gcc() -> None:
    log("\n=== RISC-V GCC (xPack) ===")
    install_dir = TOOLCHAINS / "riscv-gcc"
    if installed(install_dir, RISCV_GCC_URL):
        return
    tarball = download(RISCV_GCC_URL, CACHE / RISCV_GCC_URL.rsplit("/", 1)[1], RISCV_GCC_SHA256)
    fresh_dir(install_dir)
    log(f"  Extracting to {install_dir}")
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(install_dir, filter="tar")
    # The archive has one versioned top-level directory; expose a stable bin/.
    (top,) = [p for p in install_dir.iterdir() if p.is_dir()]
    (install_dir / "bin").symlink_to(Path(top.name) / "bin")
    # LiteX looks for several triples; provide riscv64-unknown-elf-* aliases.
    for src in sorted((top / "bin").glob("riscv-none-elf-*")):
        alias = src.with_name(src.name.replace("riscv-none-elf-", "riscv64-unknown-elf-"))
        if not alias.exists():
            alias.symlink_to(src.name)
    mark_installed(install_dir, RISCV_GCC_URL)


# ---------------------------------------------------------------------------
# OSS CAD Suite (Yosys)
# ---------------------------------------------------------------------------


def install_oss_cad_suite() -> None:
    log("\n=== OSS CAD Suite (Yosys) ===")
    install_dir = TOOLCHAINS / "oss-cad-suite"
    if installed(install_dir, OSS_CAD_SUITE_URL):
        return
    tarball = download(
        OSS_CAD_SUITE_URL, CACHE / OSS_CAD_SUITE_URL.rsplit("/", 1)[1], OSS_CAD_SUITE_SHA256
    )
    if install_dir.exists():
        shutil.rmtree(install_dir)
    log(f"  Extracting to {install_dir}")
    # The archive's single top-level directory is "oss-cad-suite".
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(TOOLCHAINS, filter="tar")
    mark_installed(install_dir, OSS_CAD_SUITE_URL)


# ---------------------------------------------------------------------------
# openXC7
# ---------------------------------------------------------------------------

# The snap's ELF binaries name /snap/core20/current/lib64/ld-linux-x86-64.so.2
# as their interpreter, which only exists under snapd.  They run fine on the
# host C library, so each wrapper starts them through the host loader.
OPENXC7_NATIVE_WRAPPER = """\
#!/bin/sh
# Generated by scripts/setup_toolchains.py
root="$(dirname "$(readlink -f "$0")")/../squashfs-root"
exec /lib64/ld-linux-x86-64.so.2 \\
    --library-path "$root/usr/lib/x86_64-linux-gnu:$root/lib/x86_64-linux-gnu" \\
    "$root/usr/bin/{tool}" "$@"
"""

# The snap's Python entry points have a /snap/openxc7/current shebang and
# hard-coded sys.path entries; rebuild the same sys.path relative to the wrapper.
OPENXC7_PYTHON_WRAPPER = """\
#!/bin/sh
# Generated by scripts/setup_toolchains.py
root="$(dirname "$(readlink -f "$0")")/../squashfs-root"
PYTHONPATH="$root/usr/lib/python3/dist-packages:$root/lib/python3.8/site-packages:$root/opt/prjxray" \\
PYTHONNOUSERSITE=1 \\
exec "$root/usr/bin/python3.8" -c 'import sys; from {module} import main; sys.exit(main())' "$@"
"""

OPENXC7_NATIVE_TOOLS = ["nextpnr-xilinx", "bbasm", "xc7frames2bit", "bitread", "xc7patch"]
OPENXC7_PYTHON_TOOLS = {"fasm2frames": "utils.fasm2frames", "bit2fasm": "utils.bit2fasm"}


def install_openxc7() -> None:
    log("\n=== openXC7 ===")
    install_dir = TOOLCHAINS / "openxc7"
    if installed(install_dir, OPENXC7_URL):
        return
    if not shutil.which("unsquashfs"):
        raise SystemExit("ERROR: unsquashfs not found (Debian package: squashfs-tools)")
    snap = download(OPENXC7_URL, CACHE / OPENXC7_URL.rsplit("/", 1)[1], OPENXC7_SHA256)
    fresh_dir(install_dir)
    run(
        ["unsquashfs", "-n", "-f", "-d", str(install_dir / "squashfs-root"), str(snap)],
        "openxc7-unsquashfs.log",
    )
    bin_dir = install_dir / "bin"
    bin_dir.mkdir()
    for tool in OPENXC7_NATIVE_TOOLS:
        write_executable(bin_dir / tool, OPENXC7_NATIVE_WRAPPER.format(tool=tool))
    for tool, module in OPENXC7_PYTHON_TOOLS.items():
        write_executable(bin_dir / tool, OPENXC7_PYTHON_WRAPPER.format(module=module))
    run([str(bin_dir / "nextpnr-xilinx"), "--version"], "openxc7-version.log")
    mark_installed(install_dir, OPENXC7_URL)


# ---------------------------------------------------------------------------
# nextpnr-xilinx chip databases
# ---------------------------------------------------------------------------


def build_one_chipdb(part: str) -> str:
    """Export one part from the X-Ray database to .bba and assemble it to .bin."""
    openxc7 = TOOLCHAINS / "openxc7"
    nextpnr_root = openxc7 / "squashfs-root" / "opt" / "nextpnr-xilinx"
    chipdb_dir = TOOLCHAINS / "chipdb"
    work = fresh_dir(SCRATCH / f"chipdb-{part}")
    bba = work / f"{part}.bba"
    bin_tmp = work / f"{part}.bin"
    logfile = LOGS / f"chipdb-{part}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    cmds = [
        # bbaexport.py only needs the standard library, so it runs under the
        # interpreter running this script.  -u keeps its progress in the log.
        [
            sys.executable,
            "-u",
            str(nextpnr_root / "python" / "bbaexport.py"),
            "--device",
            part,
            "--bba",
            str(bba),
        ],
        [str(openxc7 / "bin" / "bbasm"), "-l", str(bba), str(bin_tmp)],
    ]
    with open(logfile, "a") as lf:
        for cmd in cmds:
            lf.write(f"\n$ {' '.join(cmd)}\n")
            lf.flush()
            result = subprocess.run(
                cmd, check=False, stdout=lf, stderr=subprocess.STDOUT, env=tool_env()
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"chipdb build for {part} failed (exit {result.returncode}); see {logfile}"
                )
    final = chipdb_dir / f"{part}.bin"
    bin_tmp.replace(final)
    shutil.rmtree(work)
    return f"{final} ({final.stat().st_size // (1 << 20)} MiB)"


def build_chipdbs(jobs: int) -> None:
    log("\n=== nextpnr-xilinx chip databases ===")
    chipdb_dir = TOOLCHAINS / "chipdb"
    chipdb_dir.mkdir(parents=True, exist_ok=True)
    if not (TOOLCHAINS / "openxc7" / ".installed").exists():
        raise SystemExit("ERROR: chipdb needs the openxc7 component installed first")
    todo = [p for p in CHIPDB_PARTS if not (chipdb_dir / f"{p}.bin").exists()]
    for part in CHIPDB_PARTS:
        if part not in todo:
            log(f"  {part}: already built (delete {chipdb_dir / (part + '.bin')} to rebuild)")
    if not todo:
        return
    log(f"  Building {', '.join(todo)} (a minute or two each; logs in {LOGS}/chipdb-<part>.log)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(jobs, len(todo)))) as pool:
        futures = {pool.submit(build_one_chipdb, part): part for part in todo}
        for fut in concurrent.futures.as_completed(futures):
            log(f"  {futures[fut]}: {fut.result()}")


# ---------------------------------------------------------------------------
# F4PGA
# ---------------------------------------------------------------------------


def f4pga_env() -> dict[str, str]:
    """Environment that keeps micromamba, conda and pip state under .toolchains/."""
    root = TOOLCHAINS / "f4pga"
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return tool_env(
        {
            # conda/mamba record environments in ~/.conda; give them a private home.
            "HOME": str(home),
            "MAMBA_ROOT_PREFIX": str(root / "xc7" / "conda"),
            "CONDA_PKGS_DIRS": str(CACHE / "conda-pkgs"),
            "PIP_CACHE_DIR": str(CACHE / "pip"),
            "XDG_CACHE_HOME": str(CACHE / "xdg"),
        }
    )


def install_f4pga() -> None:
    log("\n=== F4PGA (xc7) ===")
    root = TOOLCHAINS / "f4pga"
    pin = f"{F4PGA_EXAMPLES_COMMIT} {F4PGA_TIMESTAMP}-{F4PGA_HASH} {' '.join(F4PGA_PACKAGES)}"
    if installed(root, pin):
        return
    fam_dir = root / "xc7"
    fresh_dir(root)
    fam_dir.mkdir()

    micromamba = root / "bin" / "micromamba"
    micromamba.parent.mkdir()
    shutil.copy(
        download(
            MICROMAMBA_URL, CACHE / f"micromamba-{MICROMAMBA_VERSION}-linux-64", MICROMAMBA_SHA256
        ),
        micromamba,
    )
    micromamba.chmod(0o755)

    # Environment definition, kept in its upstream directory layout because
    # xc7/requirements.txt includes ../common/requirements.txt.
    envdef = root / "f4pga-examples"
    for rel, sha in F4PGA_EXAMPLES_FILES.items():
        cached = download(
            f"{F4PGA_EXAMPLES_RAW}/{rel}",
            CACHE / "f4pga-examples" / F4PGA_EXAMPLES_COMMIT / rel,
            sha,
        )
        (envdef / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(cached, envdef / rel)

    env = f4pga_env()
    # Upstream's environment.yml lists only the litex-hub channel and relies on
    # Miniconda's implicit "defaults" channel for make, git, pip, lxml and the
    # C++ runtime.  micromamba has no implicit channel, so conda-forge is named
    # here to supply those packages.  The solve still fails: see
    # docs/toolchains.md.
    run(
        [
            str(micromamba),
            "create",
            "--yes",
            "--no-rc",
            "--name",
            F4PGA_ENV_NAME,
            "--file",
            str(envdef / "xc7" / "environment.yml"),
            "--channel",
            "conda-forge",
        ],
        "f4pga-conda.log",
        env=env,
        cwd=envdef / "xc7",
    )

    for pkg, sha in F4PGA_PACKAGES.items():
        name = f"symbiflow-arch-defs-{pkg}-{F4PGA_HASH}.tar.xz"
        tarball = download(f"{F4PGA_ARCH_DEFS_BASE}/{name}", CACHE / name, sha)
        log(f"  Extracting {name}")
        with tarfile.open(tarball, "r:xz") as tf:
            tf.extractall(fam_dir, filter="tar")
    mark_installed(root, pin)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DEFAULT_COMPONENTS = ["riscv-gcc", "oss-cad-suite", "openxc7", "chipdb"]
COMPONENTS = [*DEFAULT_COMPONENTS, "f4pga"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=COMPONENTS,
        default=DEFAULT_COMPONENTS,
        help="components to install",
    )
    parser.add_argument("--jobs", type=int, default=2, help="parallel chipdb builds (default: 2)")
    args = parser.parse_args()

    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise SystemExit("ERROR: only Linux x86_64 is supported (openXC7 publishes no other snap)")

    log(f"Installing into {TOOLCHAINS}")
    CACHE.mkdir(parents=True, exist_ok=True)
    steps = {
        "riscv-gcc": install_riscv_gcc,
        "oss-cad-suite": install_oss_cad_suite,
        "openxc7": install_openxc7,
        "chipdb": lambda: build_chipdbs(args.jobs),
        "f4pga": install_f4pga,
    }
    for name in COMPONENTS:
        if name in args.only:
            steps[name]()
    log("\nDone. Put the tools on PATH with:  source scripts/env.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
