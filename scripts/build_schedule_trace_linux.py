#!/usr/bin/env python3
"""Build Linux release cdylibs locally via Docker (never compile on Panda)."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATES = (
    ("image_schedule_trace", "libimage_schedule_trace.so"),
    ("image_schedule_core", "libimage_schedule_core.so"),
)
NATIVE = ROOT / "native"
DOCKER_IMAGE = "rust:1-bookworm"


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 900) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)


def build_local_windows() -> list[Path]:
    _run([sys.executable, str(ROOT / "scripts" / "build_schedule_trace.py")])
    out: list[Path] = []
    for _, so_name in CRATES:
        dll_name = so_name.replace("lib", "").replace(".so", ".dll")
        for name in (dll_name, so_name):
            candidate = NATIVE / name
            if candidate.is_file():
                out.append(candidate)
                break
    if not out:
        raise SystemExit("no local cdylib artifact")
    return out


def _docker_cmd(crate: Path) -> list[str]:
    if shutil.which("docker"):
        crate_mount = str(crate)
    else:
        wsl_docker = subprocess.run(
            ["wsl", "-e", "which", "docker"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if wsl_docker.returncode != 0 or not wsl_docker.stdout.strip():
            raise SystemExit(
                "docker not found in PATH or WSL; install Docker Desktop / enable WSL integration"
            )
        win_path = subprocess.run(
            ["wsl", "-e", "wslpath", "-a", str(crate)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        crate_mount = win_path
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{crate_mount}:/crate",
        "-w",
        "/crate",
        DOCKER_IMAGE,
        "cargo",
        "build",
        "--release",
    ]


def build_linux_wsl_cargo() -> list[Path]:
    probe = subprocess.run(
        ["wsl", "-e", "bash", "-lc", "command -v cargo"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise SystemExit("WSL cargo not found; install rustup in WSL or fix Docker proxy")

    NATIVE.mkdir(exist_ok=True)
    built: list[Path] = []
    for crate_name, so_name in CRATES:
        crate = ROOT / "crates" / crate_name
        if not (crate / "Cargo.toml").is_file():
            raise SystemExit(f"missing crate: {crate}")
        wsl_crate = subprocess.run(
            ["wsl", "-e", "wslpath", "-a", str(crate)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        cmd = [
            "wsl",
            "-e",
            "bash",
            "-lc",
            f"set -euo pipefail; cd {wsl_crate}; cargo build --release",
        ]
        _run(cmd, timeout=900)
        src = crate / "target" / "release" / so_name
        if not src.is_file():
            raise SystemExit(f"missing release artifact: {src}")
        dst = NATIVE / so_name
        shutil.copy2(src, dst)
        print(f"copied {src} -> {dst}")
        built.append(dst)
    return built


def build_linux_docker() -> list[Path]:
    NATIVE.mkdir(exist_ok=True)
    built: list[Path] = []
    use_wsl = shutil.which("docker") is None
    for crate_name, so_name in CRATES:
        crate = ROOT / "crates" / crate_name
        if not (crate / "Cargo.toml").is_file():
            raise SystemExit(f"missing crate: {crate}")
        cmd = _docker_cmd(crate)
        if use_wsl:
            cmd = ["wsl", "-e", *cmd]
        _run(cmd, timeout=900)
        src = crate / "target" / "release" / so_name
        if not src.is_file():
            raise SystemExit(f"missing release artifact: {src}")
        dst = NATIVE / so_name
        shutil.copy2(src, dst)
        print(f"copied {src} -> {dst}")
        built.append(dst)
    return built


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--target",
        choices=("linux", "windows", "all"),
        default="linux",
        help="linux=Docker glibc .so (Panda); windows=local cargo .dll",
    )
    args = ap.parse_args()

    paths: list[Path] = []
    if args.target in ("windows", "all"):
        paths.extend(build_local_windows())
    if args.target in ("linux", "all"):
        try:
            paths.extend(build_linux_wsl_cargo())
        except SystemExit:
            raise
        except Exception as exc:
            print(f"wsl_cargo_failed: {exc}; trying docker", flush=True)
            paths.extend(build_linux_docker())
    for path in paths:
        print(f"artifact={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
