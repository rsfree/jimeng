#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mj82（图片美学模型 V8.2）真跑探针：文生图 + 图生图 + 数量可控性。

只跑最小样本，每发都记submit_id 供事后按submit_id 对账。
余额已在探针外记录（5749）。
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

results: list[dict] = []


def record(**kw) -> None:
    results.append(kw)
    print(json.dumps(kw, ensure_ascii=False))


def wait(client: JimengClient, sid: str, budget: int = 240) -> dict:
    """轮询到终态（只读）。"""
    t0 = time.time()
    seen = None
    while time.time() - t0 < budget:
        st = client.fetch(sid)
        cur = (st.status, getattr(st, "finished", None), len(getattr(st, "images", []) or []))
        if cur != seen:
            print(f"    [{int(time.time() - t0):3d}s] status={cur[0]} "
                  f"finished={cur[1]} images={cur[2]}")
            seen = cur
        if st.status in ("success", "failure", "canceled"):
            return {"status": st.status,
                    "images": [getattr(i, "url", None) for i in (getattr(st, "images", []) or [])],
                    "finished": getattr(st, "finished", None),
                    "elapsed": round(time.time() - t0, 1)}
        time.sleep(3)
    return {"status": "timeout", "elapsed": round(time.time() - t0, 1)}


def main() -> None:
    c = JimengClient(os.environ["JIMENG_SESSIONID"])
    print(f"mj82 探针开始  model={MJ}\n")

    # ---- 1. t2i：默认张数（不传 count_options ⇒ 走 [4] 吸附）----
    print("=== 1. t2i（n 不给 ⇒ 取 min(options)=4）===")
    sid = c.submit(PROMPT, model=MJ, size="1024x1024", count=1)
    print(f"  submit_id={sid}")
    r = wait(c, sid)
    record(case="t2i_n_omitted", submit_id=sid, requested_n=None, **r)
    print()

    # ---- 2. t2i：显式要 1 张（验证是否可控）----
    print("=== 2. t2i（显式 n=1 ⇒ 看上游给几张）===")
    sid = c.submit(PROMPT, model=MJ, size="1024x1024", count=1,
                   count_options=(1,))
    print(f"  submit_id={sid}  告警={c.last_warnings}")
    r = wait(c, sid)
    record(case="t2i_n1_explicit", submit_id=sid, requested_n=1, **r)
    print()

    # ---- 3. t2i：2k 分辨率 ----
    print("=== 3. t2i（2k，验证分辨率档位）===")
    sid = c.submit(PROMPT, model=MJ, size="2048x2048", count=4,
                   count_options=(4,))
    print(f"  submit_id={sid}")
    r = wait(c, sid)
    record(case="t2i_2k", submit_id=sid, requested_n=4, **r)
    print()

    # ---- 4. i2i（blend）：mj82 是否吃垫图 ----
    print("=== 4. i2i blend（用 case1 的产物当垫图）===")
    prev = [x for x in results if x["case"] == "t2i_n_omitted" and x.get("images")]
    if not prev:
        print("  跳过：case1 没出图")
    else:
        uri = prev[0]["images"][0]
        sid = c.blend("make it blue and glossy", image_url=uri, model=MJ,
                      size="1024x1024", count=1, count_options=(1,))
        print(f"  submit_id={sid}  告警={c.last_warnings}")
        r = wait(c, sid)
        record(case="i2i_mj82", submit_id=sid, requested_n=1, **r)

    out = REPO / "scripts" / ".mj82_probe_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {out}")
    print("\n=== 汇总 ===")
    for r in results:
        print(f"  {r['case']:18s} status={r.get('status')} "
              f"出图={len(r.get('images') or [])} 用时={r.get('elapsed')}s "
              f"submit_id={r['submit_id'][:8]}")


if __name__ == "__main__":
    main()
