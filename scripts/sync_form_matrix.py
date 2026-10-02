#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步出图多形态矩阵 —— **默认只跑免费档**。

形态覆盖：t2i 的 8 种比例（ratio_type 1..8）+ 多张数 + 参数形态 + i2i 多垫图
+ hd 超清 + detail-fix 引用形态 + outpaint。

🔴 **默认档位的实扣**：t2i(Lite) / hd / detail-fix 实测 0；**i2i 存疑**
（2026-10-02 观测到两笔 amount=7，但同账号网页端并发在跑、未按 submit_id
定案 ⇒ 见 .workbuddy/memory/2026-10-02.md）；**outpaint 实测 1**（forecast 35，
高估 35 倍）。要跑后两者用 `--include-costly` 并自担费用。

🔴 串行而非并发：服务端 JM_CONCURRENCY=3，而 WORKERS=1 —— 同步端点是
`def`（阻塞线程池），并发压测会把唯一的 worker 占满，反而测不出真实行为。

用法：
    JM_API_KEY=<key> python scripts/sync_form_matrix.py
    JM_API_KEY=<key> python scripts/sync_form_matrix.py --include-costly
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("JM_BASE", "http://jimeng.1task.cn")
#: 惰性缓存的 API Key（`_key()` 首次调用时解析）。
_KEY: str | None = None
EP = f"{BASE}/v1/images/generations"
LIST_EP = f"{BASE}/async/v1/images/generations"


def _key() -> str:
    """取 API Key。优先环境变量；退回仓库 .env 的 `API_KEYS` 第一把。

    🔴 **不读 /tmp 下的临时文件** —— 那是上一次跑测的残留，换机器就没了。
    """
    global _KEY
    if _KEY:
        return _KEY
    k = os.environ.get("JM_API_KEY", "").strip()
    if k:
        _KEY = k
        return _KEY
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.startswith("API_KEYS="):
                first = line.split("=", 1)[1].split(",")[0].strip().strip("\"'")
                if first:
                    _KEY = first
                    return _KEY
    raise SystemExit("缺少 API Key：给 JM_API_KEY=<key> 或在仓库 .env 写 API_KEYS")


#: docs/UPSTREAM.md §「枚举值逐项复核」的 2K 像素表，逐项照抄不自行推算。
RATIO_2K = [
    ("1:1", "2048x2048"), ("3:4", "1728x2304"), ("16:9", "2560x1440"),
    ("4:3", "2304x1728"), ("9:16", "1440x2560"), ("2:3", "1664x2496"),
    ("3:2", "2496x1664"), ("21:9", "3024x1296"),
]
PROMPT = "一只在窗台上晒太阳的橘猫，柔和的自然光，浅景深"



def _req(url: str, body: dict | None, method: str, timeout: float) -> tuple[int, dict, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {_key()}",
                 "Content-Type": "application/json"},
        method=method,
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), time.time() - t0


def post(body: dict, timeout: float = 320.0) -> tuple[int, dict, float]:
    return _req(EP, body, "POST", timeout)


def newest_task_id(model: str) -> str | None:
    """按模型取**最近一个**任务的 id（列表端点倒序）。

    🔴 为什么必须走这一层：同步端点**成功响应体里没有 `task_id`** ——
    `view()` 只在 202（排队中/预算耗尽）时回它，success 分支只给
    `status/data/created/usage`。所以想串联 `detail-fix` 这类**引用形态**，
    只能从列表端点反查。踩过：直接取 `resp["task_id"]` 恒为 `None`，
    于是 `source_task_id` 传空 → 400「必须给 source_task_id」，看起来
    像能力坏了，其实是脚本没拿到 id。
    """
    _, body, _ = _req(LIST_EP, None, "GET", 30)
    for t in body.get("items") or []:
        if t.get("model") == model:
            return t.get("task_id")
    return None


