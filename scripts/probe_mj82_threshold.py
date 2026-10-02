#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位 mj82 的张数真实规则：gen_count=1 和 3 行为不同（2026-10-02 续）。

已实测：
  gen_count=1 → 出 1 张
  gen_count=3 → 出 4 张
  gen_count=4 → 出 4 张
⇒ 假设：**gen_count<=1 时照办，>=2（或 >1）时被抬到 4**（= 服务端默认值）。
本轮补 gen_count=2 定位阈值；若 2 也出 4 张 ⇒ 规则是"只有 1 可控"。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")
from app.upstream.jimeng.client import JimengClient  # noqa: E402

MJ = "jm_image_model_yc_mj82"
PROMPT = "a red apple on a wooden table, studio light"


def run(c: JimengClient, n: int) -> dict:
    sid = c.submit(PROMPT, model=MJ, size="1024x1024", count=n,
                   count_options=(1, 2, 3, 4))   # 不吸附，原样写入
    print(f"\n提交 gen_count={n}  submit_id={sid}")
    t0 = time.time()
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        if st.status in (50, 30, 40):
            imgs = getattr(st, "images", []) or []
            print(f"  终态 status={st.status} 出图={len(imgs)}张 "
                  f"用时={int(time.time() - t0)}s")
            return {"requested": n, "got": len(imgs), "status": st.status,
                    "submit_id": sid, "elapsed": int(time.time() - t0)}
        time.sleep(4)
    print("  超时")
    return {"requested": n, "got": None, "status": "timeout", "submit_id": sid}


def main() -> None:
    c = JimengClient(os.environ["JIMENG_SESSIONID"])
    out = [run(c, 2)]
    (REPO / "scripts" / ".mj82_threshold_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    r = out[0]
    print(f"\n判定：请求 {r['requested']} 张⇒ 实得 {r['got']} 张")
    print("（结合已测的 1→1、3→4：若2→4，说明**只有 n=1 可控**，"
          "n>=2 一律被抬到 4）")


if __name__ == "__main__":
    main()
