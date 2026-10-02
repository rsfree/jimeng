#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重测 mj82 在 `gen_count=1` 时的真实出图数（2026-10-02，用户要求复验）。

上轮结论"gen_count=1 → 出 1 张"只有**一次**样本，且当时探针把status=50
误当非终态、空转到超时才返回。虽有四个字段交叉核对，本轮仍要：
  · **跑两发**排除偶发；
  · 显式以 `status=50` 为终态（45 是中间态）；
  · 出图张数用**四个独立字段**交叉核对（不信单一字段）——
    `item_list` 长度 / 各 item 的 `large_images` 之和 /
    `total_image_count` / `finished_image_count`；
  · 同时回读回执里的 `gen_count`，看上游有没有二次改写。
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
from app.upstream.jimeng.client import (  # noqa: E402
    JimengClient, PATH_HISTORY,
)

MJ = "jm_image_model_yc_mj82"
PROMPT = "a red apple on a wooden table, studio light"
TERMINAL = (50, 30, 40)      # 50=成功（记忆里的口径），30=失败，40=取消


def submit_one(c: JimengClient, n: int, run: int) -> dict:
    # count_options=(1,) ⇒ resolve_count 不吸附，原样写入 gen_count=n
    sid = c.submit(PROMPT, model=MJ, size="1024x1024", count=n,
                   count_options=(1,))
    print(f"\n[run{run}] 提交 gen_count={n}  submit_id={sid}")
    t0 = time.time()
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        if st.status in TERMINAL:
            print(f"[run{run}] 终态 status={st.status}用时={int(time.time() - t0)}s")
            return {"run": run, "sent": n, "submit_id": sid,
                    "status": st.status, "elapsed": int(time.time() - t0)}
        time.sleep(4)
    print(f"[run{run}] 超时 300s")
    return {"run": run, "sent": n, "submit_id": sid, "status": "timeout"}


def crosscheck(c: JimengClient, results: list[dict]) -> list[dict]:
    """用四个独立字段数张数 + 回读回执的 gen_count。"""
    raw = c._post(PATH_HISTORY,
                  {"submit_ids": [r["submit_id"] for r in results]})
    data = raw.get("data") or {}
    out = []
    for r in results:
        node = data.get(r["submit_id"]) or {}
        items = node.get("item_list") or []
        # 字段2：各 item 的 large_images 逐个数（不是只取 [0]）
        large_total = sum(
            len((it.get("image") or {}).get("large_images") or [])
            for it in items
        )
        # 字段3：回执里上游最终采用的 gen_count
        dc = node.get("draft_content")
        d = json.loads(dc) if isinstance(dc, str) and dc else {}
        ab = (((d.get("component_list") or [{}])[0]).get("abilities") or {})
        receipt_count = (ab.get("gen_option") or {}).get("gen_count")
        out.append({
            **r,
            "f1_item_list": len(items),
            "f2_large_images": large_total,
            "f3_total_image_count": node.get("total_image_count"),
            "f4_finished_image_count": node.get("finished_image_count"),
            "receipt_gen_count": receipt_count,
        })
    return out


def main() -> None:
    c = JimengClient(os.environ["JIMENG_SESSIONID"])
    print("=" * 78)
    print("mj82 gen_count=1 复验（两发，四个独立字段交叉核对）")
    print("=" * 78)

    results = [submit_one(c, 1, 1), submit_one(c, 1, 2)]
    print("\n等待结算后回查原始报文 ...")
    time.sleep(5)
    rows = crosscheck(c, results)

    print("\n" + "=" * 78)
    print(f"{'run':>4s} {'发送':>4s} {'回执gen_count':>13s} {'item_list':>10s} "
          f"{'largeImg':>9s} {'total':>6s} {'finished':>9s} {'一致?':>6s}")
    print("-" * 78)
    for r in rows:
        counts = {r["f1_item_list"], r["f2_large_images"],
                  r["f3_total_image_count"], r["f4_finished_image_count"]}
        agree = "是" if len(counts) == 1 else "❌不一致"
        print(f"{r['run']:>4d} {r['sent']:>4d} {str(r['receipt_gen_count']):>13s} "
              f"{r['f1_item_list']:>10d} {r['f2_large_images']:>9d} "
              f"{str(r['f3_total_image_count']):>6s} "
              f"{str(r['f4_finished_image_count']):>9s} {agree:>6s}")

    print("\n判定：")
    for r in rows:
        counts = {r["f1_item_list"], r["f2_large_images"],
                  r["f3_total_image_count"], r["f4_finished_image_count"]}
        n = counts.pop() if len(counts) == 1 else None
        if n is None:
            print(f"  run{r['run']}: 字段不一致，需人工查（{counts}）")
        elif n == 1:
            print(f"  run{r['run']}: **确认出 1 张**（四字段一致，上游未改写 gen_count）")
        else:
            print(f"  run{r['run']}: 出 {n} 张 ⇒与 gen_count=1 **不符**，"
                  f"上轮结论需修正")

    (REPO / "scripts" / ".mj82_recount_result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
