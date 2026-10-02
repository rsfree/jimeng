#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**内容审核负缓存** —— 被上游审核拒绝过的 (能力, 输入) 短期内不再提交。

## 为什么需要（2026-10-02）

上游对内容审核的拒绝是**确定性**的：同一段prompt / 同一张垫图
再提交一次，**必然再被拒**。而实测这类任务**照样计费**
（失败 message 里就写着"该任务已被上游计费"）。

⇒ 不缓存的代价是**调用方反复重试、反复扣钱**，而且每次要等
几十秒才拿到一个"必然的失败"。本缓存让第二次直接秒回，
**不建任务、不调上游、不花钱**。

## 缓存什么键

`(能力 api_id, 上游模型, prompt 归一, 输入图 URL 集合, 分辨率档)`：

· **能力/模型**要进键 —— 同一段prompt 在 Lite 能过、在 mj82 可能被拒
  （不同模型的审核策略不同）。
· **prompt 归一** = 去首尾空白 + 折叠连续空白 + 转小写。
  🔴 **不做语义归一**（不同措辞可能一个过一个不过）——
  宁可漏缓存（多花一次钱），不可错杀（把本该过的请求挡掉）。
· **输入图**进键 —— 换图就该重试（图才是被拒的那一方时）。
· **分辨率档**进键 —— 不同档位的审核策略可能不同。

## TTL 与容量

· 默认 **24 小时**（`NEG_CACHE_TTL`，2026-10-03 用户口径从6h 调长）：
  审核策略很少变，而重复提交同一违规素材的**代价是每次都计费**
  ⇒ 宁可拦久一点也别让调用方反复踩。设 0 即**关闭**。
· 有界 LRU（默认 2048 条，`NEG_CACHE_MAX`）——
  无界的缓存是内存泄漏。
· 🔴 **进程内、不跨实例**：多副本部署时各持一份。
  这是**有意的取舍**（不引外部依赖）；代价是**命中率打折**，
  但不会错杀 ⇒ 安全方向。

## 命中后的行为

**不受理、不建任务**，直接抛 `ContentPolicyError`（400，不可重试）
并带上"因为 N 分钟前同样的输入被拒过"的留痕。
⚠️ 错误文案必须说清是**缓存命中**、**首次仍要真跑** ——
否则调用方会以为"这个 prompt 永久被禁了"。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from .errors import ContentPolicyError

#: 连续空白（含全角空格）折叠。
_WS = re.compile(r"[\s　]+")


def _norm_prompt(prompt: str | None) -> str:
    """prompt 归一 —— **只做保守归一**（见模块docstring的"不做语义归一"）。"""
    return _WS.sub(" ", (prompt or "").strip()).lower()


def cache_key(*, cap_id: str, upstream_model: str | None = None,
              prompt: str | None = None, images: Any = None,
              resolution_tier: str | None = None,
              **_ignored: Any) -> str:
    """算缓存键。**故意用哈希**而不是拼明文：

    ① prompt 可能很长（含中文/换行）⇒ 明文键会撑爆内存；
    ② 明文存了等于在内存里留一份 prompt 副本，不该留。
    哈希用 sha256（截断到 16 字节足够抗碰撞，且快）。
    """
    #: ⚠️ `_ignored` 是**故意**的：调用方（`record_failure`）会把
    #: `reason=` 一起传进来，而它**不属于键**（理由会变，键不能变）。
    #: 吞掉多余 kw 比让每个调用点自己剥掉更稳，也避免"加个字段就炸"。
    imgs = sorted(str(u) for u in (images or []) if u)
    raw = "\x1f".join([
        cap_id, upstream_model or "", _norm_prompt(prompt),
        "|".join(imgs), resolution_tier or "",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class NegativeCache:
    """线程安全的 TTL + LRU 负缓存。"""

    def __init__(self, *, ttl: float = 86400.0, max_entries: int = 2048) -> None:
        self._ttl = float(ttl)
        self._max = max(0, int(max_entries))
        self._lock = threading.Lock()
        # key -> (到期时间戳, 失败原因摘要)
        self._data: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.records = 0

    # ------------------------------------------------------------------ 读

    def lookup(self, key: str) -> str | None:
        """命中则返回**留原因**，未命中/已过期/未启用 ⇒ `None`。"""
        if not self._max or self._ttl <= 0 or not key:
            return None
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                self.misses += 1
                return None
            expires, reason = hit
            if expires <= now:
                # 过期即删（顺手清LRU 尾巴，避免过期条目占位）
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return reason

    def would_block(self, **kw: Any) -> str | None:
        """便捷入口：算键 + 查。**受理路径零成本先查这个**。"""
        return self.lookup(cache_key(**kw))

    # ------------------------------------------------------------------ 写

    def record(self, key: str, reason: str) -> None:
        """记住一次审核拒绝。**只在该 key 尚未被记时写**（保留最早那次的原因）。"""
        if not self._max or self._ttl <= 0 or not key:
            return
        now = time.time()
        with self._lock:
            existing = self._data.get(key)
            if existing and existing[0] > now:
                return                      # 已有更早的记录，不覆盖
            self._data[key] = (now + self._ttl, reason[:200])
            self._data.move_to_end(key)
            self.records += 1
            while len(self._data) > self._max:
                self._data.popitem(last=False)   # LRU 淘汰

    def record_failure(self, **kw: Any) -> None:
        self.record(cache_key(**kw), kw.get("reason") or "内容审核未通过")

    # ------------------------------------------------------------------ 其它

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            live = sum(1 for exp, _ in self._data.values() if exp > now)
        return {"entries": live, "ttl_s": self._ttl, "max": self._max,
                "hits": self.hits, "misses": self.misses, "records": self.records}

    def raise_if_blocked(self, **kw: Any) -> None:
        """受理路径用：命中即抛 `ContentPolicyError`（**不建任务、不花钱**）。"""
        reason = self.would_block(**kw)
        if not reason:
            return
        minutes = max(1, int(self._ttl // 60))
        raise ContentPolicyError(
            f"**同样的输入在 {minutes} 分钟前被上游内容审核拒绝过**"
            f"（{reason}）⇒ 本次**不再提交上游**。"
            f"原样重试必然再被拒，且每次都会计费 —— 请**改prompt 或换输入图**。"
            f"⚠️ 这是负缓存命中（TTL {minutes} 分钟），不是永久封禁；"
            f"换个说法或换素材即可正常受理。",
            upstream="jimeng")


__all__ = ["NegativeCache", "cache_key", "_norm_prompt"]
