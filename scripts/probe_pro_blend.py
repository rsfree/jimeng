#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pro（方舟名）· 多图生图（blend）直连上游真跑 —— 2026-10-03。

背景：走线上 HTTP 端点验证时被两件事挡住——① 线上队列积压 40+条；
② 上游风控冷却（gate.cooling_for=418s）导致"槽位全空也不派发"。
本探针用**给定 sessionid 直连上游**，绕开调度层，单独验证两件事：

  1. 方舟名 `doubao-seedream-5-0-pro-260628` 能否解析到 Pro 上游 key；
  2. **多图生图是否生效** —— 权威判据= 草稿里
     `abilities.blend.ability_list[0].image_uri_list` 的长度（不是
     `core_param.large_image_info`，那个是**输出尺寸**，极易误读）。

三段式：dry_run（零计费，看草稿）→ 真跑（计费 8积分/张）→ 四字段交叉核对 + 对账。
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

# 注：原先这里有一行未使用的 `import httpx`（ruff F401），已删——本探针直连
# 上游走 JimengClient，不需要 httpx。
from app.upstream.jimeng.client import JimengClient, PATH_HISTORY  # noqa: E402
from app.upstream.jimeng.upload import ImageXUploader  # noqa: E402
from app.models import resolve, UPSTREAM_ARK_NAMES  # noqa: E402

# 凭据：`PROBE_SESSIONID` 优先，其次 `PROBE_COOKIE`（整串，多带无害），
# 最后回退到 .env。🔴 诊断纪律：**只读端点能过≠ 凭据可用**（实测 32 位 hex 的
# 凭据在 get_common_config 上 ret=0，但所有生成类端点 1015）⇒ 判定必须打
# 一次生成类端点（get_history_by_ids）。
SESSIONID = os.environ.get("PROBE_SESSIONID", "")
COOKIE = os.environ.get("PROBE_COOKIE", "")
if not SESSIONID and not COOKIE:
    SESSIONID = os.environ["JIMENG_SESSIONID"]

ARK = os.environ.get("PROBE_ARK", "doubao-seedream-5-0-pro-260628")
# 🔴 覆盖入口：想换上游档位时用 PROBE_UPSTREAM（如 high_aes_general_v50 = Lite，
#    2k 免费）。方舟名与上游 key 是**两条路**——方舟名走别名表解析，
#    PROBE_UPSTREAM 则直接指定上游 key（跳过别名解析，仍校验 blend 能力）。
FORCE_UPSTREAM = os.environ.get("PROBE_UPSTREAM", "")
PROMPT = os.environ.get("PROBE_PROMPT") or (
    "合成两张参考图为一张抽象海报：把图一的暖红色调与图二的蓝紫色调融合，"
    "中心明亮、两侧渐暗的柔和双重色调渐变")
TERMINAL = (50, 30, 40)
N_PAD = int(os.environ.get("PROBE_N_PAD", "2"))
#: 🔴 `PROBE_PADS` = 逗号分隔的**本地图片路径**，给了就用真图、不再造渐变图。
#:   （会话里粘贴的图不会落地成文件，需先存盘再传路径）
PAD_FILES = [x.strip() for x in os.environ.get("PROBE_PADS", "").split(",") if x.strip()]


