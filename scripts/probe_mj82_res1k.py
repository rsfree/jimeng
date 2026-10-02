#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性验证：mj82 传 `resolution_type="1k"` 能真出 1024²吗？（2026-10-02）

问题：`build_draft(resolution_type="2k")` **硬编码**，且 `submit()` 不暴露该
参数 ⇒ 我们历史上所有单子（哪怕传 size=1024x1024）**都是 2k 档**。
而服务端 `default_resolution_type` 是 **"1k"** ⇒ 默认口径与实际落点**不一致**。

本轮绕过 `submit()`，直接构造草稿 + 自建提交，确认 1k 档真的能跑、
且产物确实是 1024²（而不是被上游按 2k 出）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")
from app.upstream.jimeng.client import (  # noqa: E402
    JimengClient, PATH_HISTORY, PATH_SUBMIT, build_draft, APPID,
)

MJ = "jm_image_model_yc_mj82"
PROMPT = "a red apple on a wooden table, studio light"
TERMINAL = (50, 30, 40)


def submit_with(c: JimengClient, resolution_type: str) -> str:
    """按指定resolution_type 构造并提交（绕开 submit() 的硬编码 2k）。"""
    draft = build_draft(prompt=PROMPT, model=MJ, count=4,
                        width=1024, height=1024,
                        resolution_type=resolution_type)
    sid = str(uuid.uuid4())
    body = {
        "extend": {"root_model": MJ},
        "submit_id": sid,
        "metrics_extra": json.dumps({
            "promptSource": "custom", "generateCount": 4,
            "enterFrom": "click", "position": "page_bottom_box",
        }, separators=(",", ":")),
        "draft_content": draft,
        "http_common_info": {"aid": int(APPID)},
    }
    # 必须自建提交包：`submit()` **刻意不暴露** `resolution_type`
    # （那正是被查的 bug：该字段曾硬编码 2k）。要验证"显式传 1k 能不能生效"
    # 只能绕过公开封装 —— 走 `submit()` 这个 bug 根本测不出来。
    c._post(PATH_SUBMIT, body)   # noqa: SLF001
    return sid


def main() -> None:
    c = JimengClient(os.environ["JIMENG_SESSIONID"])
    sid = submit_with(c, "1k")
    print(f"提交 resolution_type=1k（width/height=1024）  submit_id={sid}")

    t0 = time.time()
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        if st.status in TERMINAL:
            break
        time.sleep(4)
    else:
        print("超时")
        return

    # 同上：读原始报文（绕过 `fetch()` 的解析器）才能拿到"上游最终采用的
    # gen_count"与四个独立的出图计数字段 —— 判"实际几张"必须四者交叉核对。
    n = (c._post(PATH_HISTORY, {"submit_ids": [sid]}).get("data") or {}).get(sid) or {}  # noqa: SLF001
    dc = n.get("draft_content")
    d = json.loads(dc) if isinstance(dc, str) and dc else {}
    lii = ((((d.get("component_list") or [{}])[0]).get("abilities") or {})
           .get("generate", {}).get("core_param", {})).get("large_image_info") or {}
    items = n.get("item_list") or []
    sizes = []
    for it in items:
        li = (it.get("image") or {}).get("large_images") or []
        if li and isinstance(li[0], dict):
            sizes.append((li[0].get("width"), li[0].get("height")))

    print(f"终态 status={st.status}  用时={int(time.time() - t0)}s")
    print("-" * 62)
    print(f"  草稿 large_image_info = {lii.get('width')}x{lii.get('height')} "
          f"/ resolution_type={lii.get('resolution_type')!r}")
    print(f"  实际产物尺寸= {sizes[:3]}{'…' if len(sizes) > 3 else ''}")
    print(f"  产物张数          = {len(items)}")
    got = sizes[0] if sizes else (None, None)
    ok = got == (1024, 1024)
    print()
    print("判定：", "✅ **resolution_type=1k 真的生效**，产物 1024²"
          if ok else f"❌ 传 1k 但出{got} ⇒ 该字段被上游忽略/改写")
    Path("/tmp/mj82_res1k.json").write_text(json.dumps(
        {"submit_id": sid, "status": st.status, "draft": lii,
         "sizes": sizes, "is_1k": ok}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"submit_id={sid}")


if __name__ == "__main__":
    main()
