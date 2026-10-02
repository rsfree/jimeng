#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性验证：mj82 的 `gen_count` 到底生效吗？（2026-10-02 续）

背景：服务端能力表声明 `generate_count_options=[4]`，但探针实测
  · 请求 n=1（我们传 gen_count=1）→ 出 1 张
两者矛盾，必须用**一次只改一个变量**的实验定性：
传 gen_count=3，若出 3 张 ⇒ 可控（能力表的 [4] 只是"默认/推荐值"）；
若仍出 4 张 ⇒ 不可控。
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


def main() -> None:
    c = JimengClient(os.environ["JIMENG_SESSIONID"])
    # 关键：`count_options=(1,2,3,4)` 让resolve_count **不吸附**，
    # 原样把 3 写进 gen_option.gen_count
    sid = c.submit(PROMPT, model=MJ, size="1024x1024", count=3,
                   count_options=(1, 2, 3, 4))
    print(f"提交 gen_count=3  submit_id={sid}\n")

    t0 = time.time()
    last = None
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        n = len(getattr(st, "images", []) or [])
        cur = (st.status, getattr(st, "finished", None), n)
        if cur != last:
            print(f"  [{int(time.time() - t0):3d}s] status={cur[0]} "
                  f"finished={cur[1]} images={cur[2]}")
            last = cur
        # 🔴 终态判定：status=50 即成功（记忆里的"45 是中间态"）。
        # 上一版探针把50 当成非终态，空转到 240s 超时 —— 那是探针的 bug，
        # 不是上游的问题。
        if st.status in (50, 30, 40):
            imgs = getattr(st, "images", []) or []
            print(f"\n终态 status={st.status}  出图={len(imgs)}")
            for i, im in enumerate(imgs):
                print(f"   {i+1}. {getattr(im,'width','?')}x{getattr(im,'height','?')}")
            print("\n判定：", "gen_count **生效**（可控）" if len(imgs) == 3
                  else f"gen_count **不生效**（要3 张给了 {len(imgs)} 张）")
            (REPO / "scripts" / ".mj82_genncount_probe.json").write_text(
                json.dumps({"submit_id": sid, "requested": 3,
                            "got": len(imgs), "status": st.status},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            return
        time.sleep(3)
    print("超时（300s）—— 仍按未出完处理，不猜")


if __name__ == "__main__":
    main()
