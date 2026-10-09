#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**prompt 前置决策预审**（Jev 决策模型）—— 违规 prompt 在受理时就地拦下，
**不落库、不提交上游、不花钱**。

## 为什么需要（2026-10-09）

即梦**没有文字预审接口**：文字违规只能在 `generate` 提交后被拒，且**照样计费**
（实测失败任务"该任务已被上游计费"）。图片有提交前的素材预审
（`execute_generate_audit`）兜底，prompt 没有 ⇒ 用外部决策模型补上这道闸。

用户口径三条（2026-10-09）：
1. 用决策模型判定 **是否放行提示词进入下一步骤**（生成提交）；
2. **请求缓存** —— 同一段 prompt 的判定结果缓存，不重复调用决策服务；
3. **请求失败默认进入下一步** —— 决策服务探不通/超时/解析失败 ⇒ **放行**
   + degradations 留痕（fail-open，与素材预审同一取向：预审是优化不是闸门，
   外部服务一抖就拒所有请求，比多花一次钱严重得多）。

## 实测校准（2026-10-09，bocha-jev-v1 @ jev.bocha.cn）

| prompt | noul（泛化问法） | noul（审核口径版 instructions） |
|---|---|---|
| 橘猫水彩（良性） | 0.009 | — |
| 泳装沙滩排球（良性） | 0.033 | 0.018 |
| **束缚挠痒全本**（即梦实测拒绝） | 0.616 ⚠️ 不稳 | **0.992** |
| 裸体女孩（明确违规） | 0.986 | — |

⇒ **题目质量是决定性的**：必须把即梦的真实审核口径（穿着衣物也算、
恋物式细节也算）写进 instructions，分数才拉开。阈值默认 **0.5**：
漏拦的代价是计费+风控升级，误拦的代价是调用方改写重试 ⇒ 保守方向。

## 与负缓存（negcache）的分工

· 本模块是**前置**闸（提交前、零成本拦），负缓存是**事后**闸（第一次真实失败后记 24h）；
· 本模块判定来自决策模型（可能有误判），负缓存来自**上游真实审核结果**（确定性）；
· 两者键都基于 prompt 归一（复用 `negcache._norm_prompt`），缓存里**不留 prompt 明文**。

## ⚠️ 数据出口声明

启用后，**prompt 原文会发送给决策服务**（`JEV_BASE_URL`，默认 bocha）。
这是该闸门工作的前提，部署前须知悉。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx

from .errors import ContentPolicyError
from .negcache import _norm_prompt

#: 审核口径版题目 —— **实测校准过**（见模块 docstring 的分数表）。
#: 要点：把"穿着衣物也拦、恋物式细节也拦"写明白，泛化问法对擦边样本
#: 只给 0.6（不够稳），这版给 0.99。
_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": (
        "你是图片生成平台的内容审核员。该提示词描述的画面是否属于平台禁止"
        "生成的性暗示/性癖内容？判定口径："
        "①色情裸露或未成年人性化，一律拦截；"
        "②捆绑束缚（binding/shibari/被绑住无法行动）、恋物式身体细节特写"
        "（脚部/腋下/足底压痕、挠痒挑逗）、强调身体被器具压出痕迹等性癖场景"
        "——即使角色身穿泳装等衣物、画面风格卡通可爱，也一律拦截；"
        "③正常衣着人物的一般场景（运动、风景、日常）放行。"
    ),
    "criteria": {"true": "属于禁止内容，应拦截", "false": "正常内容，应放行"},
}


def _fingerprint(prompt: str) -> str:
    """缓存键：prompt 归一后 sha256 截断 —— **不落明文**（同 negcache 取向）。"""
    return hashlib.sha256(_norm_prompt(prompt).encode("utf-8")).hexdigest()[:32]


#: 🔴 远端失败后的**故障冷却**：冷却期内直接跳过预审放行，不再白等超时。
#: 没有它，Jev 挂掉时每次受理都要干等 `JEV_TIMEOUT`（默认 8s）才 fail-open
#: —— 那会把"上游故障"放大成"受理全面变慢"。
_ERROR_COOLDOWN_S = 60.0


