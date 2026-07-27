#!/usr/bin/env python3
"""Install Webshare residential/datacenter proxy list files into data/runlogs/."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from services.config import DATA_DIR

RESI_DEST = DATA_DIR / "runlogs" / "webshare_residential_proxies.secret.txt"
DC_DEST = DATA_DIR / "runlogs" / "webshare_100_proxies.secret.txt"


def _install(src: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    lines = [line for line in dest.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    return len(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--residential", type=Path, help="Webshare residential proxy list")
    parser.add_argument("--datacenter", type=Path, help="Webshare datacenter proxy list")
    args = parser.parse_args()
    out: dict[str, object] = {"ok": True}
    if args.residential:
        out["residential_lines"] = _install(args.residential, RESI_DEST)
    if args.datacenter:
        out["datacenter_lines"] = _install(args.datacenter, DC_DEST)
    print(out)


if __name__ == "__main__":
    main()
