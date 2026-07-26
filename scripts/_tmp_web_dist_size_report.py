#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "web_dist"


def main() -> None:
    static = ROOT / "_next" / "static"
    files = [p for p in static.rglob("*") if p.is_file()]
    total_kb = sum(p.stat().st_size for p in files) / 1024
    print(f"static_total_kb={total_kb:.1f} files={len(files)}")
    print("\nTop chunks:")
    for p in sorted(files, key=lambda x: x.stat().st_size, reverse=True)[:20]:
        rel = p.relative_to(ROOT)
        print(f"  {p.stat().st_size/1024:7.1f} KB  {rel}")
    print("\nPer-route HTML + referenced JS:")
    for html in sorted(ROOT.rglob("index.html")):
        if html.parent == ROOT:
            route = "/"
        else:
            route = "/" + html.parent.relative_to(ROOT).as_posix()
        text = html.read_text(encoding="utf-8", errors="replace")
        scripts = re.findall(r'/_next/static/[^"\']+\.js', text)
        js_kb = 0.0
        for s in scripts:
            fp = ROOT / s.lstrip("/")
            if fp.is_file():
                js_kb += fp.stat().st_size / 1024
        print(f"  {route:20} html={html.stat().st_size/1024:.1f}KB scripts={len(scripts)} js_ref={js_kb:.1f}KB")


if __name__ == "__main__":
    main()