class PromptGuard:
    """线程安全的 prompt 决策预审闸（Jev systemone，noul 题 + 阈值）。"""

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout_s: float, threshold: float,
                 cache_ttl: float = 86400.0, cache_max: int = 4096,
                 enabled: bool = True) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout_s = float(timeout_s)
        self.threshold = float(threshold)
        self.cache_ttl = float(cache_ttl)
        self.cache_max = max(0, int(cache_max))
        self.enabled = bool(enabled)
        self._http: Any = None  # 测试注入点（直接替换该属性）；生产惰性建 httpx
        self._lock = threading.Lock()
        # key -> (到期时间戳, blocked, noul)
        self._cache: "OrderedDict[str, tuple[float, bool, float]]" = OrderedDict()
        # 观测计数
        self.calls = 0          # 实际发起的远端请求数
        self.blocks = 0         # 拦截次数（含缓存命中）
        self.errors = 0         # 远端失败次数（fail-open 放行）
        self.cache_hits = 0     # 缓存命中次数
        self.disabled_skips = 0  # 未启用/无 key 直接放行的次数
        self.error_skips = 0    # 故障冷却期内跳过的次数
        self._error_until = 0.0  # 故障冷却截止时刻（0 = 无故障）

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_settings(cls, settings: Any) -> "PromptGuard":
        """唯一的构造点 —— **要调闸门参数改这里**（同 `_build_gate` 取向）。"""
        return cls(
            base_url=settings.jev_base_url,
            api_key=settings.jev_api_key,
            model=settings.jev_model,
            timeout_s=settings.jev_timeout_s,
            threshold=settings.guard_block_threshold,
            cache_ttl=settings.guard_cache_ttl,
            cache_max=settings.guard_cache_max,
            enabled=settings.guard_enabled,
        )

    # ---------------------------------------------------------------- 查

    def check(self, prompt: str | None) -> str | None:
        """受理路径入口。

        · **放行** ⇒ 返回 `None`；
        · **远端失败**（fail-open）⇒ 返回**留痕文案**（调用方追加进
          degradations，让调用方知道这次没预审成）；
        · **判定违规** ⇒ 抛 `ContentPolicyError`（本地拦，不建任务、不花钱）。
        """
        p = (prompt or "").strip()
        if not self.enabled or not self.api_key or not p:
            with self._lock:
                self.disabled_skips += 1
            return None

        key = _fingerprint(p)
        cached = self._cache_get(key)
        if cached is not None:
            blocked, noul = cached
            with self._lock:
                self.cache_hits += 1
                if blocked:
                    self.blocks += 1
            if blocked:
                self._raise_blocked(noul, cached=True)
            return None

        now = time.time()
        if now < self._error_until:
            # 故障冷却期内：直接放行（不再白等一次超时），照常留痕。
            with self._lock:
                self.error_skips += 1
            return ("⚠️ prompt 决策预审处于故障冷却（此前远端失败）⇒ "
                    "本次跳过预审直接提交。")

        try:
            noul = self._remote_noul(p)
        except Exception as e:  # noqa: BLE001 —— fail-open：见模块 docstring 第 3 条
            with self._lock:
                self.errors += 1
            self._error_until = now + _ERROR_COOLDOWN_S
            return (f"⚠️ prompt 决策预审未完成（{type(e).__name__}）⇒ "
                    f"本次**跳过预审**直接提交；若 prompt 违规被上游拒绝，"
                    f"费用照常计入（事后由负缓存兜底）。")
        if noul is None:
            # 响应里解析不出判定 —— 视同预审失败，放行 + 留痕
            with self._lock:
                self.errors += 1
            return ("⚠️ prompt 决策预审无判定（响应缺 noul）⇒ 本次跳过直接提交。")

        blocked = noul >= self.threshold
        self._cache_put(key, blocked, noul)
        with self._lock:
            if blocked:
                self.blocks += 1
        if blocked:
            self._raise_blocked(noul, cached=False)
        return None

    def _raise_blocked(self, noul: float, *, cached: bool) -> None:
        via = "（缓存命中）" if cached else ""
        raise ContentPolicyError(
            f"**前置内容审查判定该 prompt 疑似违规**（决策模型 noul={noul:.2f}"
            f" ≥ 阈值 {self.threshold}{via}）⇒ 本次**不提交上游、不产生费用**。"
            f"请改写 prompt 后重试。⚠️ 这是本地决策模型的预判"
            f"（不是上游审核结论，也**不是永久封禁**）；"
            f"若上游真实审核通过了同类内容，负缓存/上游结果优先。",
            upstream="jimeng")

    # ---------------------------------------------------------------- 远端

    def _remote_noul(self, prompt: str) -> float | None:
        """唯一的网络出口。返回 noul；响应异常交给 `check` 统一 fail-open。"""
        client = self._http
        if client is None:
            client = self._ensure_http()
        with self._lock:
            self.calls += 1
        resp = client.post(
            f"{self.base_url}/systemone",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "state": prompt,
                  "questions": {"text_policy": _QUESTION}},
        )
        resp.raise_for_status()
        answers = (resp.json() or {}).get("answers") or {}
        verdict = answers.get("text_policy") or {}
        raw = verdict.get("noul")
        return None if raw is None else float(raw)

    def _ensure_http(self) -> Any:
        """惰性建 httpx 客户端。🔴 `trust_env=False`：绝不吃代理环境变量
        （HTTP_PROXY 陷阱 —— 对外 API 实测一律直连）。"""
        if self._http is None:
            self._http = httpx.Client(trust_env=False, timeout=self.timeout_s)
        return self._http

    # ---------------------------------------------------------------- 缓存

    def _cache_get(self, key: str) -> tuple[bool, float] | None:
        if not self.cache_max or self.cache_ttl <= 0:
            return None
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            expires, blocked, noul = hit
            if expires <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return blocked, noul

    def _cache_put(self, key: str, blocked: bool, noul: float) -> None:
        if not self.cache_max or self.cache_ttl <= 0:
            return
        with self._lock:
            self._cache[key] = (time.time() + self.cache_ttl, blocked, noul)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)   # LRU 淘汰

    # ---------------------------------------------------------------- 其它

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            live = sum(1 for exp, _, _ in self._cache.values() if exp > now)
        return {"enabled": self.enabled, "configured": bool(self.api_key),
                "model": self.model, "threshold": self.threshold,
                "calls": self.calls, "blocks": self.blocks,
                "errors": self.errors, "cache_hits": self.cache_hits,
                "entries": live, "disabled_skips": self.disabled_skips,
                "error_skips": self.error_skips}


__all__ = ["PromptGuard"]