def pad_images(n: int) -> list[bytes]:
    """垫图字节流。`PROBE_PADS` 给了本地路径就读真图，否则造**肉眼可区分**的渐变图。

    ⚠️ 真实业务里垫图是**内容图**（人像/商品），用纯渐变图只能验证"传了几张"，
    验证不了"内容有没有被参考"。要判后者必须传真图。
    """
    if PAD_FILES:
        out = []
        for f in PAD_FILES:
            p = Path(f).expanduser()
            if not p.is_file():
                raise SystemExit(f"垫图不存在：{p}")
            out.append(p.read_bytes())
        return out
    import struct
    import zlib

    def png(w: int, h: int, fn) -> bytes:
        raw = b""
        for y in range(h):
            raw += b"\x00"
            for x in range(w):
                raw += bytes(fn(x, y))
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6))
                + chunk(b"IEND", b""))

    fns = [
        lambda x, y: (x * 255 // 512, y * 40 // 512, 30),                    # 红横
        lambda x, y: (30, y * 255 // 512, x * 255 // 512),                   # 蓝纵
        lambda x, y: (x * 255 // 512, 200, y * 255 // 512),                  # 绿对角
        lambda x, y: (200, x * 255 // 512, y * 255 // 512),                  # 黄反向
    ]
    return [png(512, 512, fns[i % len(fns)]) for i in range(n)]


def draft_images(draft: dict) -> list[str]:
    """从草稿里取垫图 uri 列表 —— 权威字段。"""
    ab = (draft.get("component_list") or [{}])[0].get("abilities") or {}
    al = (ab.get("blend") or {}).get("ability_list") or []
    if not al:
        return []
    return list(al[0].get("image_uri_list") or [])


def main() -> None:
    print("=" * 78)
    print("方舟名 · 多图生图 blend · 直连上游真跑")
    print("=" * 78)

    # ---------- 0. 方舟名→ 上游 key 解析 ----------
    cap, key = (None, FORCE_UPSTREAM) if FORCE_UPSTREAM \
        else resolve(ARK, has_image=True, n_images=N_PAD)
    print(f"[0] 方舟名解析  {ARK}   垫图数={N_PAD}")
    print(f"    → capability={cap.api_id if cap else None}  upstream_key={key!r}")
    print(f"    → 登记的方舟名 ={UPSTREAM_ARK_NAMES.get(key)!r}")
    # 🔴 守卫只管"**方舟名**有没有被正确解析成它登记的那个上游 key"，
    # 绝不能限制"跑哪个档位"—— 否则传Flash / Lite 的方舟名会被误拦。
    # 正确性由下面两处保证：① `feats.byte_edit` 校验（图生图能力）
    # ② 最终判定拿草稿 model跟**本次实际请求的 key** 比。
    if not FORCE_UPSTREAM:
        _expect = UPSTREAM_ARK_NAMES.get(key)
        if _expect != ARK:
            print(f"    🔴 方舟名 {ARK} 解析出的key={key!r} 反查方舟名={_expect!r} 不一致 ⇒ 中止")
            return
        print("    → 方舟名↔ 上游 key 双向一致")
    # 判定凭据：只读端点 + 生成类端点分打（只看只读会误判）
    c = JimengClient(SESSIONID, cookie=COOKIE)
    try:
        _cc = c.common_config()
        _models = (_cc.get("data") or {}).get("model_list") or []
        print(f"[凭据] common_config ret={_cc.get('ret')} 模型数={len(_models)}")
    # 探针的**目的**就是"把任何上游失败打印出来并中止"，收窄异常类型会让
    # 某些失败变成 traceback —— 那正是这个探针要避免的。
    except Exception as _e:  # noqa: BLE001
        print(f"[凭据] 🔴 common_config 失败：{type(_e).__name__} {str(_e)[:90]}")
        return
    try:
        _h = c._post(PATH_HISTORY, {"submit_ids": ["0" * 8 + "-0000-0000-0000-" + "0" * 12]})  # noqa: SLF001
        print(f"[凭据] 生成类端点 ret={_h.get('ret') }  ← 这个才算数")
    # 同上：这里要的就是"任何失败都报出来"（凭据可能格式错、可能被风控、
    # 可能 ret 非0……），收窄类型会让诊断信息丢失。
    except Exception as _e:  # noqa: BLE001
        print(f"[凭据] 🔴 生成类端点失败：{type(_e).__name__} {str(_e)[:110]}")
        print("    ⇒ 凭据不可用（或账号被风控），后续必失败。")
        return

    # blend 能力**只认服务端 feats**（读不到 ⇒ 保守拒绝，不猜）
    _spec = next((m for m in _models
                  if m.get("model_req_key") == key), None)
    _feats = (_spec or {}).get("feats") or []
    print(f"[能力] {key} 服务端 feats 含 byte_edit = {'byte_edit' in _feats}")
    if "byte_edit" not in _feats:
        print("    🔴 服务端未声明 byte_edit ⇒ 不支持图生图，中止")
        return

    # ---------- 1. 上传垫图（不计生成费） ----------
    print(f"\n[1] 上传 {N_PAD} 张垫图到 ImageX ...")
    up = ImageXUploader(c)
    uris: list[str] = []
    _blobs = pad_images(N_PAD)
    print(f"    来源：{'本地文件 ' + str(PAD_FILES) if PAD_FILES else f'合成渐变图 ×{len(_blobs)}'}")
    for i, blob in enumerate(_blobs, 1):
        uri = up.upload(blob)
        uris.append(uri)
        print(f"    [{i}] {len(blob)}B → {uri}")

    # ---------- 2. dry_run：只验草稿结构，不发请求、零计费 ----------
    #🔴 只 build 草稿并本地检视，**不要**拿 dry_run 的 submit_id 去查 history：
    #   那个 id 从未提交过，上游对它的响应与鉴权无关（实测会ret=1015，
    #   用已验证可用的 sessionid 做对照同样 1015 ⇒ 不是凭据问题）。
    print("\n[2] dry_run 本地检视草稿（零计费、不发请求）...")
    dry = c.blend(PROMPT, image_uris=uris, model=key, size="1024x1024",
                  count=1, dry_run=True)
    print(f"    dry submit_id={dry}  告警={c.last_warnings}")

    # ---------- 3. 真跑 ----------
    print("\n[3] 真跑 blend（计费动作）...")
    t0 = time.time()
    sid = c.blend(PROMPT, image_uris=uris, model=key, size="1024x1024", count=1)
    print(f"    ✅ submit_id={sid}  告警={c.last_warnings}")

    last = None
    while time.time() - t0 < 300:
        st = c.fetch(sid)
        cur = (st.status, getattr(st, "finished", None), len(getattr(st, "images", []) or []))
        if cur != last:
            print(f"    [{int(time.time() - t0):3d}s] status={cur[0]} "
                  f"finished={cur[1]} images={cur[2]}")
            last = cur
        if st.status in TERMINAL:
            break
        time.sleep(4)
    else:
        print("    超时 300s")
        return

    # ---------- 4. 草稿回读：多图是否真进草稿 ----------
    raw = c._post(PATH_HISTORY, {"submit_ids": [sid]})  # noqa: SLF001
    node = (raw.get("data") or {}).get(sid) or {}
    dc = node.get("draft_content")
    draft = json.loads(dc) if isinstance(dc, str) and dc else {}
    in_draft = draft_images(draft)

    ab = ((draft.get("component_list") or [{}])[0].get("abilities") or {})
    core = ((ab.get("blend") or {}).get("core_param") or {})
    lii = core.get("large_image_info") or {}

    items = node.get("item_list") or []
    large = sum(len((it.get("image") or {}).get("large_images") or []) for it in items)
    counts = {len(items), large, node.get("total_image_count"), node.get("finished_image_count")}

    print("\n" + "=" * 78)
    print(f"终态 status={st.status}  用时={int(time.time() - t0)}s")
    print("=" * 78)
    print("【多图生图是否生效】")
    print(f"  送入垫图数= {len(uris)}")
    print(f"  草稿 image_uri_list   = {len(in_draft)}张")
    for i, u in enumerate(in_draft, 1):
        print(f"      [{i}] {u}")
    same_set = set(in_draft) == set(uris)
    print(f"  🔴 数量一致            = {'是' if len(in_draft) == len(uris) else '否'}")
    print(f"     uri 集合完全相同= {'是' if same_set else '否'}")
    print(f"  （注：core_param.large_image_info =输出尺寸 "
          f"{lii.get('width')}x{lii.get('height')} @{lii.get('resolution_type')}，"
          f"**不是垫图**）")

    print("\n【模型与张数】")
    print(f"  草稿 model      = {core.get('model')!r}  ← 须== 本次请求的 {key!r}")
    print(f"  草稿 gen_count  = {(ab.get('gen_option') or {}).get('gen_count')!r}")
    print(f"  四字段交叉核对  = item_list {len(items)} / large {large} / "
          f"total {node.get('total_image_count')} / finished {node.get('finished_image_count')}"
          f"  → {'一致' if len(counts) == 1 else f'不一致 {counts}'}")
    for i, im in enumerate(getattr(st, "images", []) or [], 1):
        print(f"    产物{i}. {im.width}x{im.height} {im.format or '?'} {im.url[:70]}")

    # 🔴 判定必须跟**本次实际请求的 key** 比，不能写死"Pro"——
    #  覆盖模式（PROBE_UPSTREAM）跑的是别的档位，写死会让真跑成功被判成失败。
    pads_ok = len(in_draft) == len(uris) and same_set
    model_ok = core.get("model") == key
    ok = pads_ok and model_ok
    print("\n判定：", "✅ **多图生图生效**（垫图全部入草稿 + 模型与请求一致）" if ok
          else f"❌ 垫图一致={pads_ok} 模型一致={model_ok}")
    Path("/tmp/pro_blend_result.json").write_text(json.dumps({
        "submit_id": sid, "status": st.status,
        "ark_name": ARK, "upstream_model": core.get("model"),
        "pads_sent": len(uris), "pads_in_draft": len(in_draft),
        "uri_set_identical": same_set,
        "gen_count": (ab.get("gen_option") or {}).get("gen_count"),
        "f_item_list": len(items), "f_large": large,
        "f_total": node.get("total_image_count"),
        "f_finished": node.get("finished_image_count"),
        "images": [{"w": im.width, "h": im.height, "url": im.url}
                   for im in (getattr(st, "images", []) or [])],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsubmit_id={sid}（供对账）")


if __name__ == "__main__":
    main()
