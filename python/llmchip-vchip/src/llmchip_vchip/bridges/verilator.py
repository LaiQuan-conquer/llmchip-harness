"""Building and running an RTL bundle under Verilator in the EDA container.

The bundle (`rtl.bundle`, a tar with `bundle/design.json` and `bundle/rtl/`) is built with the
harness's own pin-level testbench (tb/vchip_tb.cpp) and testbench top (tb/vchip_top.v) around the
bundle's chip function; the
binary is cached per (bundle, testbench, image) under the chip directory. The container never
gets a network and sees the build directory read-only when it runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import subprocess
import tarfile
from importlib import resources
from pathlib import Path

from ..interface import ChipError
from ..manifest import file_hash

TOOLS = "/foss/tools/bin"
TESTBENCH = "vchip_tb.cpp"
TOP = "vchip_top.v"
BINARY = "bundle/rtl/obj/Vchip"
FLAGS = [
    "--cc",
    "--exe",
    "--build",
    "-O3",
    "--x-assign",
    "fast",
    "--x-initial",
    "fast",
    "-Wno-fatal",
    "-Wno-lint",
    "-Wno-style",
    # The circuits of a spatial design make very large functions: split them.
    "--output-split",
    "20000",
    "--output-split-cfuncs",
    "2000",
]


# Build containers running now, so that a host being stopped can remove them (`remove_builds`).
_BUILDS: set[str] = set()


def remove_container(name: str, timeout: float = 10.0) -> None:
    """Kills, then removes, a container. Podman 3.4 (this machine) has no `rm -t`, and the
    testbench is PID 1 of its container and ignores SIGTERM, so the kill comes first."""
    for command in (["podman", "kill", name], ["podman", "rm", "-f", name]):
        with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError):
            subprocess.run(command, capture_output=True, timeout=timeout)


def remove_builds() -> None:
    for name in list(_BUILDS):
        remove_container(name)


def testbench_source() -> bytes:
    return (resources.files("llmchip_vchip.bridges") / "tb" / TESTBENCH).read_bytes()


def image_present(image: str) -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "image", "exists", image], capture_output=True).returncode == 0


def _user() -> list[str]:
    uid, gid = os.getuid(), os.getgid()
    return [f"--user={uid}:{gid}", *([] if uid == 0 else ["--userns=keep-id"])]


def container(image: str, mounts: list[str], entrypoint: str, *args: str, name: str | None = None) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "-i",
        *([f"--name={name}"] if name else []),
        "--network=none",
        *_user(),
        "--security-opt=label=disable",
        *(f"--volume={m}" for m in mounts),
        f"--entrypoint={entrypoint}",
        image,
        *args,
    ]


def _safe(name: str) -> str:
    """A bundle path as Verilator gets it: relative, inside the bundle, not an option."""
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or name.startswith("-") or not name:
        raise ChipError("crash", f"the bundle names an unsafe source path {name!r}")
    return p.as_posix()


def build(bundle: Path, image: str, cache: Path, timeout: float = 3600) -> Path:
    """The build directory with the testbench binary for `bundle` (built once, then reused).

    The chip function (`design.json` `top` and `sources`) is wrapped in the harness's own
    `tb_top` (tb/vchip_top.v); the bundle's testbench files are not used. Builds of the same key
    are serialized by a lock file; a finished build directory is never replaced."""
    import fcntl
    import json

    tb, top_v = testbench_source(), (resources.files("llmchip_vchip.bridges") / "tb" / TOP).read_bytes()
    digest = hashlib.sha256(tb + b"\0" + top_v).hexdigest()
    key = hashlib.sha256(f"{file_hash(bundle)}\0{digest}\0{image}".encode()).hexdigest()[:16]
    out = cache / key
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / f"{key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (out / BINARY).is_file():
            return out
        if not image_present(image):
            raise ChipError("unsupported", f"the simulator image {image} is not present (podman pull it)")
        tmp = cache / f"{key}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        try:
            tmp.mkdir()
            with tarfile.open(bundle) as tar:
                tar.extractall(tmp, filter="data")
            root = tmp / "bundle"
            design = json.loads((root / "design.json").read_text())
            top = design.get("top", "llmchip_chip")
            if not top.isidentifier():
                raise ChipError("crash", f"the bundle's top {top!r} is not a module name")
            (root / TESTBENCH).write_bytes(tb)
            (root / TOP).write_bytes(top_v)
            sources = [_safe(p) for p in design["sources"]]
            name = f"llmchip-vchip-build-{key}-{secrets.token_hex(3)}"
            cmd = container(
                image,
                [f"{tmp}:/work"],
                f"{TOOLS}/verilator",
                *FLAGS,
                "-j",
                str(os.cpu_count() or 4),
                f"+define+CHIP_TOP={top}",
                "--Mdir",
                "rtl/obj",
                "-o",
                "Vchip",
                "--top-module",
                "tb_top",
                TOP,
                *sources,
                TESTBENCH,
                "-CFLAGS",
                "-O2",
                name=name,
            )
            cmd.insert(cmd.index(f"--entrypoint={TOOLS}/verilator"), "--workdir=/work/bundle")
            _BUILDS.add(name)
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise ChipError("timeout", f"the Verilator build took more than {timeout:g} s") from e
            finally:
                remove_container(name)
                _BUILDS.discard(name)
            if proc.returncode != 0 or not (tmp / BINARY).is_file():
                raise ChipError("crash", f"Verilator failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-2000:]}")
            tmp.rename(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def build_tile(bundle: Path, image: str, cache: Path, timeout: float = 3600) -> Path:
    """The build directory of a `taalas-tile/1` bundle with the tile pin testbench binary.

    The tile chip has its own host pins, so its testbench (`tb/tile_tb.cpp` + `tb/tile_top.v`)
    wraps `tile_chip` and every `rtl/*.v` of the bundle is compiled with it."""
    import fcntl
    import json

    tb = (resources.files("llmchip_vchip.bridges") / "tb" / "tile_tb.cpp").read_bytes()
    top_v = (resources.files("llmchip_vchip.bridges") / "tb" / "tile_top.v").read_bytes()
    digest = hashlib.sha256(tb + b"\0" + top_v).hexdigest()
    key = hashlib.sha256(f"{file_hash(bundle)}\0{digest}\0{image}".encode()).hexdigest()[:16]
    out = cache / key
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / f"{key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (out / "bundle/rtl/obj_tile/Vtile").is_file():
            return out
        if not image_present(image):
            raise ChipError("unsupported", f"the simulator image {image} is not present (podman pull it)")
        tmp = cache / f"{key}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        try:
            tmp.mkdir()
            with tarfile.open(bundle) as tar:
                tar.extractall(tmp, filter="data")
            root = tmp / "bundle"
            design = json.loads((root / "design.json").read_text())
            if design.get("architecture") != "taalas-tile/1":
                raise ChipError("crash", f"not a tile bundle: {design.get('architecture')!r}")
            (root / "tile_tb.cpp").write_bytes(tb)
            (root / "tile_top.v").write_bytes(top_v)
            sources = sorted(_safe(p.relative_to(root).as_posix()) for p in (root / "rtl").glob("*.v"))
            name = f"llmchip-vchip-tile-build-{key}-{secrets.token_hex(3)}"
            cmd = container(
                image,
                [f"{tmp}:/work"],
                f"{TOOLS}/verilator",
                "--cc",
                "--exe",
                "--build",
                "-O3",
                "-Wno-fatal",
                "--x-assign",
                "fast",
                "--x-initial",
                "fast",
                "--Mdir",
                "rtl/obj_tile",
                "-o",
                "Vtile",
                "--top-module",
                "tb_top",
                "tile_top.v",
                *sources,
                "tile_tb.cpp",
                "-CFLAGS",
                "-O2",
                name=name,
            )
            cmd.insert(cmd.index(f"--entrypoint={TOOLS}/verilator"), "--workdir=/work/bundle")
            _BUILDS.add(name)
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            finally:
                remove_container(name)
                _BUILDS.discard(name)
            if proc.returncode != 0 or not (tmp / "bundle/rtl/obj_tile/Vtile").is_file():
                raise ChipError("crash", f"Verilator failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-2000:]}")
            tmp.rename(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out
