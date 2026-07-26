#!/usr/bin/env python3
"""Build release cdylibs for image_schedule_trace + image_schedule_core."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
CRATES = (
    ROOT / "crates" / "image_schedule_trace",
    ROOT / "crates" / "image_schedule_core",
)


def _cargo() -> str:
    import shutil as sh

    found = sh.which("cargo")
    if found:
        return found
    home = Path.home() / ".cargo" / "bin" / "cargo"
    if home.is_file():
        return str(home)
    raise SystemExit("cargo not found")


def main() -> int:
    cargo = _cargo()
    NATIVE.mkdir(exist_ok=True)
    names = (
        "image_schedule_trace.dll",
        "image_schedule_core.dll",
        "libimage_schedule_trace.so",
        "libimage_schedule_core.so",
    )
    for crate in CRATES:
        subprocess.run(
            [cargo, "test", "--manifest-path", str(crate / "Cargo.toml")],
            check=True,
        )
        subprocess.run(
            [cargo, "build", "--release", "--manifest-path", str(crate / "Cargo.toml")],
            check=True,
        )
        release = crate / "target" / "release"
        for name in names:
            src = release / name
            if src.is_file():
                dst = NATIVE / name
                shutil.copy2(src, dst)
                print(f"copied {src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
