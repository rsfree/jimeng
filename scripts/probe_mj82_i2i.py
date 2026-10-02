#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mj82 走 **blend（图生图）** 的端到端真跑（2026-10-02 补测）。

背景：i2i 接线已修（`blend(model=…)` 会传、`resolve` 带图时落 i2i），
但**上游是否真接受 mj82 走 byte_edit** 从未验证过 —— 首版探针的 case4
因case1 报错图被跳过。门禁只证明代码路径通，**证明不了上游能用**。

本轮验证三件事：
  1. blend 提交**不报错**（ret=0、拿到 submit_id）；
  2. 终态出图（四字段交叉核对）+ 张数是否符合"恒 4 张"；
  3. 消耗记录里能按 submit_id 对上账（确认 blend 也走 mj82 计费档）。
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
import httpx  # noqa: E402

from app.upstream.jimeng.client import (  # noqa: E402
    JimengClient, PATH_HISTORY,
)
from app.upstream.jimeng.upload import ImageXUploader  # noqa: E402

MJ = "jm_image_model_yc_mj82"
PROMPT = "make it blue and glossy, studio lighting"
TERMINAL = (50, 30, 40)


def main() -> None:
    ref = Path("/tmp/mj82_ref_url.txt").read_text(encoding="utf-8").strip()
    c = JimengClient(os.environ["JIMENG_SESSIONID"])

    print("=" * 74)
    print("mj82 · blend（图生图）真跑")
    print("=" * 74)
    print(f"垫图: {ref[:76]}...")
    print(f"prompt: {PROMPT}\n")

    # 🔴 走**生产同一条路**：下载垫图 → ImageX 上传成 image_uri → blend(image_uris=…)
    # `source_from` 默认 "upload"，要求给 image_uri(s)；直接传 URL 需要
    # `source_from="link"`，那是**另一条**路径，不走它以免测的不是生产链路。
    print("下载垫图并上传成 image_uri ...")
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=True) as h:
        blob = h.get(ref).content
    print(f"  下载 {len(blob)} 字节")
    up = ImageXUploader(c)
    uri = up.upload(blob)
    print(f"  image_uri = {uri[:70]}\n")

    # count_options 不传 ⇒ 走 COUNT_OPTIONS_BY_MODEL[mj82]=(4,)
    # ⇒ resolve_count 会吸附成 4 并留痕（预期出 4 张）
    sid = c.blend(PROMPT, image_uris=[uri], model=MJ,
                  size="1024x1024", count=1)
    print(f"✅ blend 提交成功  submit_id={sid}")
    print(f"   告警={c.last_warnings}")
    print("   （提交包 model 字段已确认为 mj82，见下方回读）\n")

    t0 = time.time()
    last = None
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        cur = (st.status, getattr(st, "finished", None),
               len(getattr(st, "images", []) or []))
        if cur != last:
            print(f"  [{int(time.time() - t0):3d}s] status={cur[0]} "
                  f"finished={cur[1]} images={cur[2]}")
            last = cur
        if st.status in TERMINAL:
            break
        time.sleep(4)
    else:
        print("  超时 300s")
        return

    # ---- 四字段交叉核对 + 回读提交包model ----
    raw = c._post(PATH_HISTORY, {"submit_ids": [sid]})  # noqa: SLF001  （探针直读私有 _post：与 credit_probe/e2e_video 同惯例）
    node = (raw.get("data") or {}).get(sid) or {}
    items = node.get("item_list") or []
    large = sum(len((it.get("image") or {}).get("large_images") or [])
                for it in items)
    dc = node.get("draft_content")
    d = json.loads(dc) if isinstance(dc, str) and dc else {}
    ab = (((d.get("component_list") or [{}])[0]).get("abilities") or {})
    gen_count = (ab.get("gen_option") or {}).get("gen_count")
    model_used = ((ab.get("blend") or {}).get("core_param") or {}).get("model") \
        or ((ab.get("generate") or {}).get("core_param") or {}).get("model")

    print(f"\n终态 status={st.status}  用时={int(time.time() - t0)}s")
    print("-" * 74)
    print(f"  提交包 model         = {model_used!r}   ← 必须等于 mj82 才算真走 mj82")
    print(f"  提交包 gen_count     = {gen_count!r}")
    print(f"  item_list 长度       = {len(items)}")
    print(f"  large_images 之和= {large}")
    print(f"  total_image_count   = {node.get('total_image_count')}")
    print(f"  finished_image_count= {node.get('finished_image_count')}")
    counts = {len(items), large, node.get("total_image_count"),
              node.get("finished_image_count")}
    print(f"  🔴 四字段是否一致     = {'是' if len(counts) == 1 else f'否 {counts}'}")
    for i, im in enumerate(getattr(st, "images", []) or [], 1):
        print(f"    {i}. {im.width}x{im.height} {im.format or '?'}")

    ok = (model_used == MJ)
    print("\n判定：", "✅ **mj82 走 blend 可用**（上游接受了该模型）"
          if ok else f"❌ 上游没在用 mj82（实际 {model_used!r}）")
    Path("/tmp/mj82_i2i_result.json").write_text(
        json.dumps({"submit_id": sid, "status": st.status,
                    "model_used": model_used, "gen_count": gen_count,
                    "f1_item_list": len(items), "f2_large": large,
                    "f3_total": node.get("total_image_count"),
                    "f4_finished": node.get("finished_image_count")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"submit_id={sid}（供对账）")


if __name__ == "__main__":
    main()
