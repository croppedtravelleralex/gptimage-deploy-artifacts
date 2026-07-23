#!/usr/bin/env python3
"""从现网 HAR 提取生图链路请求，与 bench 的 picture_v2 body 逐字段 diff。

只输出结构化摘要，避免把 embed 的 base64 图片灌进 stdout。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CAP = ROOT / "docs" / "captures" / "spa"


def _latest_har() -> Path:
    hars = sorted(CAP.glob("spa-image-*.har"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hars:
        raise SystemExit("no spa-image HAR found")
    return hars[0]


def _clip(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"...<+{len(s)-n}>"


def _summarize_body(body: dict) -> dict:
    """列出 top-level 键 + 关键子结构，图片/长文本裁剪。"""
    out = {}
    for k, v in body.items():
        if k == "messages" and isinstance(v, list):
            msgs = []
            for m in v:
                md = m.get("metadata") or {}
                content = m.get("content") or {}
                msgs.append(
                    {
                        "author": (m.get("author") or {}).get("role"),
                        "content_type": content.get("content_type"),
                        "parts_kinds": [
                            (p.get("content_type") if isinstance(p, dict) else "text")
                            for p in (content.get("parts") or [])
                        ],
                        "metadata_keys": sorted(md.keys()),
                        "metadata_system_hints": md.get("system_hints"),
                    }
                )
            out[k] = msgs
        else:
            out[k] = _clip(v, 200)
    return out


def main() -> int:
    har_path = _latest_har()
    har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    entries = har.get("log", {}).get("entries", [])

    conv_posts = []
    prepare_posts = []
    image_signals = {"async_task": 0, "image_gen": 0, "image_asset_pointer": 0, "file_service_file": 0, "dalle": 0}
    for e in entries:
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = req.get("url") or ""
        method = req.get("method")
        # 统计生图信号（在响应/SSE 文本里）
        rtext = ((resp.get("content") or {}).get("text")) or ""
        if "/f/conversation" in url or "/backend-api/conversation" in url:
            if "async_task" in rtext:
                image_signals["async_task"] += 1
            if '"image_gen"' in rtext or "image_gen" in rtext:
                image_signals["image_gen"] += 1
            if "image_asset_pointer" in rtext:
                image_signals["image_asset_pointer"] += 1
            if re.search(r"file-service://file-", rtext):
                image_signals["file_service_file"] += 1
            if "dalle" in rtext.lower():
                image_signals["dalle"] += 1

        if method != "POST":
            continue
        if url.endswith("/f/conversation/prepare"):
            text = ((req.get("postData") or {}).get("text")) or ""
            try:
                body = json.loads(text)
            except Exception:
                body = {}
            prepare_posts.append({"body_keys": sorted(body.keys()), "system_hints": body.get("system_hints"), "summary": _summarize_body(body)})
        elif re.search(r"/f/conversation$", url):
            text = ((req.get("postData") or {}).get("text")) or ""
            try:
                body = json.loads(text)
            except Exception:
                body = {}
            hdrs = {}
            for h in req.get("headers") or []:
                name = str(h.get("name", ""))
                low = name.lower()
                if low.startswith("openai-sentinel") or low in ("x-conduit-token", "x-oai-turn-trace-id", "oai-device-id", "oai-client-version", "oai-client-build-number", "accept"):
                    hdrs[name] = _clip(h.get("value"), 60)
            conv_posts.append(
                {
                    "top_keys": sorted(body.keys()),
                    "system_hints": body.get("system_hints"),
                    "header_keys": sorted(hdrs.keys()),
                    "headers": hdrs,
                    "summary": _summarize_body(body),
                }
            )

    # bench 生成的 picture_v2 body（对齐生产）
    from services.protocol.chatgpt_web_request import build_image_start_body, build_image_prepare_body

    bench_start = build_image_start_body("a simple flat blue circle icon on white background, no text", "auto", spa_tool_path=False)
    bench_prepare = build_image_prepare_body("a simple flat blue circle icon on white background, no text", "auto", spa_tool_path=False)

    # 找现网带 picture_v2 的那个 conversation POST
    live_pic = next((c for c in conv_posts if c.get("system_hints") == ["picture_v2"]), None)

    report = {
        "har": str(har_path.name),
        "har_bytes": har_path.stat().st_size,
        "image_signals": image_signals,
        "prepare_count": len(prepare_posts),
        "conv_post_count": len(conv_posts),
        "conv_system_hints_seq": [c.get("system_hints") for c in conv_posts],
        "live_picture_v2_conv": live_pic,
        "bench_start_top_keys": sorted(bench_start.keys()),
        "bench_start_msg0_meta_keys": sorted((bench_start["messages"][0].get("metadata") or {}).keys()),
        "bench_start_msg0_system_hints": (bench_start["messages"][0].get("metadata") or {}).get("system_hints"),
        "bench_prepare_keys": sorted(bench_prepare.keys()),
    }

    # key-level diff（现网 picture_v2 conv vs bench_start）
    if live_pic:
        live_keys = set(live_pic["top_keys"])
        bench_keys = set(bench_start.keys())
        report["top_key_diff"] = {
            "only_in_live": sorted(live_keys - bench_keys),
            "only_in_bench": sorted(bench_keys - live_keys),
            "common": sorted(live_keys & bench_keys),
        }
        live_meta = set((live_pic["summary"].get("messages") or [{}])[0].get("metadata_keys") or [])
        bench_meta = set((bench_start["messages"][0].get("metadata") or {}).keys())
        report["msg_metadata_key_diff"] = {
            "only_in_live": sorted(live_meta - bench_meta),
            "only_in_bench": sorted(bench_meta - live_meta),
            "common": sorted(live_meta & bench_meta),
        }
        report["live_header_keys"] = live_pic["header_keys"]

    out_path = CAP / "field-diff-picture_v2-live-vs-bench.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("WROTE", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