def run(label: str, body: dict) -> dict:
    code, resp, dt = post(body)
    st = resp.get("status", "-")
    n = len(resp.get("data") or [])
    deg = resp.get("degradations") or []
    fc = (resp.get("usage") or {}).get("forecast_credits", "-")
    ok = code == 200 and st == "success"
    line = (f"  {'✅' if ok else '❌'} {label:26s} {code}/{st:8s} "
            f"{dt:5.1f}s  n={n} forecast={fc}")
    if deg:
        line += f"  降级{len(deg)}条"
    print(line, flush=True)
    for d in deg:
        print(f"       ⚠️ {d[:120]}")
    if not ok:
        err = resp.get("error") or {}
        print(f"       → {err.get('code')}: {str(err.get('message'))[:160]}")
    return {"label": label, "code": code, "status": st, "elapsed": round(dt, 1),
            "images": n, "forecast": fc, "degradations": deg,
            "task_id": resp.get("task_id"),
            "url": (resp.get("data") or [{}])[0].get("url"),
            "error": resp.get("error")}



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-costly", action="store_true",
                    help="连 outpaint 一起跑（实测 1 积分/次）")
    ap.add_argument("--out", default="/tmp/jm_multiform.json",
                    help="明细 JSON 落盘路径")
    args = ap.parse_args()
    results: list[dict] = []

    print("\n── A · t2i 八种比例（免费 Lite） " + "─" * 22)
    for rname, size in RATIO_2K:
        results.append(run(f"t2i {rname} {size}",
                           {"model": "jimeng-t2i", "prompt": PROMPT,
                            "image": [], "size": size, "n": 1}))

    print("\n── B · 张数吸附（Lite 声明 1..8） " + "─" * 20)
    for n in (2, 4, 8):
        results.append(run(f"t2i n={n}",
                           {"model": "jimeng-t2i", "prompt": PROMPT,
                            "image": [], "size": "2048x2048", "n": n}))
    print("  ── 越界张数应被吸附并留痕 ──")
    results.append(run("t2i n=3（合法）",
                       {"model": "jimeng-t2i", "prompt": PROMPT,
                        "image": [], "size": "2048x2048", "n": 3}))

    print("\n── C · seed / negative_prompt / 面板名 " + "─" * 16)
    results.append(run("t2i seed 固定",
                       {"model": "jimeng-t2i", "prompt": PROMPT, "image": [],
                        "size": "2048x2048", "n": 1, "seed": 42}))
    results.append(run("t2i negative_prompt",
                       {"model": "jimeng-t2i", "prompt": PROMPT, "image": [],
                        "size": "2048x2048", "n": 1,
                        "negative_prompt": "模糊, 水印, 多余的手指"}))
    results.append(run("t2i 面板名 5.0 Lite",
                       {"model": "Seedream 5.0 Lite", "prompt": PROMPT,
                        "image": [], "size": "2048x2048", "n": 1}))
    results.append(run("t2i 不写 model",
                       {"prompt": PROMPT, "image": [],
                        "size": "2048x2048", "n": 1}))

    print("\n── D · 降级字段（认得但做不到，应进 degradations 而非报错） " + "─" * 8)
    results.append(run("t2i + response_format/watermark",
                       {"model": "jimeng-t2i", "prompt": PROMPT, "image": [],
                        "size": "2048x2048", "n": 1,
                        "response_format": "b64_json", "watermark": True,
                        "quality": "hd", "style": "vivid"}))

    print("\n── E · i2i 多垫图（⚠️ 实扣存疑，见 memory 2026-10-02） " + "─" * 8)
    base = next((r for r in results if r.get("url")), None)
    if not base:
        print("  ❌ 没有可复用的产物 URL，i2i/hd/detail-fix 跳过")
        return 1
    u = base["url"]
    for k in (1, 2, 4):
        results.append(run(f"i2i 垫图×{k}",
                           {"model": "jimeng-i2i",
                            "prompt": "把猫改成一只黑猫，其余不变",
                            "image": [u] * k, "size": "2048x2048", "n": 1}))

    print("\n── F · hd 超清（免费，引用 i2i 产物） " + "─" * 14)
    i2i = next((r for r in results if r["label"].startswith("i2i 垫图×1")
                and r.get("url")), None)
    if i2i:
        results.append(run("hd 超清",
                           {"model": "jimeng-hd", "image": [i2i["url"]],
                            "size": "2048x2048", "n": 1}))
    else:
        print("  ⚠️ i2i 未产出 URL，hd 跳过")

    print("\n── G · detail-fix 引用形态（两步，引用 i2i 产物） " + "─" * 8)
    i2i_tid = newest_task_id("jimeng-i2i")
    if i2i_tid:
        print(f"      引用源 = 最近一个 i2i 任务 {i2i_tid}")
        results.append(run("detail-fix 引用 i2i",
                           {"model": "jimeng-detail-fix",
                            "source_task_id": i2i_tid}))
    else:
        print("  ⚠️ 列表端点没找到 i2i 任务，detail-fix 跳过")

    if args.include_costly:
        print("\n── H · outpaint 扩图（🔴 实测 1 分/次，forecast 报 35） " + "─" * 4)
        t2i_tid = newest_task_id("jimeng-t2i")
        imgs = []
        if t2i_tid:
            _, src, _ = _req(f"{LIST_EP}/{t2i_tid}", None, "GET", 30)
            imgs = src.get("data") or []
        if imgs:
            results.append(run("outpaint 扩图",
                               {"model": "jimeng-outpaint",
                                "image": [imgs[0]["url"]], "n": 1}))
        else:
            print("  ⚠️ 源任务无产物 URL，outpaint 跳过")
    else:
        print("\n── H · outpaint 已跳过（加 --include-costly 才跑） " + "─" * 12)

    json.dump(results, open(args.out, "w"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in results if r["code"] == 200 and r["status"] == "success")
    print("\n" + "=" * 58)
    print(f"结果：{ok}/{len(results)} 成功；明细已写 {args.out}")
    print("⚠️ forecast_credits 是**预估**不是实扣 —— 对账走 "
          "scripts/credit_probe.py --find <submit_id>")
    for r in results:
        if r["code"] != 200 or r["status"] != "success":
            print(f"  ❌ {r['label']}: {r['code']}/{r['status']} "
                  f"{(r.get('error') or {}).get('message', '')[:100]}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _key()          # 缺 key 时尽早报错，别跑到一半才失败
    sys.exit(main())
