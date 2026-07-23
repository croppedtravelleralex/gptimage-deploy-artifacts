#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "scripts/_tmp_spa_sse_diag.py",
    "scripts/_tmp_spa_warm_handoff_poc.py",
    "scripts/_tmp_spa_camoufox_image_http_repro.py",
    "scripts/_tmp_spa_cookie_strip.py",
    "scripts/_tmp_spa_camoufox_via_panda_socks.py",
    "scripts/_tmp_spa_http_repro_aligned.py",
    "scripts/_tmp_spa_text_continue_ablate.py",
]

PATTERN = re.compile(
    r'("prepare_token"\s*:\s*[^\n]+,\s*)'
    r'"proof_token"'
    r'(\s*:\s*[^\n]+,\s*)'
    r'"turnstile_token"',
    re.MULTILINE,
)


def main() -> None:
    for rel in FILES:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        new, n = PATTERN.subn(r'\1"proofofwork"\2"turnstile"', text)
        if n:
            path.write_text(new, encoding="utf-8")
            print(f"updated {rel} ({n})")
        else:
            print(f"unchanged {rel}")


if __name__ == "__main__":
    main()
