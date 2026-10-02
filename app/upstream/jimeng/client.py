#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦（jimeng.jianying.com）图片生成客户端 —— **建任务 + 取任务**，纯 HTTP。

上游契约见 `docs/UPSTREAM.md`。本模块只做「翻译 + 轮询」。

## 这条链路最重要的四个事实（都是实测，不是推测）

1. **最小凭据 = 一个 cookie：`sessionid`。**
   其余 20 多个 cookie、`sign` / `sign-ver` / `device-time` / `tdid` 头、
   query 里的 `msToken` / `a_bogus` **全部不需要**。判据（负对照）：

   | 格 | cookie | sign | 读接口 | 写接口 |
   |---|---|---|---|---|
   | W1/A | ✅ | ✅ | `ret=0` | `ret=1002`（业务错误，鉴权已过） |
   | W3 | ✅ | ❌ | `ret=0` | `ret=1002`（**与 W1 同码**） |
   | C/E/W2 | ❌ | — | `ret=1015 login error` | `ret=1015 login error` |

   W1 == W3 ⇒ **`sign` 不参与鉴权**（读写一致）；只有 cookie 是硬前提。

2. **建任务是异步的**：`POST /mweb/v1/aigc_draft/generate` 只回执，
   真正的图要轮询 `POST /mweb/v1/get_history_by_ids`（body 带 `submit_ids`）。
   `submit_id` **由客户端自己生成**（uuid4）⇒ 即使建任务的回执体解析失败，
   轮询依然可用 —— 这是刻意设计，不依赖回执格式。

3. **`draft_content` 是「JSON 字符串」而不是 JSON 对象**（双重编码）。
   写成对象会被上游按 `1002 common error` 拒掉。

4. 🔴 **"被接受" ≠ "能跑通"**：建任务返回 `ret=0` 只说明请求**被受理**，
   任务仍可能终态 `status=30 generate_failed`，而且**照样计费**。
   ⇒ 判成败**必须看 `task.status`**，只看 `ret` 会把失败报成成功。
   （2026-09-19 细节修复工具就是这么白花了 2×16 积分。）

## 计费

建任务是**计费动作**（响应里有 `forecast_generate_cost` 给出预估积分）。
本客户端因此把「建任务」与「轮询」拆成两个公开方法，并在 `generate()` 上
保留显式的 `dry_run` —— `dry_run=True` 时只构造请求体、**不发任何上游生成**。
"""
from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

# 🔴 上游 HTTP 埋点用的单例。**这个 import 曾经漏掉过** —— `_report()` 里引用 `OBS`，
# 而它的 `except Exception: pass` 把 `NameError` 一起吞了 ⇒ 埋点从来没生效、
# 且**毫无报错**。现已去掉那层 try/except，让接线错误当场炸出来。
# 静态门禁：`ruff check`（F821）+ `tests/test_wiring.py` 里有专门用例守着。
from ...observability import OBS

from .sign import (APPID, APP_SDK_VERSION, APPVR, DA_VERSION, PF,
                   WEB_VERSION, sign_headers)

BASE = "https://jimeng.jianying.com"
PATH_SUBMIT = "/mweb/v1/aigc_draft/generate"
PATH_HISTORY = "/mweb/v1/get_history_by_ids"
PATH_HISTORY_LIST = "/mweb/v1/get_history"
PATH_IMAGE_BY_URI = "/mweb/v1/get_image_by_uri"
PATH_UPLOAD_TOKEN = "/mweb/v1/get_upload_token"
#: 服务端下发的**模型能力表**（张数选项 / 各比例精确像素 / 支持的能力）。
#: 只读、零成本 —— 见 `capabilities.py` 与 `docs/UPSTREAM.md` §11。
PATH_COMMON_CONFIG = "/mweb/v1/get_common_config"

#: 默认模型：抓包里用的就是它（站点自报 Seedream 5.0 Lite）。
DEFAULT_MODEL = "high_aes_general_v50"
#: 抓包实测唯一跑通的尺寸（2048×2048，`resolution_type=2k`）。
DEFAULT_SIZE = "2048x2048"

# ---------------------------------------------------------------------------
# 站点枚举（全部来自 bundle 静态取证，非推测）
# ---------------------------------------------------------------------------

#: 任务状态。原文：`7960.13db92ac44.js` / `5794.fa66eab7e8.js`，两处一致。
TASK_STATUS = {
    -1: "unknown",
    0: "init",                # 已受理，尚未提交生成
    10: "pre_check_reject",   # 送审前就被拒（文本/图片风控）
    20: "submitted",          # 已提交生成，等结果
    30: "generate_failed",    # 生成失败
    40: "post_check_reject",  # 生成完送审被拒
    42: "thinking",           # 生成中
    45: "partial_success",    # 部分成功
    50: "success",
    100: "deleted",
}
TASK_STATUS_TERMINAL = frozenset({10, 30, 40, 50, 100})
TASK_STATUS_OK = frozenset({50})

#: 比例枚举。原文：`DAImageRatioTypeUtils`（4 个 bundle 一致）。
IMAGE_RATIOS: dict[int, tuple[str, float]] = {
    1: ("1:1", 1.0),
    2: ("3:4", 0.75),
    3: ("16:9", 16 / 9),
    4: ("4:3", 4 / 3),
    5: ("9:16", 9 / 16),
    6: ("2:3", 2 / 3),
    7: ("3:2", 1.5),
    8: ("21:9", 21 / 9),
}

#: 上游业务错误码。原文：`2748.64c9a93285.js` 的 ErrNo 枚举。
ERR_NO: dict[int, str] = {
    0: "success",
    1: "rate_limit",
    1001: "invalid_params",
    1002: "common_error",
    1006: "credit_not_enough",
    1010: "concurrency_limit",
    1014: "sign_error",
    1015: "login_error",
    1018: "punish_limit_aigenerate",
    1019: "risk_not_pass",
    1021: "commercial_shark_block",
    1057: "rate_limit",
    1063: "text_audit_not_pass",
    1159: "copyright",
    1161: "mix_cn_en",
    1162: "unsupported_lang",
    2002: "generate_error",
    2003: "pre_img_risk",
    2004: "post_img_risk",
    2005: "pre_text_risk",
    2014: "access_limit",
    2020: "rate_limit",
    2035: "security_not_pass",
    2038: "text_security_block",
    2039: "image_security_block",
    2041: "image_security_block",
    2042: "video_security_block",
    2043: "security_not_retry",
    2048: "image_copyright_block",
    2050: "text_copyright_block",
    3021: "unsupported_by_beta",
    4001: "external_no_credits",
    4003: "no_permission",
    4010: "compliance_confirmation_required",
    10020: "rate_limit_non_commercial_region",
    121101: "reached_daily_generation_limit",
}

#: 五类错误的码表 —— 分类决定重试语义，所以刻意保守：
#: 能确定不可重试的一律不可重试（重试一个必然失败的付费请求 = 白花钱）。
CODES_RATE_LIMIT = frozenset({1, 1010, 1057, 2014, 2020, 10020})  # 可退避重试
CODES_QUOTA = frozenset({1006, 4001, 121101})                     # 重试无效（按天/按余额）
CODES_RISK = frozenset({1018, 1019, 1021, 2035, 2038, 2039,       # 重试会加剧，必须退避
                        2041, 2042, 2043})
CODES_CONTENT = frozenset({1063, 1159, 2003, 2004, 2005,          # 内容审核/版权
                           2048, 2050})

#: 🔴 **终态**的内容/安全拦截码 —— 与提交期的 `CODES_RISK` 不是一回事：
#: 那些是"退避再试"，这些是"这次结果被平台拒了，**换 prompt / 换图才有用**"。
#: 实测 `status=30` + `fail_code=2038`（InputTextRisk，
#: `fail_starling_message` = "你输入的文字不符合平台规则，请修改后重试"）就属此类 ——
#: 只看 status 会把它归成"上游故障"，调用方会去重试一个必然再被拒的请求。
CODES_SECURITY = frozenset({1063, 1159, 2003, 2004, 2005, 2038, 2039, 2041,
                            2042, 2043, 2048, 2050})
CODES_PARAM = frozenset({1001, 1002, 1161, 1162, 1190, 1189, 3021, 4003,
                         4010, 2203, 2204})


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class JimengError(RuntimeError):
    """上游侧错误基类。`retryable` 决定调用方该不该重试。"""

    def __init__(self, message: str, *, code: int | None = None,
                 retryable: bool = False, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.extra = extra


class JimengAuthError(JimengError):
    """`1015` —— 凭据缺失/失效（cookie 里没有有效的 `sessionid`）。"""


class JimengRateLimitError(JimengError):
    """限流/并发上限（`1` / `1010` / `1057` / `2020` / `10020`）。**可退避重试**。"""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class JimengQuotaError(JimengError):
    """积分不足 / 日额度用尽（`1006` / `4001` / `121101`）。**重试无效**。"""


class JimengRiskError(JimengError):
    """风控命中（`1018` / `1019` / `1021` / `2035` / …）。重试会加剧，必须退避。"""


class JimengContentError(JimengError):
    """内容审核 / 版权拦截（`1063` / `1159` / `2003`~`2005` / `2048` / `2050`）。"""


class JimengParamError(JimengError):
    """请求体或参数被上游拒绝（`1001` / `1002` / `3021` / `4003` / `4010` …）。"""


class JimengTimeout(JimengError):
    """轮询超时（任务可能仍在跑，`submit_id` 仍可继续查）。"""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


#: 码 → 异常类。**查表得出**，不在 `_raise` 里写一长串 if —— 那样两份实现必然漂移。
_ERROR_CLASSES: tuple[tuple[frozenset[int], type[JimengError]], ...] = (
    (CODES_RATE_LIMIT, JimengRateLimitError),
    (CODES_QUOTA, JimengQuotaError),
    (CODES_RISK, JimengRiskError),
    (CODES_CONTENT, JimengContentError),
    (CODES_PARAM, JimengParamError),
)


#: 上游拒绝码 → (人话, **是否对"请求形态/草稿结构"有判别力**)。
#:
#: 🔴 这条分类是用一次**假结论**换来的（2026-09-18）。当时把所有拒绝都当成
#: "形态不对"，于是把 `1006 credit_not_enough`（积分预扣/权益不足）读成了
#: 「图生图形态仍未解开」—— 而同一时刻文生图正常出图，证明那是额度问题。
#: **只有 `1001/1002`（参数/业务体校验）才说明"服务端读不懂这个草稿"。**
REJECT_VERDICTS: dict[str, tuple[str, bool]] = {
    "shape": ("形态被拒（对草稿结构有判别力）", True),
    "quota": ("积分/权益不足（与形态无关，INCONCLUSIVE）", False),
    "auth": ("凭据失效（INCONCLUSIVE）", False),
    "risk": ("风控（INCONCLUSIVE）", False),
    "unknown": ("其它错误码（INCONCLUSIVE）", False),
}


def classify_reject(code: int | None) -> tuple[str, bool]:
    """把一个上游拒绝码分成「有判别力 / INCONCLUSIVE」。

    这是**判据统一入口**：上游拒绝码必须先分类再下结论，否则会把
    "额度不足"读成"请求形态不对"，产出假结论。
    """
    if code in (1001, 1002, 1161, 1162, 3021):
        return REJECT_VERDICTS["shape"]
    if code in (1006, 4001, 121101):
        return REJECT_VERDICTS["quota"]
    if code == 1015:
        return REJECT_VERDICTS["auth"]
    if code in (1018, 1019, 1021, 2035):
        return REJECT_VERDICTS["risk"]
    return REJECT_VERDICTS["unknown"]


# ---------------------------------------------------------------------------
# 尺寸 → 比例
# ---------------------------------------------------------------------------

def parse_size(size: str) -> tuple[int, int]:
    """`"2048x2048"` / `"2048*2048"` / `"2048×2048"` → (w, h)。"""
    raw = (size or "").strip().lower()
    for sep in ("x", "*", "×"):
        if sep in raw:
            a, _, b = raw.partition(sep)
            try:
                return int(a.strip()), int(b.strip())
            except ValueError:
                break
    raise JimengParamError(
        f"无法解析 size={size!r}，期望形如 '2048x2048'（宽x高，像素）", code=1001)


def ratio_for_size(width: int, height: int) -> int:
    """按站点自己的算法（`valueToType`：取**数值比例最近**的枚举）定比例。

    刻意不硬编「某个比例 = 某个像素」的表 —— 那张表是服务端下发的
    （`resolution_map`，见 `capabilities.py`），本地编一份就是假知识。
    这里只做站点做过的事：把调用方给的像素归到最近枚举，像素本身原样透传
    （上游不接受时会给明确的业务错误码）。
    """
    if width <= 0 or height <= 0:
        raise JimengParamError(f"size 非法：{width}x{height}", code=1001)
    target = width / height
    return min(IMAGE_RATIOS, key=lambda k: abs(target - IMAGE_RATIOS[k][1]))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 草稿（draft_content）构造
# ---------------------------------------------------------------------------

#: 服务端声明的「一次能出几张」合法取值 —— **冻结快照，运行期优先用服务端实时值**。
#:
#: 来源 = `POST /mweb/v1/get_common_config` 的 `model_list[].generate_count_options`。
#: ⚠️ **这是服务端数据的一次快照，不是常量**。运行期由 `capabilities.py` 用零成本的
#: 只读接口刷新；本表只在**探测失败**时兜底，并会在响应里如实标注降级。
#: 2026-09-18 的教训：先按"用户经验 1–4"把上界写死成 4，实际
#: `high_aes_general_v50`（默认模型）服务端声明的是 **1..8** ——
#: 教训是**别用经验值替代可读的服务端数据**。
COUNT_OPTIONS_BY_MODEL: dict[str, tuple[int, ...]] = {
    # 🔴 2026-10-02 mj82（图片美学模型 V8.2）：服务端 `generate_count_options`
    # 实读就是 **[4]**（`default_generate_count` 也是 4）。
    # ⚠️ 这里只登记**服务端声明**，**不是**实测能控的取值 —— 实测结论见下方
    # `MJ82_COUNT_EFFECTIVE`（`gen_count=2/3` 会被上游**静默改写**成 4）。
    "jm_image_model_yc_mj82": (4,),
    "high_aes_general_v50_flash": (1, 2, 3, 4),
    "high_aes_general_v50": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v50p_large": (1, 2, 3, 4),
    "high_aes_general_v43": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v42": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v41": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v40l": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v40": (1, 2, 3, 4, 5, 6, 7, 8),
    "high_aes_general_v30l_art_fangzhou:general_v3.0_18b": (1, 2, 3, 4),
    "high_aes_general_v30l:general_v3.0_18b": (1, 2, 3, 4),
}
#: 未登记模型的兜底：取各模型选项的**下确界**（最严的那个），宁少勿多。
DEFAULT_COUNT_OPTIONS: tuple[int, ...] = (1, 2, 3, 4)

#: 🔴🔴 mj82 **实测**张数规则（2026-10-02，**七发真跑** + 原始报文四字段交叉核对）：
#:
#: ⚠️ 先说**判据**，因为这里有两个都叫"张数"的字段，**只有草稿里那个算数**：
#:
#: | 字段 | 语义 | 能否用来判出图数 |
#: |---|---|---|
#: | `abilities.gen_option.gen_count`（草稿） | **实际执行的张数** | ✅ 唯一可信 |
#: | `metrics_extra.generateCount` | 提交时的**请求/埋点**计数 | ❌ 恒为 1，不反映实际 |
#!
#: 🔴 2026-10-02 实测七发（`count`是我们要的，回执草稿值 = 上游实际采用值）：
#:
#: | 我们提交 | 回执草稿 `gen_count` | 实际出图（四字段一致） |
#: |---|---|---|
#: | 1 | 1 | **1** |
#: | 1 | 1 | **1** |
#: | 2 | **4** | 4（被抬） |
#: | 3 | **4** | 4（被抬） |
#: | 4 | 4 | 4 |
#: | 1 | **4** | 4（被抬） |
#: | 1 | **4** | 4（被抬） |
#:
#: ⇒ **同样传 1，有时出 1、有时出 4** —— 前两发是我**误把 `(1,2,3,4)`
#: 喂给 `count_options`** 导致 `resolve_count` 原样透传 1（上游照办），
#: 后两发改成 `(1,)` 后仍被抬成 4。
#: 🔴 结论：**mj82 的张数实际上不可控**，`gen_count` 会被上游按自己的
#: `generate_count_options=[4]` 归一；只有在**绕过吸附**且恰好落 1 时
#: 才可能出 1 张，**这条路径不该依赖**（不稳定，且计费随之变化）。
#: ⇒ 按**恒 4 张**设计与告知调用方（`service.create` 的
#: `len(declared)==1` 分支会响亮留痕）。
#: ⇒ 用户 2026-10-02 的 UI 抓包正是这一机制的**直接证据**：
#: 草稿 `gen_count=4` 而 `metrics_extra.generateCount=1`。
#:
#: 🔴 判"实际出图几张"必须**四个独立字段一致**：`item_list` 长度 /
#: 各 item 的 `large_images` 之和 / `total_image_count` /
#: `finished_image_count`。（`parse_task` 每个 item 只取`large_images[0]`，
#: 所以**不会**多报；但交叉核对能排除"某一字段单独异常"。）
MJ82_COUNT_EFFECTIVE: tuple[int, ...] = (1, 4)


def count_options(model: str) -> tuple[int, ...]:
    return COUNT_OPTIONS_BY_MODEL.get(model, DEFAULT_COUNT_OPTIONS)


def resolve_count(model: str, count: int,
                  options: tuple[int, ...] | None = None) -> tuple[int, str | None]:
    """把请求张数**吸附到该模型声明的合法取值**上，返回 (生效值, 告警或 None)。

    吸附而不是报错：网关场景下「要 5 张、这个模型只能出 4 张」应当降级并留痕，
    而不是整单失败。取**不超过请求值**的最大合法值；请求值低于下界则抬到下界。

    ⚠️ **吸附会改变花费**（张数直接乘积分）⇒ 调用方必须能看到这条告警，
    不能静默降级。调用方读到的是响应里的 `degradations`。
    """
    opts = options or count_options(model)
    if count in opts:
        return count, None
    smaller = [x for x in opts if x <= count]
    fixed = max(smaller) if smaller else min(opts)
    return fixed, (f"模型 {model} 的张数选项为 {list(opts)}，"
                   f"请求 n={count} 已吸附为 {fixed}")


def build_draft(*, prompt: str, model: str = DEFAULT_MODEL, count: int = 1,
                width: int = 2048, height: int = 2048,
                negative_prompt: str = "", seed: int | None = None,
                sample_strength: float = 0.5,
                resolution_type: str = "2k") -> str:
    """构造**文生图**的 `draft_content` —— 返回的是 **JSON 字符串**（上游要双重编码）。

    结构完全照抄抓包，只把可变字段参数化。
    所有 `id` 都是现场生成的 uuid4 —— 抓包里它们也是随机的，不是常量。
    """
    if not prompt or not prompt.strip():
        raise JimengParamError("prompt 不能为空（即梦文生图必填）", code=1001)
    count, _warn = resolve_count(model, count)

    comp_id = _uid()
    draft = {
        "type": "draft",
        "id": _uid(),
        "min_version": "3.0.2",
        "min_features": [],
        "is_from_tsn": True,
        "version": DA_VERSION,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component",
            "id": comp_id,
            "min_version": "3.0.2",
            "aigc_mode": "workbench",
            "metadata": {
                "type": "", "id": _uid(), "created_platform": 3,
                "created_platform_version": "",
                "created_time_in_ms": str(_now_ms()), "created_did": "",
            },
            "generate_type": "generate",
            "abilities": {
                "type": "", "id": _uid(),
                "generate": {
                    "type": "", "id": _uid(),
                    "core_param": {
                        "type": "", "id": _uid(),
                        "model": model,
                        "prompt": prompt,
                        "negative_prompt": negative_prompt,
                        "seed": seed if seed is not None
                                else random.randint(1, 2 ** 32 - 1),
                        "sample_strength": sample_strength,
                        "image_ratio": ratio_for_size(width, height),
                        "large_image_info": {
                            "type": "", "id": _uid(),
                            "height": height, "width": width,
                            "resolution_type": resolution_type,
                        },
                        "intelligent_ratio": False,
                        "generate_type": 0,
                    },
                },
                "gen_option": {
                    "type": "", "id": _uid(),
                    "gen_count": count, "generate_all": False,
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass
class GeneratedImage:
    url: str
    width: int | None = None
    height: int | None = None
    format: str | None = None
    item_id: str | None = None
    #: 视频 item 的 `vid`（补帧 v2v_opt 与 video_ref_params 都要引用它）
    vid: str | None = None
    #: 视频时长（毫秒，来自 `video.duration_ms`）
    duration_ms: int | None = None
    note: str | None = None


@dataclass
class TaskState:
    submit_id: str
    status: int | None = None          # 站点 task.status 枚举值
    status_name: str = "unknown"
    finished: bool = False
    failed: bool = False
    failed_reason: str = ""
    #: 上游 `fail_code` —— **分类真因**。别看 `status` 就下结论：
    #: 实测 `status=30`（通用"生成失败"）配上 `fail_code=2038`（InputTextRisk）
    #: 才是真因；只看 status 会把它误归成"上游故障"。
    fail_code: int | None = None
    images: list[GeneratedImage] = field(default_factory=list)
    total: int | None = None
    finished_count: int | None = None
    history_record_id: str | None = None
    submit_id_echo: str | None = None
    model: str | None = None
    cost: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.finished and not self.failed


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def parse_task(submit_id: str, node: dict) -> TaskState:
    """把一个 `data[submit_id]` 节点翻译成 `TaskState`。

    字段名按抓包实测；同时保留若干别名兜底（上游改字段名时不至于静默空手而归）——
    但**兜底失败一律当显式失败**，不把「没解析到图」当成「还没生成好」。
    """
    st = TaskState(submit_id=submit_id)
    st.raw = dict(node)
    task = node.get("task") or {}
    status = _pick(task, "status", "task_status")
    if status is None:
        status = _pick(node, "status")
    st.status = int(status) if status is not None and str(status).lstrip("-").isdigit() \
        else None
    st.status_name = (TASK_STATUS.get(st.status, f"unknown({st.status})")
                      if st.status is not None else "unknown")
    st.finished = st.status in TASK_STATUS_TERMINAL if st.status is not None else False
    st.failed = st.finished and st.status not in TASK_STATUS_OK
    if st.failed:
        key = node.get("fail_starling_key") or ""
        msg = node.get("fail_starling_message") or ""
        st.failed_reason = " ".join(x for x in (key, msg) if x) or st.status_name
        _fc = node.get("fail_code")
        st.fail_code = _fc if isinstance(_fc, int) else None

    st.history_record_id = node.get("history_record_id")
    st.submit_id_echo = _pick(task, "submit_id") or node.get("submit_id")
    st.total = _pick(node, "total_image_count")
    st.finished_count = _pick(node, "finished_image_count")
    mi = node.get("model_info") or {}
    st.model = _pick(mi, "model_req_key", "model_name")
    st.cost = _pick(node, "forecast_generate_cost")

    for it in (node.get("item_list") or []):
        # ---- 视频产物（t2v/vfi）：item 里带的是 `video` 而不是 `image` ----
        # ✅ 2026-09-20 真实回包实抓验证（t2v status=50）：
        #   `video.video_id` 是 vid；**下载 URL 在 `video.transcoded_video`
        #   里按分辨率分档**（如 "360p": {vid, fps, width, height,
        #   video_url}）—— 取 width 最大的那档。
        vid_node = it.get("video")
        if isinstance(vid_node, dict):
            vid = vid_node.get("video_id")
            trans = vid_node.get("transcoded_video") or {}
            best = None
            for v in trans.values():
                if isinstance(v, dict) and v.get("video_url"):
                    if best is None or (v.get("width") or 0) > \
                            (best.get("width") or 0):
                        best = v
            url = (best or {}).get("video_url")
            if url:
                st.images.append(GeneratedImage(
                    url=url, width=best.get("width"),
                    height=best.get("height"),
                    format="mp4",
                    item_id=(it.get("common_attr") or {}).get("id"),
                    vid=vid,
                    duration_ms=vid_node.get("duration_ms"),
                    note=None,
                ))
                continue
        img = it.get("image") or {}
        large = img.get("large_images") or []
        url = None
        w = h = fmt = None
        if large and isinstance(large[0], dict):
            url = large[0].get("image_url")
            w, h = large[0].get("width"), large[0].get("height")
            fmt = large[0].get("format")
        if not url:
            # 兜底：item_urls 一般只放水印变体，仍值得一试（并在产物上标注）
            urls = ((it.get("common_attr") or {}).get("item_urls") or [])
            url = next((u for u in urls if u), None)
        if url:
            st.images.append(GeneratedImage(
                url=url, width=w, height=h,
                format=fmt or img.get("format"),
                item_id=(it.get("common_attr") or {}).get("id"),
                note=None if large else "URL 取自 item_urls 兜底（可能带水印）",
            ))
    return st


# ---------------------------------------------------------------------------
# 后编辑（post-edit）工具族：智能超清 / 超清 / 扩图 / 细节修复
# ---------------------------------------------------------------------------
# 取证：2026-09-18 四组真实抓包（每组一条 generate + 一条 get_history）。
#
# 🔴 **它们共享同一个形态**：往 draft 的 `component_list` 末尾**追加一个**
# `image_base_component`，用 `parent_id` 指向上一个组件、用 `generate_type` 字符串
# 选中工具、并把该工具的参数放在 `abilities.<同名 key>` 里。
# 也就是说「新增一个编辑工具」在这条链路上**不是新增端点**，而是新增一个组件描述。
#
# ⚠️ **输入图是即梦自己存储里的资产**（`tos-cn-i-<bucket>/<hash>`），
# 不是外链也不是字节流 ⇒ 要用**本地图片**必须先走上传换 `image_uri`（见 upload.py）。
POST_EDIT_TOOLS: dict[str, dict[str, Any]] = {
    "pro_hd": {
        "label": "智能超清",
        "generate_type": "pro_hd", "ability": "pro_hd",
        "component_gen_type": 35,      # 抓包里有时带、有时不带
        "postedit_generate_type": 35,
        # 该工具特有字段（照抄抓包）。
        # 注意：**不放 `resolution_type`** —— 它是 `build_post_edit_draft` 的入参、
        # 由参数动态注入。放在表里会与注入逻辑重复（服务侧表就没有它）。
        "fields": {"hd_scene_type": "auto",
                   "original_image_strength": 0.65, "detail_strength": 0.65},
        "scene": "ImageProHD",
    },
    "normal_hd": {
        "label": "超清",
        "generate_type": "normal_hd", "ability": "normal_hd",
        "component_gen_type": 13,
        "postedit_generate_type": 13,
        "fields": {},
        "scene": None,
    },
    "outpaint": {
        "label": "扩图（OutPaint）",
        "generate_type": "painting", "ability": "painting",
        "component_gen_type": 8,
        "postedit_generate_type": 8,
        "fields": {},                  # 参数走 up_scale，单独传
        "scene": None,
    },
    "detail": {
        "label": "细节修复",
        "generate_type": "super_resolution", "ability": "super_resolution",
        "component_gen_type": None,    # 抓包里**不带** gen_type
        "postedit_generate_type": 2,
        "fields": {},
        # 🔴 抓包里 `super_resolution` 的 ability **带 `core_param`**（其余三个都没有）。
        # 首轮实测漏了它 ⇒ 任务被**接受**了但 `status=30 generate_failed`
        # （且照样计费）—— 这是"被接受≠能跑通"的又一例。
        "core_param": {"generate_type": 0},
        "scene": None,
        #: ✅ **9-20 路 A 已验证**：单组件 + item_id/origin_history_id
        #: （无 origin_image）真跑成功 —— 死因就是 origin_image，父链非必需
        #: （见 UPSTREAM.md §9.2）。
        #: 🔴 **9-20 晚复测（同日第 3、4 样本）**：upload（origin_image 带 tos
        #: uri）形态又败两次 —— 纯单组件（8s 即败）与 + 自造父组件重放
        #: （仿 vfi 产物形态）均 generate_failed，且**零扣费**（推翻"失败且
        #: 计费"旧记录）。本地图不支持是稳定结论。
        #: ✅ 本地图的**可用正解**（9-20 晚真跑全通，两步均免费）：
        #:    本地图 → i2i blend（Lite 免费）→ 生成产物 → 引用形态 detail
        #:    （1920² 出片；外部图同理：上传→blend→修复）。
    },
}

#: `postedit_param.generate_type` 的整型枚举（原文 `5794.fa66eab7e8.js`）。
POSTEDIT_GENERATE_TYPE: dict[int, str] = {
    1: "Text2Image", 2: "SuperResolution", 7: "InPaint", 8: "OutPaint",
    13: "SuperDefinition", 27: "ByteEditPainting", 28: "EttaPainting",
    35: "ProHD",
}


def build_post_edit_draft(*, tool: str, image_uri: str = "", image_url: str = "",
                          item_id: int | None = None,
                          origin_history_id: int | None = None,
                          source_from: str = "upload",
                          width: int = 2048, height: int = 2048,
                          resolution_type: str = "2k",
                          up_scale: dict[str, Any] | None = None,
                          parent_id: str | None = None,
                          count: int = 1) -> str:
    """构造**后编辑**任务的 `draft_content`（返回 JSON 字符串）。

    `source_from`：`upload` = 引用即梦存储里的 `image_uri`（抓包实测形态）；
    `link` = 尝试直接给外链（枚举里确实有这个取值，但**能否被接受未验证**）。

    `item_id` / `origin_history_id` 只在"编辑自己账号里已有的作品"时才需要。
    """
    spec = POST_EDIT_TOOLS.get(tool)
    if spec is None:
        raise JimengParamError(
            f"未知的即梦后编辑工具 {tool!r}；可用：{sorted(POST_EDIT_TOOLS)}", code=1001)
    # 🔴 输入图三种承载方式（2026-09-20 细节修复抓包补全第三种）：
    #   ① image_uri（tos 资产，已验证）② image_url（外链，未验证）
    #   ③ item_id + origin_history_id（**不带 origin_image**，引用账号已有作品 ——
    #      细节修复真实 UI 唯一可见的形态，见 UPSTREAM.md §9.1）
    if not image_uri and not image_url:
        if item_id is None or origin_history_id is None:
            raise JimengParamError(
                "后编辑需要输入图：image_uri / image_url，"
                "或 item_id+origin_history_id（引用账号已有作品）", code=1001)
    elif source_from == "upload" and not image_uri:
        raise JimengParamError("source_from=upload 时必须给 image_uri", code=1001)

    comp_id = _uid()
    node = _uid()

    if source_from == "upload":
        img = {"type": "image", "id": _uid(), "source_from": "upload",
               "platform_type": 1, "name": "", "image_uri": image_uri,
               "width": 0, "height": 0, "format": "", "title": "", "uri": image_uri}
    else:
        img = {"type": "image", "id": _uid(), "source_from": "link",
               "platform_type": 1, "name": "", "image_url": image_url,
               "width": 0, "height": 0, "format": "", "title": "", "uri": image_url}

    pedit: dict[str, Any] = {"type": "", "id": _uid(),
                             "generate_type": spec["postedit_generate_type"]}
    if item_id is not None:
        pedit["item_id"] = item_id
    if origin_history_id is not None:
        pedit["origin_history_id"] = origin_history_id
    # 🔴 `origin_image` 是**输入图的唯一载体**：抓包里后续几跳省略它，是因为它们的父组件
    # 已经带着图（`parent_id` 串链）；本函数产出的是**单组件草稿**（没有父链），
    # 所以必须带上它 —— 否则服务端无从知道要处理哪张图。
    if image_uri or image_url:
        pedit["origin_image"] = img

    ability: dict[str, Any] = {"type": "", "id": node}
    ability.update(dict(spec["fields"]))
    if spec.get("core_param"):
        ability["core_param"] = {"type": "", "id": _uid(), **spec["core_param"]}
    if spec["generate_type"] == "pro_hd":
        ability["resolution_type"] = resolution_type
    if spec["generate_type"] == "painting":
        ability["up_scale"] = up_scale or {
            "type": "", "id": _uid(), "top": 0.9, "bottom": 0.9,
            "left": 0.9, "right": 0.9, "max_size": 12960, "image_ratio": 1,
        }
    ability["postedit_param"] = pedit

    comp: dict[str, Any] = {
        "type": "image_base_component", "id": comp_id, "min_version": "3.0.2",
    }
    if parent_id:
        comp["parent_id"] = parent_id
    comp["aigc_mode"] = "workbench"
    if spec["component_gen_type"] is not None:
        comp["gen_type"] = spec["component_gen_type"]
    comp["metadata"] = {"type": "", "id": _uid(), "created_platform": 3,
                        "created_platform_version": "",
                        "created_time_in_ms": str(_now_ms()), "created_did": ""}
    comp["generate_type"] = spec["generate_type"]
    # 🔴 `gen_option` 是**组件级**字段：它挂在组件的 `abilities` 下、与具体 ability
    # **平级**。文生图与 blend 都写在这里（参考仓自检原文：
    # `component_list[0]["abilities"]["gen_option"]["gen_count"] == 2`），
    # 后编辑族同属 `image_base_component` ⇒ 同一个位置也吃它。
    #
    # ⚠️ 这里原先**漏了**，于是"扩图固定出 4 张"看起来像上游的硬约束 ——
    # 其实是我们没传张数、上游才用了默认值。**同一个坑在 blend 上已经踩过一次。**
    comp["abilities"] = {
        "type": "", "id": _uid(), spec["ability"]: ability,
        "gen_option": {"type": "", "id": _uid(),
                       "gen_count": count, "generate_all": False},
    }

    draft = {
        "type": "draft", "id": _uid(), "min_version": "3.2.9", "min_features": [],
        "is_from_tsn": True, "version": DA_VERSION,
        "main_component_id": comp_id, "component_list": [comp],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 图生图（blend）
# ---------------------------------------------------------------------------
# 结构**逐字段照抄账号历史里的真实样本**（2026-09-19 重新读取，30 条 blend 记录）：
#
#   component_list[0]: type=image_base_component  min_version="3.0.2"
#                      aigc_mode="workbench"  gen_type=12  generate_type="blend"
#                      ⚠️ **没有 `metadata`**（与后编辑组件不同）
#     abilities.blend: {type, id, min_features:[], core_param, ability_list[],
#                       history_option, prompt_placeholder_info_list, postedit_param}
#
# ⇒ 也就是说 blend **不是**"另一个端点"，而是同一个 `component_list` 里的另一种组件，
#   与文生图（`abilities.generate`）和后编辑（`abilities.postedit_param`）并列。

#: blend 的 `ability_list[].name`。抓样里恒为 `byte_edit`
#: （对应 `DABlendAbilityName` 枚举；`image2image` 是另一个取值，未在样本里出现）。
BLEND_ABILITY_NAME = "byte_edit"


def build_blend_draft(*, prompt: str, image_uris: Sequence[str] | None = None,
                      image_uri: str = "", image_url: str = "",
                      source_from: str = "upload", model: str = DEFAULT_MODEL,
                      strength: float = 0.5, width: int = 2048, height: int = 2048,
                      resolution_type: str = "2k", count: int = 1) -> str:
    """构造**图生图（blend）**的 `draft_content`（JSON 字符串）。

    `source_from`：`upload` = 即梦存储里的 `image_uri`（**样本实测形态**）；
    `link` = 直接给外链（枚举里存在该取值，但**未被验证**）。

    ## 多张垫图

    blend 是唯一**原生用列表**承载输入图的能力：`ability_list[0]` 里的
    `image_uri_list`（uri 列表）与 `image_list`（图片对象列表）都是列表，
    所以多张就是往里多放元素 —— **不需要**组件串链（后编辑那三族才需要）。

    ⚠️ `image_uri`（单数）与 `image_uris`（复数）二选一；两个都给时以**复数**为准，
    这是为了不破坏既有单图调用点。每张图各拿一个独立 `id`（抓样里
    `image_list` 的元素都带 `id`，多个元素时应各自独立）。
    """
    uris = [u for u in (image_uris or ()) if u]
    if not uris and image_uri:
        uris = [image_uri]

    if not prompt or not prompt.strip():
        raise JimengParamError("blend 需要 prompt（描述要怎么改）", code=1001)
    if source_from == "upload" and not uris:
        raise JimengParamError("source_from=upload 时必须给 image_uri(s)", code=1001)
    if source_from != "upload" and not image_url:
        raise JimengParamError(f"source_from={source_from} 时必须给 image_url", code=1001)

    if source_from == "upload":
        imgs = [{"type": "image", "id": _uid(), "source_from": "upload",
                 "platform_type": 1, "name": "", "image_uri": u,
                 "width": 0, "height": 0, "format": "", "uri": u} for u in uris]
    else:
        imgs = [{"type": "image", "id": _uid(), "source_from": source_from,
                 "platform_type": 1, "name": "", "image_url": image_url,
                 "width": 0, "height": 0, "format": "", "uri": image_url}]

    comp_id = _uid()
    draft = {
        "type": "draft", "id": _uid(), "min_version": "3.0.2", "min_features": [],
        "is_from_tsn": True, "version": DA_VERSION,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id, "min_version": "3.0.2",
            "aigc_mode": "workbench", "gen_type": 12, "generate_type": "blend",
            "abilities": {
                "type": "", "id": _uid(),
                "blend": {
                    "type": "", "id": _uid(), "min_features": [],
                    "core_param": {
                        "type": "", "id": _uid(), "model": model, "prompt": prompt,
                        "sample_strength": strength,
                        "image_ratio": ratio_for_size(width, height),
                        "large_image_info": {"type": "", "id": _uid(),
                                             "height": height, "width": width,
                                             "resolution_type": resolution_type},
                    },
                    "ability_list": [{
                        "type": "", "id": _uid(), "name": BLEND_ABILITY_NAME,
                        "image_uri_list": (list(uris) if source_from == "upload"
                                           else []),
                        "image_list": imgs,
                        "strength": strength,
                    }],
                    "history_option": {"type": "", "id": _uid()},
                    "prompt_placeholder_info_list": [
                        {"type": "", "id": _uid(), "ability_index": 0}],
                    "postedit_param": {"type": "", "id": _uid(), "generate_type": 0},
                },
                # 🔴 张数控制就在这里 —— 与 `blend` **平级**（即 `abilities.gen_option`），
                # 与文生图共用同一个字段（参考仓自检：
                # `component_list[0]["abilities"]["gen_option"]["gen_count"] == 2`）。
                # ⚠️ 早先漏了它：请求 `n=1` 时上游按**模型默认**（实测 4 张）出图，
                # 于是调用方按 1 张的预期被按 4 张收费。`metrics_extra.generateCount`
                # 只是**埋点计数**、不是控制字段（实测它写 1 也照样出 4 张）。
                "gen_option": {
                    "type": "", "id": _uid(),
                    "gen_count": count, "generate_all": False,
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 文生视频（Seedance，t2v）
# ---------------------------------------------------------------------------
# 取证：2026-09-20 用户实抓网页端「文生视频」（babi_param 场景
# `makesame-text_to_video`）。与图片族**共用同一个端点**（`aigc_draft/generate`），
# 区别在草稿：`video_base_component` + `generate_type=gen_video` +
# `abilities.gen_video.text_to_video_params`。
#
# 🔴 **两个刻意保守的决定**（都源于"没有抓包依据就拒绝猜测"）：
# 1. **模型与计费档位白名单**：`extend.m_video_commerce_info` 里的
#    `benefit_type` / `amount` 是**计费字段**，写错等于按错的档位扣积分。
#    目前只有抓包里这一档（720p × 4s ⇒ `seedance_20_mini_720p_output_5s` / 4），
#    其余组合（1080p、5s/10s…）**没有抓包依据 ⇒ 拒绝构造**，当场 400 说清楚。
# 2. **视频草稿没有 `gen_option.gen_count`**：抓包里不存在该字段，
#    `batchNumber=1` 只是埋点。⇒ 视频一次一条，`n>1` 不支持（降级留痕）。

#: 抓包实测的视频模型（网页端 Seedance 4.0 Mini，t2v）。
DEFAULT_VIDEO_MODEL = "dreamina_seedance_40_mini"
#: 分辨率 → (benefit_type, 已实抓的输出时长秒集合)。
#: 🔴 计费口径（两条实抓联立解出）：amount = 输出秒数 + Σ输入视频秒数
#: （4s 无输入 ⇒ 4；5s + 10.35s 输入 ⇒ **15.35**）。
#: ✅ 2026-09-20 晚三份 UI 抓包：计费档位**逐模型**不同 ——
#:   mini：seedance_20_mini_720p_output_5s（4s/5s）
#:   vision（Seedance 4.0 Vision，UI 标 Fast 5s 免费试用）：dreamina_seedance_20_fast_5s
#:   pro_vision（Seedance 4.0 Pro Vision）：seedance_20_pro_720p_output（无 _5s 尾）
#: 名字全部**逐字照抄抓包**（别"纠正"大小写/前后缀）。
VIDEO_COMMERCE: dict[str, dict[str, tuple[str, tuple[int, ...]]]] = {
    "dreamina_seedance_40_mini": {
        "720p": ("seedance_20_mini_720p_output_5s", (4, 5)),
    },
    "dreamina_seedance_40_vision": {
        "720p": ("dreamina_seedance_20_fast_5s", (5,)),
    },
    "dreamina_seedance_40_pro_vision": {
        "720p": ("seedance_20_pro_720p_output", (5,)),
    },
    # ✅ 2026-09-24 实抓 + 三证对账（网页端真跑一次）：
    #   benefit_type 逐字照抄提交包；**实扣 45**（预扣 amount=5 只是占位，
    #   再次证明 amount 与实扣无关）。submit_id=59995987-… 见 docs/UPSTREAM.md §16。
    #   样片模式只有 480p（UI 实读：样片分辨率radiogroup 仅此一档）× 5s。
    "dreamina_seedance_45_pro_draft": {
        "480p": ("seedance_25_draft_480p_no_input_video_output", (5,)),
    },
    # ⚠️ `dreamina_seedance_45_pro`（2.5 正式版，480p/720p/1080p）**刻意未登记**：
    #   benefit_type 无提交包依据（网页端正式版还没抓过包）——按纪律拒绝猜。
}
#: 分辨率取值（抓包见 720p；1080p 是站点枚举但**无抓包样本**，登记仅供报错提示）。
#: `480p`：2026-09-24 实抓（2.5 样片模式提交包 resolution="480p"，小写）。
VIDEO_RESOLUTIONS: tuple[str, ...] = ("480p", "720p", "1080p")
DEFAULT_VIDEO_RESOLUTION = "720p"
#: 画面比例（图片族同一套枚举标签；抓包样本是 16:9）。
VIDEO_ASPECT_RATIOS: tuple[str, ...] = tuple(v[0] for v in IMAGE_RATIOS.values())
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_VIDEO_FPS = 24


def resolve_video_commerce(model: str, resolution: str, duration_s: int,
                           input_video_s: float = 0.0) -> tuple[str, float]:
    """查视频计费档位：返回 (benefit_type, 预扣 amount)。

    🔴 计费口径（实抓解出）：
    · amount = 输出秒数 + Σ输入视频秒数（音频不计入；≈1 积分/秒）；
    · benefit_type **逐模型不同**、逐字照抄抓包（见 VIDEO_COMMERCE 注释）。

    白名单外（未登记的模型/分辨率/时长）没有抓包依据 ⇒ 拒绝，绝不猜。
    """
    tiers = VIDEO_COMMERCE.get(model) or {}
    hit = tiers.get(resolution)
    if hit is None or int(duration_s) not in hit[1]:
        known = ", ".join(
            f"{m.split('_')[-2] + '_' + m.split('_')[-1]}:{r}×{d}s"
            for m, tiers_ in sorted(VIDEO_COMMERCE.items())
            for r, durs in tiers_.items() for d in durs)
        raise JimengParamError(
            f"视频计费档位 {model} × {resolution} × {duration_s}s 没有抓包依据，"
            f"本服务拒绝构造计费字段（benefit_type/amount 写错 = 按错档位扣积分）。"
            f"当前已实抓的档位：{known}。要放开新档位请先补抓对应请求包。",
            code=1001)
    amount = round(int(duration_s) + float(input_video_s), 2)
    return hit[0], amount


def build_video_draft(*, prompt: str,
                      model: str = DEFAULT_VIDEO_MODEL,
                      resolution: str = DEFAULT_VIDEO_RESOLUTION,
                      duration_ms: int = 4000,
                      aspect_ratio: str = DEFAULT_VIDEO_ASPECT_RATIO,
                      fps: int = DEFAULT_VIDEO_FPS,
                      seed: int | None = None,
                      min_version: str = "3.0.5",
                      min_features: list[str] | None = None,
                      draft_mode: bool = False,
                      metrics: dict[str, Any] | None = None) -> str:
    """构造**文生视频（t2v）**的 `draft_content` —— 返回 JSON 字符串（双重编码）。

    结构逐字段照抄 2026-09-20 的网页端实抓（t2v）。与图片族的关键差异：
    · 组件类型是 `video_base_component`（不是 `image_base_component`）；
    · `generate_type` 是 `gen_video`；
    · 参数在 `abilities.gen_video.text_to_video_params`，且**没有 `gen_option`**
      （视频草稿没有张数字段，一次一条）；
    · 组件带 `process_type: 1`（抓包形态）。

    2.5 家族（2026-09-24 实抓 `dreamina_seedance_45_pro_draft`）的差异：
    `min_version "3.3.28"`、`min_features ["AIGC_Video_Seedance25ResultAction"]`、
    `video_gen_inputs[0].is_draft_mode: true`（样片标记）。调用方按模型推导。
    """
    if not prompt or not prompt.strip():
        raise JimengParamError("prompt 不能为空（文生视频必填）", code=1001)
    video_task_extra = json.dumps(metrics or {}, ensure_ascii=False,
                                  separators=(",", ":"))
    comp_id = _uid()
    draft = {
        "type": "draft",
        "id": _uid(),
        "min_version": min_version,
        "min_features": list(min_features or []),
        "is_from_tsn": True,
        "version": DA_VERSION,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "video_base_component",
            "id": comp_id,
            "min_version": "1.0.0",
            "aigc_mode": "workbench",
            "metadata": {
                "type": "", "id": _uid(), "created_platform": 3,
                "created_platform_version": "",
                "created_time_in_ms": str(_now_ms()), "created_did": "",
            },
            "generate_type": "gen_video",
            "abilities": {
                "type": "", "id": _uid(),
                "gen_video": {
                    "type": "", "id": _uid(),
                    "text_to_video_params": {
                        "type": "", "id": _uid(),
                        "video_gen_inputs": [{
                            "type": "", "id": _uid(),
                            "min_version": min_version,
                            "prompt": prompt,
                            **({"is_draft_mode": True} if draft_mode else {}),
                            "video_mode": 2,
                            "fps": fps,
                            "duration_ms": duration_ms,
                            "resolution": resolution,
                            "idip_meta_list": [],
                        }],
                        "video_aspect_ratio": aspect_ratio,
                        "seed": seed if seed is not None
                                else random.randint(1, 2 ** 32 - 1),
                        "model_req_key": model,
                        "priority": 0,
                    },
                    # 抓包里这是 metrics_extra 的**原样复本**（埋点字符串），
                    # 挂在草稿内。照抄，不拼凑。
                    "video_task_extra": video_task_extra,
                },
            },
            "process_type": 1,
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


#: 视频补帧（insert_frame / VideoFrameInterpolation）的计费档位
#: （2026-09-20 实抓：`benefit_type "video_frame_interpolation"`，**amount 0**）。
VFI_BENEFIT_TYPE = "video_frame_interpolation"
VFI_AMOUNT = 0
#: 默认插帧目标帧率（实抓 24 → 60）。
DEFAULT_VFI_TARGET_FPS = 60
DEFAULT_VFI_ORIGIN_FPS = 24


def build_video_vfi_draft(source_draft: str | None, *, prompt: str, vid: str,
                          origin_history_id: str | int | None = None,
                          item_id: str | int | None = None,
                          resolution: str = DEFAULT_VIDEO_RESOLUTION,
                          duration_ms: int = 4000,
                          origin_fps: int = DEFAULT_VFI_ORIGIN_FPS,
                          target_fps: int = DEFAULT_VFI_TARGET_FPS,
                          insert_duration_ms: int | None = None,
                          metrics: dict[str, Any] | None = None) -> str:
    """构造**视频补帧**（`scene: "insert_frame"`）的 `draft_content`。

    两种形态（均有真跑实证，2026-09-20）：

    · **产物补帧**（`source_draft` 给出）：双组件 —— 父 = 源 t2v 组件**原样
      重放**（实抓连组件 id/created_time 都没变）+ 子带 `parent_id`、
      `process_type: 3`、`video_ref_params{item_id, origin_history_id}`
      （inputs 里 origin_history_id 是**字符串**、ref_params 里是**数字**）；
    · **本地视频补帧**（`source_draft=None`）：✅ 真跑实证支持！**单组件、
      无 `video_ref_params`、无 origin_history_id**，只有 `vid` ——
      本地/外部视频经 VOD 上传拿 vid 后即可补帧（72s 出片成功）。

    其它差异（两种形态同）：`min_version "3.1.0"`、子组件带 `scene:
    "insert_frame"`、`v2v_opt.insert_frame{enable,target_fps,origin_fps,
    duration_ms}`、`vid`/`lens_motion_type ""`/`motion_speed ""`/
    `template_id 0`；**没有 seed / video_aspect_ratio / model_req_key /
    priority**。insert 的 duration_ms 实抓里是源视频**实际**时长（4097>4000）；
    默认取请求时长。
    """
    if not prompt or not prompt.strip():
        raise JimengParamError("prompt 不能为空（补帧必填）", code=1001)
    if not vid:
        raise JimengParamError("补帧需要源视频 vid（VOD 上传产物）", code=1001)
    has_source = origin_history_id is not None and item_id is not None
    if has_source:
        if not source_draft:
            raise JimengParamError(
                "产物补帧需要源任务的 draft_json（父组件原样重放）", code=1001)
        try:
            source = json.loads(source_draft)
        except (TypeError, ValueError) as e:
            raise JimengParamError(f"源草稿不是合法 JSON：{e}", code=1001) from e
        comps = source.get("component_list") if isinstance(source, dict) else None
        if (not isinstance(comps, list) or not comps
                or comps[0].get("generate_type") != "gen_video"):
            raise JimengParamError(
                "源草稿不是视频任务草稿（component_list[0].generate_type != gen_video）"
                "—— 补帧只能引用视频产物。", code=1001)
    video_task_extra = json.dumps(metrics or {}, ensure_ascii=False,
                                  separators=(",", ":"))
    child_id = _uid()
    inp: dict[str, Any] = {
        "type": "", "id": _uid(),
        "min_version": "3.0.5",
        "prompt": prompt,
        "lens_motion_type": "",
        "motion_speed": "",
        "vid": vid,
        "video_mode": 2,
        "fps": origin_fps,
        "duration_ms": duration_ms,
        "template_id": 0,
        "v2v_opt": {
            "type": "", "id": _uid(),
            "min_version": "3.1.0",
            "insert_frame": {
                "type": "", "id": _uid(),
                "enable": True,
                "target_fps": target_fps,
                "origin_fps": origin_fps,
                "duration_ms": insert_duration_ms or duration_ms,
            },
        },
        "resolution": resolution,
        "idip_meta_list": [],
    }
    gen: dict[str, Any] = {
        "type": "", "id": _uid(),
        "text_to_video_params": {"type": "", "id": _uid(),
                                 "video_gen_inputs": [inp]},
        "scene": "insert_frame",
        "video_task_extra": video_task_extra,
    }
    child: dict[str, Any] = {
        "type": "video_base_component",
        "id": child_id,
        "min_version": "1.0.0",
        "aigc_mode": "workbench",
        "metadata": {
            "type": "", "id": _uid(), "created_platform": 3,
            "created_platform_version": "",
            "created_time_in_ms": str(_now_ms()), "created_did": "",
        },
        "generate_type": "gen_video",
        "abilities": {"type": "", "id": _uid(), "gen_video": gen},
        "process_type": 3,
    }
    if has_source:
        inp["origin_history_id"] = str(origin_history_id)
        gen["video_ref_params"] = {
            "type": "", "id": _uid(),
            "generate_type": 0,
            "item_id": int(item_id) if str(item_id).isdigit() else item_id,
            "origin_history_id": int(origin_history_id)
            if str(origin_history_id).isdigit() else origin_history_id,
        }
        child["parent_id"] = comps[0].get("id")  # type: ignore[union-attr]
        component_list = [comps[0], child]       # 父组件**原样**重放
    else:
        component_list = [child]                 # 本地视频：单组件
    draft = {
        "type": "draft",
        "id": _uid(),
        "min_version": "3.1.0",              # 补帧实抓是 3.1.0（t2v 是 3.0.5）
        "min_features": [],
        "is_from_tsn": True,
        "version": DA_VERSION,
        "main_component_id": child_id,
        "component_list": component_list,
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def build_video_omni_draft(*, instruction: str,
                           materials: list[dict[str, Any]],
                           model: str = DEFAULT_VIDEO_MODEL,
                           resolution: str = DEFAULT_VIDEO_RESOLUTION,
                           duration_ms: int = 4000,
                           aspect_ratio: str = DEFAULT_VIDEO_ASPECT_RATIO,
                           fps: int = DEFAULT_VIDEO_FPS,
                           seed: int | None = None,
                           metrics: dict[str, Any] | None = None) -> str:
    """构造**全能参考视频**（omni_reference / unified_edit）的 `draft_content`。

    结构逐字段照抄 2026-09-20 实抓（2 视频 + 1 图 + 1 音频的混合参考）。
    与 t2v 草稿的差异：

    · `min_version "3.3.9"` + `min_features ["AIGC_Video_UnifiedEdit"]`；
    · `prompt` 字段为 **""**，指令放在 `unified_edit_input.meta_list` 的
      text 条目里（照抄抓包形态）；
    · 素材在 `material_list`：video→`video_info.vid`、image→`image_info.image_uri`
      （走 ImageX 上传，与图生图同一链路）、audio→`audio_info.vid`
      （走 VOD 上传，产物 `vid`）；
    · **引用结构**：meta_list 用 `material_ref.material_idx` 指向素材下标 ——
      照抄抓包：**视频素材不进 meta_list**，图片/音频各一条，指令文本一条。

    `materials` 每项：`{"kind": "image"|"video"|"audio", "uri": <image_uri|vid>,
    "width"?, "height"?, "duration_ms"?, "name"?}`（已由服务层上传完成）。
    """
    if not instruction or not instruction.strip():
        raise JimengParamError("全能参考需要指令文本（prompt）", code=1001)
    if not materials:
        raise JimengParamError(
            "全能参考至少要一个参考素材（图/视频/音频）；纯文字请走 jimeng-t2v",
            code=1001)
    video_task_extra = json.dumps(metrics or {}, ensure_ascii=False,
                                  separators=(",", ":"))
    comp_id = _uid()
    material_list: list[dict[str, Any]] = []
    meta_list: list[dict[str, Any]] = []
    material_types: list[int] = []
    for idx, m in enumerate(materials):
        kind = m.get("kind")
        uid = _uid()
        if kind == "video":
            material_list.append({
                "type": "", "id": uid, "material_type": "video",
                "video_info": {"type": "video", "id": _uid(),
                               "source_from": "upload", "name": "",
                               "vid": m["uri"],
                               "fps": 0,
                               "width": int(m.get("width") or 0),
                               "height": int(m.get("height") or 0),
                               "duration": int(m.get("duration_ms") or 0),
                               "cover_image_url": ""}})
            # 🔴 照抄抓包：视频素材**不进 meta_list**
            material_types.append(2)
        elif kind == "image":
            material_list.append({
                "type": "", "id": uid, "material_type": "image",
                "image_info": {"type": "image", "id": _uid(),
                               "source_from": "upload", "platform_type": 1,
                               "name": "", "image_uri": m["uri"],
                               "aigc_image": {"type": "", "id": _uid()},
                               "width": int(m.get("width") or 0),
                               "height": int(m.get("height") or 0),
                               "format": "", "title": m.get("name") or "",
                               "uri": m["uri"]}})
            meta_list.append({"type": "", "id": _uid(), "meta_type": "image",
                              "text": "",
                              "material_ref": {"type": "", "id": _uid(),
                                               "material_idx": idx}})
            material_types.append(1)
        elif kind == "audio":
            material_list.append({
                "type": "", "id": uid, "material_type": "audio",
                "audio_info": {"type": "audio", "id": _uid(),
                               "source_from": "upload",
                               "vid": m["uri"],
                               "duration": int(m.get("duration_ms") or 0),
                               "name": m.get("name") or ""}})
            meta_list.append({"type": "", "id": _uid(), "meta_type": "audio",
                              "text": "",
                              "material_ref": {"type": "", "id": _uid(),
                                               "material_idx": idx}})
            material_types.append(3)
        else:
            raise JimengParamError(
                f"素材[{idx}] 的 kind {kind!r} 不认识（image/video/audio）",
                code=1001)
    meta_list.append({"type": "", "id": _uid(), "meta_type": "text",
                      "text": instruction})
    draft = {
        "type": "draft",
        "id": _uid(),
        "min_version": "3.3.9",
        "min_features": ["AIGC_Video_UnifiedEdit"],
        "is_from_tsn": True,
        "version": DA_VERSION,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "video_base_component",
            "id": comp_id,
            "min_version": "1.0.0",
            "aigc_mode": "workbench",
            "metadata": {
                "type": "", "id": _uid(), "created_platform": 3,
                "created_platform_version": "",
                "created_time_in_ms": str(_now_ms()), "created_did": "",
            },
            "generate_type": "gen_video",
            "abilities": {
                "type": "", "id": _uid(),
                "gen_video": {
                    "type": "", "id": _uid(),
                    "text_to_video_params": {
                        "type": "", "id": _uid(),
                        "video_gen_inputs": [{
                            "type": "", "id": _uid(),
                            "min_version": "3.3.9",
                            "prompt": "",                    # 照抄抓包：空
                            "video_mode": 2,
                            "fps": fps,
                            "duration_ms": duration_ms,
                            "resolution": resolution,
                            "idip_meta_list": [],
                            "unified_edit_input": {
                                "type": "", "id": _uid(),
                                "material_list": material_list,
                                "meta_list": meta_list,
                            },
                        }],
                        "video_aspect_ratio": aspect_ratio,
                        "seed": seed if seed is not None
                                else random.randint(1, 2 ** 32 - 1),
                        "model_req_key": model,
                        "priority": 0,
                    },
                    "video_task_extra": video_task_extra,
                },
            },
            "process_type": 1,
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class JimengClient:
    """即梦 mweb 客户端。

    `sessionid` 是**唯一必需**的凭据。传 `cookie` 可以带整串（多带无害），
    但只有 `sessionid` 会真的参与鉴权。

    ⚠️ 用 **httpx** 而非 requests：本服务已经需要 httpx 下输入图，
    少一个 HTTP 栈就少一类行为差异（超时语义、连接池、`trust_env`）。
    """

    def __init__(self, sessionid: str = "", *, cookie: str = "",
                 timeout: float = 60.0, poll_interval: float = 2.0,
                 poll_timeout: float = 300.0,
                 web_id: str | None = None,
                 workspace_id: int | None = None,
                 base: str = BASE,
                 transport: httpx.BaseTransport | None = None,
                 user_agent: str | None = None,
                 capture_upstream: bool = True) -> None:
        jar = _parse_cookie(cookie) if cookie else {}
        if sessionid:
            jar["sessionid"] = sessionid
        if not jar.get("sessionid"):
            raise JimengAuthError("缺少 sessionid —— 即梦唯一的硬前提凭据")
        self.jar = jar
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        # webId 在抓包里等于 `_tea_web_id` cookie；没有就随机造一个 19 位数字
        self.web_id = (web_id or jar.get("_tea_web_id")
                       or str(random.randint(10 ** 18, 10 ** 19 - 1)))
        self.workspace_id = workspace_id
        # cookie 挂在 **client 上**，不是每个请求上传：httpx 已弃用
        # per-request `cookies=`（"cookie 持久化的预期行为有歧义"），
        # 留着它等于给自己埋一个未来版本必然爆的坑。
        self._client = httpx.Client(timeout=timeout, transport=transport,
                                    cookies=self.jar)
        #: 是否把上游原始报文绑成 span 属性（观测面口径，见 observability 模块）
        self._capture = capture_upstream
        #: 最近一次 submit 产生的降级告警（张数吸附等）—— 便于调用方取回并上报
        self.last_warnings: list[str] = []
        #: 最近一次提交用的 `draft_content`（JSON 串）—— 续生成（`action=2`）
        #: 要求**原样再带一遍草稿**（用户抓包确认），所以必须能取回。
        self.last_draft: str = ""
        self.ua = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JimengClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------ 内部工具

    def _url(self, path: str, extra: dict[str, str] | None = None) -> str:
        q = {
            "aid": APPID, "device_platform": "web", "region": "cn",
            "webId": self.web_id, "da_version": DA_VERSION,
            "web_version": WEB_VERSION, "aigc_features": "app_lip_sync",
        }
        q.update(extra or {})
        return f"{self.base}{path}?" + "&".join(f"{k}={v}" for k, v in q.items())

    def _headers(self, path: str) -> dict[str, str]:
        h = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-sdk-version": APP_SDK_VERSION,
            "appid": APPID,
            "appvr": APPVR,
            "content-type": "application/json",
            "lan": "zh-Hans",
            "loc": "cn",
            "origin": self.base,
            "pf": PF,
            "priority": "u=1, i",
            "referer": f"{self.base}/ai-tool/generate"
                       + (f"?workspace={self.workspace_id}" if self.workspace_id else ""),
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", '
                         '"Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "tdid": "",
            "user-agent": self.ua,
        }
        # sign 不是硬前提（实测），但纯 MD5 零成本，带上以贴合浏览器形态
        h.update(sign_headers(path))
        return h

    def _post(self, path: str, body: dict, *,
              extra_q: dict[str, str] | None = None) -> dict:
        """**上游 HTTP 的唯一收缝** —— 埋点只在这里包一次。

        · 一条 span + 一条带 `duration_ms` 的记点（时长是**实测**的，
          不是 span 自身时长 —— span 是我们自己包出来的区间，属性才是量出来的值）；
        · `http_path` 只给 path、**丢掉 query**（query 里有 webId/da_version，
          而且签名类参数可能就藏在 query 里）；
        · 请求/响应体经 `parsed_json` 变成**可展开的 dict 属性**，
          面板里能按 `upstream_response_json.ret` 这类路径过滤。
        """
        started = time.perf_counter()
        ok = False
        status_code: int | None = None
        resp_body: dict | Any = None
        try:
            r = self._client.post(
                self._url(path, extra_q), headers=self._headers(path),
                content=json.dumps(body, ensure_ascii=False).encode())
            status_code = r.status_code
            try:
                data = r.json()
            except Exception as e:
                raise JimengError(
                    f"jimeng 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]!r}",
                    retryable=r.status_code >= 500) from e
            resp_body = data
            ret = data.get("ret")
            if str(ret) != "0":
                raise_for_ret(ret, data)
            ok = True
            return data
        except httpx.HTTPError as e:
            raise JimengError(f"jimeng 请求失败：{e}", retryable=True) from e
        finally:
            self._report(path, body, resp_body, status_code,
                         round((time.perf_counter() - started) * 1000, 2), ok)

    def _report(self, path: str, body: dict, resp: Any, status: int | None,
                duration_ms: float, ok: bool) -> None:
        """上报一次上游调用。

        🔴 **刻意不在这里包 `try/except`。** 「观测绝不阻塞业务」这条不变量
        已经由 `Observability._emit` 自己保证（它内部吞掉一切）；
        在这里再包一层，只会把 `NameError`（比如曾经漏掉的 `OBS` import）
        一起吞掉 —— 症状是"埋点静默不生效、日志里一个字都没有"，
        比业务报错难查得多。接线错误就该当场炸。
        """
        capture = self._capture
        OBS.upstream(
            method="POST", path=path, status=status,
            duration_ms=duration_ms,
            # 观测面口径：上游明细全量上报；凭据的值在**取值处**就没进这里
            # （见 observability 模块 docstring 的三条实现约束）
            body=body if capture else None,
            response=resp if capture else None,
            error=None if ok else "upstream_rejected",
        )

    # ------------------------------------------------------------ 建任务

    def submit(self, prompt: str, *, model: str = DEFAULT_MODEL,
               size: str = DEFAULT_SIZE, count: int = 1,
               negative_prompt: str = "", seed: int | None = None,
               submit_id: str | None = None, dry_run: bool = False,
               count_options: tuple[int, ...] | None = None) -> str:
        """建任务，返回 `submit_id`（**计费动作**）。

        `submit_id` 由客户端生成 ⇒ 调用方拿到它就能独立轮询，不必依赖本次回执。
        `dry_run=True` 时**不发任何请求**，只走完构造与吸附并返回 id。
        """
        width, height = parse_size(size)
        count, warn = resolve_count(model, count, count_options)
        self.last_warnings = [warn] if warn else []
        draft = build_draft(prompt=prompt, model=model, count=count,
                            width=width, height=height,
                            negative_prompt=negative_prompt, seed=seed)
        sid = submit_id or _uid()
        if dry_run:
            return sid
        body = {
            "extend": {"root_model": model,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {})},
            "submit_id": sid,
            "metrics_extra": json.dumps({
                "promptSource": "custom", "generateCount": count,
                "generateId": sid, "templateId": "0", "enterFrom": "click",
                "isRegenerate": False, "isBoxSelect": False, "isCutout": False,
            }, separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self.last_draft = draft
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 图生图

    def blend(self, prompt: str, *, image_uri: str = "", image_url: str = "",
              source_from: str = "upload", model: str = DEFAULT_MODEL,
              strength: float = 0.5, size: str = DEFAULT_SIZE,
              submit_id: str | None = None, dry_run: bool = False,
              image_uris: Sequence[str] | None = None,
              count: int = 1,
              count_options: tuple[int, ...] | None = None) -> str:
        """提交一个**图生图（blend）**任务，返回 `submit_id`（**计费动作**）。

        结构照抄账号历史里的真实 blend 样本。
        `metrics_extra` 用 blend 专用形态（**没有** `generateId`/`isRegenerate`，
        与文生图不同）—— 照抄样本，不拼凑。

        `image_uris` 多张垫图；与 `image_uri`（单张）二选一，复数优先。

        `count` 出图张数，走**与文生图同一套吸附**（`generate_count_options`）。
        """
        width, height = parse_size(size)
        count, warn = resolve_count(model, count, count_options)
        # ⚠️ 必须在 dry_run 早退**之前**设好，否则 dry 路径会把告警漏掉
        self.last_warnings = [warn] if warn else []
        draft = build_blend_draft(prompt=prompt, image_uris=image_uris,
                                  image_uri=image_uri,
                                  image_url=image_url, source_from=source_from,
                                  model=model, strength=strength,
                                  width=width, height=height, count=count)
        sid = submit_id or _uid()
        if dry_run:
            return sid
        body = {
            "extend": {"root_model": model,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {})},
            "submit_id": sid,
            "metrics_extra": json.dumps({
                "templateId": "", "generateCount": count, "promptSource": "custom",
                "templateSource": "", "lastRequestId": "", "originRequestId": "",
            }, separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self.last_draft = draft
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 文生视频

    def submit_video(self, prompt: str, *, model: str = DEFAULT_VIDEO_MODEL,
                     resolution: str = DEFAULT_VIDEO_RESOLUTION,
                     duration_ms: int = 4000,
                     aspect_ratio: str = DEFAULT_VIDEO_ASPECT_RATIO,
                     seed: int | None = None,
                     submit_id: str | None = None,
                     dry_run: bool = False) -> str:
        """提交一个**文生视频（t2v）**任务，返回 `submit_id`（**计费动作**）。

        形态逐字段照抄 2026-09-20 网页端实抓（Seedance 4.0 Mini，720p × 4s）。
        与图片族共用端点 `aigc_draft/generate`；差异全在草稿与 `extend`：

        · `extend.m_video_commerce_info`（**计费字段**）按 (分辨率, 时长)
          查 `VIDEO_COMMERCE` 白名单，**查不到当场拒绝**；
        · `metrics_extra` 是视频专用形态（`functionMode=omni_reference` 等），
          同时**原样复本**挂进草稿的 `video_task_extra`。

        ⚠️ 视频草稿**没有张数字段**（抓包里不存在 `gen_option`）—— 一次一条。
        `dry_run=True` 时**不发任何请求**，只走完构造与档位校验。
        """
        duration_s = int(duration_ms // 1000)
        if duration_ms <= 0 or duration_ms % 1000:
            raise JimengParamError(
                f"duration_ms 必须是正的整秒（毫秒），实得 {duration_ms}", code=1001)
        if resolution not in VIDEO_RESOLUTIONS:
            raise JimengParamError(
                f"resolution 只接受 {list(VIDEO_RESOLUTIONS)}，实得 {resolution!r}",
                code=1001)
        if aspect_ratio not in VIDEO_ASPECT_RATIOS:
            raise JimengParamError(
                f"aspect_ratio 只接受 {list(VIDEO_ASPECT_RATIOS)}，"
                f"实得 {aspect_ratio!r}", code=1001)
        benefit, amount = resolve_video_commerce(model, resolution, duration_s)
        sid = submit_id or _uid()
        # `metrics_extra` 与草稿内 `video_task_extra` 是同一份内容的两种挂法
        metrics: dict[str, Any] = {
            "isDefaultSeed": 1, "originSubmitId": sid, "isRegenerate": False,
            "enterFrom": "ai_feature", "position": "page_bottom_box",
            "aiFeatureName": "21228507900428", "promptType": "original_prompt",
            "functionMode": "omni_reference", "generatorFeature": "omniReference",
            "sceneOptions": json.dumps([{
                "type": "video", "scene": "BasicVideoGenerateButton",
                "resolution": resolution, "modelReqKey": model,
                "videoDuration": duration_s, "batchNumber": 1,
                "inputVideoDuration": 0, "chargeInputVideoDuration": True,
                "isLongVideo": False, "hasInputVideo": False,
                # 🔴 照抄抓包：只有 vision（Fast）带免费试用标记
                "useSeedanceFast5sFreeTrial": model == "dreamina_seedance_40_vision",
                "reportParams": {
                    "enterSource": "generate", "vipSource": "generate",
                    "extraVipFunctionKey": f"{model}-{resolution}",
                    "useVipFunctionDetailsReporterHoc": True},
                "materialTypes": [],
            }], separators=(",", ":")),
            "batchNumber": 1, "submitGroupId": _uid(), "hasRejectedAudit": 0,
            # 2.5 样片实抓多出 videoStage（2026-09-24 提交包）
            **({"videoStage": "draft"} if model.endswith("_draft") else {}),
        }
        # 2.5 样片家族（模型 key 以 `_draft` 结尾）：min_version 3.3.28 +
        # min_features + is_draft_mode —— 差异全部来自 2026-09-24 实抓。
        draft_mode = model.endswith("_draft")
        draft = build_video_draft(
            prompt=prompt, model=model, resolution=resolution,
            duration_ms=duration_ms, aspect_ratio=aspect_ratio,
            seed=seed,
            min_version="3.3.28" if draft_mode else "3.0.5",
            min_features=(["AIGC_Video_Seedance25ResultAction"]
                          if draft_mode else None),
            draft_mode=draft_mode,
            metrics=metrics)
        # ⚠️ 必须在 dry_run 早退**之前**设好（与 blend/edit 同一教训）
        self.last_warnings = []
        self.last_draft = draft
        if dry_run:
            return sid
        commerce = {"amount": amount, "benefit_type": benefit,
                    "resource_id": "generate_video", "resource_id_type": "str",
                    "resource_sub_type": "aigc"}
        body = {
            "extend": {"root_model": model, "m_video_commerce_info": commerce,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {}),
                       "m_video_commerce_info_list": [dict(commerce)]},
            "submit_id": sid,
            "metrics_extra": json.dumps(metrics, ensure_ascii=False,
                                        separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 视频补帧

    def submit_video_vfi(self, source_draft: str | None, *, prompt: str,
                         vid: str,
                         origin_history_id: str | int | None = None,
                         item_id: str | int | None = None,
                         resolution: str = DEFAULT_VIDEO_RESOLUTION,
                         duration_ms: int = 4000,
                         origin_fps: int = DEFAULT_VFI_ORIGIN_FPS,
                         target_fps: int = DEFAULT_VFI_TARGET_FPS,
                         source_submit_id: str | None = None,
                         source_item_id: str | int | None = None,
                         submit_id: str | None = None,
                         dry_run: bool = False) -> str:
        """提交一个**视频补帧**任务，返回 `submit_id`（计费动作，实抓 amount=0）。

        两种形态（均真跑实证）：
        · `source_draft` + 三件引用：产物补帧（父组件重放形态）；
        · `source_draft=None`：**本地/外部视频补帧** —— vid 由 VodUploader
          上传获得，单组件形态，72s 真跑出片。
        `dry_run=True` 时**不发任何请求**。
        """
        if duration_ms <= 0 or duration_ms % 1000:
            raise JimengParamError(
                f"duration_ms 必须是正的整秒（毫秒），实得 {duration_ms}", code=1001)
        if resolution not in VIDEO_RESOLUTIONS:
            raise JimengParamError(
                f"resolution 只接受 {list(VIDEO_RESOLUTIONS)}，实得 {resolution!r}",
                code=1001)
        sid = submit_id or _uid()
        duration_s = duration_ms // 1000
        basic_scene = {
            "type": "video", "scene": "BasicVideoGenerateButton",
            "resolution": resolution,
            "modelReqKey": DEFAULT_VIDEO_MODEL,
            "videoDuration": duration_s, "batchNumber": 1,
            "inputVideoDuration": 0, "chargeInputVideoDuration": True,
            "isLongVideo": False, "hasInputVideo": False,
            "useSeedanceFast5sFreeTrial": False,
            "reportParams": {
                "enterSource": "generate", "vipSource": "generate",
                "extraVipFunctionKey": f"{DEFAULT_VIDEO_MODEL}-{resolution}",
                "useVipFunctionDetailsReporterHoc": True},
            "materialTypes": [],
        }
        vfi_scene = {"type": "video", "scene": "VideoFrameInterpolation",
                     "videoDuration": duration_s,
                     "reportParams": {"enterSource": "generate"}}
        origin_ref = source_item_id if source_item_id is not None else item_id
        metrics: dict[str, Any] = {
            "promptSource": "custom", "isDefaultSeed": 1,
            "originSubmitId": source_submit_id or sid,
            "enterFrom": "click", "batchNumber": 1,
            "promptType": "original_prompt",
            "submitGroupId": _uid(), "isRegenerate": False,
            "functionMode": "omni_reference", "generatorFeature": "omniReference",
            "sceneOptions": json.dumps([basic_scene, vfi_scene],
                                       separators=(",", ":")),
            "originId": str(origin_ref),
            "previewSubmitId": source_submit_id or sid,
        }
        draft = build_video_vfi_draft(
            source_draft, prompt=prompt, vid=vid,
            origin_history_id=origin_history_id, item_id=item_id,
            resolution=resolution, duration_ms=duration_ms,
            origin_fps=origin_fps, target_fps=target_fps, metrics=metrics)
        self.last_warnings = []
        self.last_draft = draft
        if dry_run:
            return sid
        commerce = {"amount": VFI_AMOUNT, "benefit_type": VFI_BENEFIT_TYPE,
                    "resource_id": "generate_video", "resource_id_type": "str",
                    "resource_sub_type": "aigc"}
        body = {
            "extend": {"root_model": DEFAULT_VIDEO_MODEL,
                       "m_video_commerce_info": commerce,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {}),
                       "m_video_commerce_info_list": [dict(commerce)]},
            "submit_id": sid,
            "metrics_extra": json.dumps(metrics, ensure_ascii=False,
                                        separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 全能参考视频

    def submit_video_omni(self, instruction: str, *,
                          materials: list[dict[str, Any]],
                          resolution: str = DEFAULT_VIDEO_RESOLUTION,
                          duration_ms: int = 4000,
                          aspect_ratio: str = DEFAULT_VIDEO_ASPECT_RATIO,
                          seed: int | None = None,
                          input_video_s: float = 0.0,
                          submit_id: str | None = None,
                          dry_run: bool = False) -> str:
        """提交一个**全能参考视频**任务，返回 `submit_id`（**计费动作**）。

        形态照抄 2026-09-20 实抓（omni_reference，混合参考 2 视频+1 图+1 音频）。
        `materials` 每项 `{"kind", "uri", ...}` —— 素材**必须已上传**
        （image→image_uri 走 ImageX；video/audio→vid 走 VOD，见 VodUploader）。

        计费：amount = 输出秒数 + Σ输入视频秒数（音频不计），见
        `resolve_video_commerce`；输入视频时长服务层探测不到时按 0 计并留痕。
        `dry_run=True` 不发请求。
        """
        duration_s = int(duration_ms // 1000)
        if duration_ms <= 0 or duration_ms % 1000:
            raise JimengParamError(
                f"duration_ms 必须是正的整秒（毫秒），实得 {duration_ms}", code=1001)
        if resolution not in VIDEO_RESOLUTIONS:
            raise JimengParamError(
                f"resolution 只接受 {list(VIDEO_RESOLUTIONS)}，实得 {resolution!r}",
                code=1001)
        if aspect_ratio not in VIDEO_ASPECT_RATIOS:
            raise JimengParamError(
                f"aspect_ratio 只接受 {list(VIDEO_ASPECT_RATIOS)}，"
                f"实得 {aspect_ratio!r}", code=1001)
        benefit, amount = resolve_video_commerce(
            DEFAULT_VIDEO_MODEL, resolution, duration_s,
                                                 input_video_s=input_video_s)
        sid = submit_id or _uid()
        type_code = {"video": 2, "image": 1, "audio": 3}
        material_types = [type_code.get(m.get("kind"), 0) for m in materials]
        input_video = any(m.get("kind") == "video" for m in materials)
        metrics: dict[str, Any] = {
            "isDefaultSeed": 1, "originSubmitId": sid, "isRegenerate": False,
            "enterFrom": "click", "position": "page_bottom_box",
            "promptType": "original_prompt",
            "functionMode": "omni_reference", "generatorFeature": "omniReference",
            "sceneOptions": json.dumps([{
                "type": "video", "scene": "BasicVideoGenerateButton",
                "resolution": resolution, "modelReqKey": DEFAULT_VIDEO_MODEL,
                "videoDuration": duration_s, "batchNumber": 1,
                "inputVideoDuration": round(float(input_video_s), 2),
                "chargeInputVideoDuration": True,
                "isLongVideo": False,
                "hasInputVideo": input_video,
                "useSeedanceFast5sFreeTrial": False,
                "reportParams": {
                    "enterSource": "generate", "vipSource": "generate",
                    "extraVipFunctionKey": f"{DEFAULT_VIDEO_MODEL}-{resolution}",
                    "useVipFunctionDetailsReporterHoc": True},
                "materialTypes": material_types,
            }], separators=(",", ":")),
            "batchNumber": 1, "submitGroupId": _uid(), "hasRejectedAudit": 0,
        }
        draft = build_video_omni_draft(
            instruction=instruction, materials=materials,
            resolution=resolution, duration_ms=duration_ms,
            aspect_ratio=aspect_ratio, seed=seed, metrics=metrics)
        self.last_warnings = []
        self.last_draft = draft
        if dry_run:
            return sid
        commerce = {"amount": amount, "benefit_type": benefit,
                    "resource_id": "generate_video", "resource_id_type": "str",
                    "resource_sub_type": "aigc"}
        body = {
            "extend": {"root_model": DEFAULT_VIDEO_MODEL,
                       "m_video_commerce_info": commerce,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {}),
                       "m_video_commerce_info_list": [dict(commerce)]},
            "submit_id": sid,
            "metrics_extra": json.dumps(metrics, ensure_ascii=False,
                                        separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 后编辑

    def edit(self, tool: str, *, image_uri: str = "", image_url: str = "",
             item_id: int | None = None, origin_history_id: int | None = None,
             source_from: str = "upload", size: str = DEFAULT_SIZE,
             resolution_type: str = "2k",
             up_scale: dict | None = None,
             submit_id: str | None = None, dry_run: bool = False,
             count: int = 1,
             count_options: tuple[int, ...] | None = None) -> str:
        """提交一个**后编辑**任务（智能超清/超清/扩图/细节修复），返回 `submit_id`。

        **计费动作**（与 `submit` 同级）。工具清单见 `POST_EDIT_TOOLS`。
        输入图必须是即梦存储里的 `image_uri`（`source_from="upload"`）。

        `count` 出图张数，**与文生图/blend 走同一套吸附**（`generate_count_options`）：
        草稿里同样是 `abilities.gen_option.gen_count`（组件级字段）。
        ⚠️ 原先没传它，所以"扩图固定出 4 张"其实是**上游默认值**、不是硬约束。
        """
        width, height = parse_size(size)
        # 后编辑族提交时 `extend.root_model` 用的就是 `DEFAULT_MODEL`（见下方 body），
        # 所以张数选项也要按它查，否则查的是另一个模型的合法区间。
        count, warn = resolve_count(DEFAULT_MODEL, count, count_options)
        # ⚠️ 在 dry_run 早退**之前**设好，否则 dry 路径会把告警漏掉
        self.last_warnings = [warn] if warn else []
        draft = build_post_edit_draft(
            tool=tool, image_uri=image_uri, image_url=image_url,
            item_id=item_id, origin_history_id=origin_history_id,
            source_from=source_from, width=width, height=height,
            resolution_type=resolution_type, up_scale=up_scale, count=count)
        sid = submit_id or _uid()
        spec = POST_EDIT_TOOLS[tool]
        if dry_run:
            return sid

        metrics: dict[str, Any] = {
            "promptSource": "custom", "generateCount": count,
            "generateId": sid, "templateId": "0", "enterFrom": "click",
            "isRegenerate": False, "isBoxSelect": False, "isCutout": False,
        }
        if item_id is not None:
            metrics["originItemId"] = str(item_id)
        if spec.get("scene"):
            metrics["sceneOptions"] = json.dumps([{
                "type": "image", "scene": spec["scene"],
                "proHDResolutionType": resolution_type,
                "reportParams": {"enterSource": "generate",
                                 "extraVipFunctionKey": str(item_id or "")},
            }], separators=(",", ":"))

        body = {
            "extend": {"root_model": DEFAULT_MODEL,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {})},
            "submit_id": sid,
            "metrics_extra": json.dumps(metrics, separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self.last_draft = draft
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------ 续生成（action=2）

    def continue_task(self, history_id: str, draft: str, *,
                      count: int = 1, submit_id: str | None = None,
                      dry_run: bool = False) -> str:
        """**续生成** —— 让上游把原任务剩下的几张接着生成出来（`action=2`）。

        形态**逐字来自**用户 2026-09-20 的实抓（网页端"继续生成"按钮）：

        | 字段 | 值/含义 |
        |---|---|
        | `action` | **2** —— 首次提交**不带**它，它才是"续"的标记 |
        | `history_id` | 原任务的 history id（= 回执里的 `history_record_id`） |
        | `draft_content` | **原样再带一遍草稿**（所以草稿必须先持久化） |
        | `metrics_extra.generateCount` | 一次续 1 张 |

        ⚠️ **这是计费动作**（多一次真实生成）⇒ 调用方**必须有上限 + 每次留痕**，
        不能静默循环。

        `history_id` 缺了就直接报错：拿不到原任务上下文，"续"出来的可能是别的东西。
        """
        if not history_id:
            raise JimengParamError(
                "续生成需要 history_id（取回执里的 history_record_id）")
        count, warn = resolve_count(DEFAULT_MODEL, count, None)
        self.last_warnings = [warn] if warn else []
        sid = submit_id or _uid()
        self.last_draft = draft
        if dry_run:
            return sid
        body = {
            "extend": {"root_model": DEFAULT_MODEL,
                       **({"workspace_id": self.workspace_id}
                          if self.workspace_id else {})},
            "action": 2,
            "submit_id": sid,
            "history_id": history_id,
            "metrics_extra": json.dumps({
                "promptSource": "custom", "generateCount": count,
                "generateId": sid, "templateId": "0", "enterFrom": "click",
                "isRegenerate": False, "isBoxSelect": False, "isCutout": False,
            }, separators=(",", ":")),
            "draft_content": draft,
            "http_common_info": {"aid": int(APPID)},
        }
        self._post(PATH_SUBMIT, body)
        return sid

    # ------------------------------------------------------------ 资产查询

    def image_by_uri(self, uris: str | list[str]) -> dict[str, dict]:
        """按 `image_uri` 换取可访问的签名 URL（只读、零额度）。

        ⚠️ body 字段是 **`uris`（复数）**：传 `image_uri` 会得到
        `ret=1000 invalid parameter`（2026-09-18 实测）。
        返回 `{uri: {image_uri, image_url, ...}}`。
        """
        lst = [uris] if isinstance(uris, str) else list(uris)
        if not lst:
            return {}
        d = self._post(PATH_IMAGE_BY_URI, {"uris": lst})
        return d.get("uri2image") or {}

    def common_config(self, *, model: str | None = None) -> dict:
        """读服务端**模型能力表**（`GET/POST get_common_config`）—— 只读、零成本。

        body 为 `{}`（抓包形态）；`model` 仅用于日志/上下文，服务端按 tk 下发全集。
        上游随时会调这张表，所以**不要**把它当常量缓存到代码里（见 capabilities.py）。
        """
        _ = model
        return self._post(PATH_COMMON_CONFIG, {})

    # ------------------------------------------------------------ 取任务

    def fetch_many(self, submit_ids: list[str]) -> dict[str, TaskState]:
        """**一次**查多个任务状态（只读、零额度）。

        上游 `get_history_by_ids` 吃的就是 **`submit_ids`（复数）**，
        所以我们没有理由按任务逐个打 —— 并发 N 个在途任务时，
        逐个查会让上游请求量随 N **线性增长**，而合并成一次就与 N 无关。
        这与"上游风控最敏感的是请求频次"是同一件事的两面。

        返回 `{submit_id: TaskState}`；某个 id 还没落库时给 `init` 态（不是错误）。
        """
        ids = [s for s in submit_ids if s]
        if not ids:
            return {}
        data = self._post(PATH_HISTORY, {"submit_ids": ids})
        payload = data.get("data") or {}
        out: dict[str, TaskState] = {}
        for sid in ids:
            node = payload.get(sid)
            if isinstance(node, dict):
                out[sid] = parse_task(sid, node)
            else:
                # 刚提交时可能还没落库 —— 不是错误，返回 init 态
                out[sid] = TaskState(submit_id=sid, status=0, status_name="init")
        return out

    def fetch(self, submit_id: str) -> TaskState:
        """查**单个**任务状态。单条入口，内部走同一个批量实现（避免两套解析）。"""
        return self.fetch_many([submit_id]).get(
            submit_id, TaskState(submit_id=submit_id, status=0, status_name="init"))

    def wait(self, submit_id: str, *, interval: float | None = None,
             timeout: float | None = None) -> TaskState:
        """轮询到终态。超时抛 `JimengTimeout`（`submit_id` 仍可继续查）。"""
        interval = self.poll_interval if interval is None else interval
        limit = self.poll_timeout if timeout is None else timeout
        deadline = time.time() + limit
        last = TaskState(submit_id=submit_id)
        while True:
            last = self.fetch(submit_id)
            if last.finished:
                return last
            if time.time() >= deadline:
                raise JimengTimeout(
                    f"jimeng 轮询超时（{limit}s，最后状态 {last.status_name}）；"
                    f"任务可能仍在跑，可用 submit_id={submit_id} 继续查询",
                    code=last.status)
            time.sleep(interval)

    def generate(self, prompt: str, *, model: str = DEFAULT_MODEL,
                 size: str = DEFAULT_SIZE, count: int = 1,
                 negative_prompt: str = "", seed: int | None = None,
                 dry_run: bool = False) -> TaskState:
        """建任务 + 轮询到终态。

        `dry_run=True` 时**只构造请求、不发任何真实生成**（返回 dry_run 态）——
        用来检查翻译结果，零积分消耗。
        """
        sid = self.submit(prompt, model=model, size=size, count=count,
                          negative_prompt=negative_prompt, seed=seed,
                          dry_run=dry_run)
        if dry_run:
            return TaskState(submit_id=sid, status=0, status_name="dry_run",
                             raw={"dry_run": True,
                                  "draft_preview": _draft_preview(prompt, model,
                                                                  size, count)})
        return self.wait(sid)


def raise_for_ret(ret: Any, data: dict) -> None:
    """把上游 `ret` 翻成异常。**查表得出**，不写一长串 if。"""
    try:
        code = int(ret)
    except (TypeError, ValueError):
        code = None
    name = ERR_NO.get(code, "unknown") if code is not None else "unknown"
    msg = data.get("errmsg") or data.get("message") or data.get("msg") or ""
    text = f"jimeng 拒绝请求：ret={ret}（{name}）{msg}"
    extra = {"logid": data.get("logid"), "ret": ret}
    if code == 1015:
        raise JimengAuthError(
            text + "；cookie 里没有有效的 sessionid（或已过期）", code=code, **extra)
    for codes, cls in _ERROR_CLASSES:
        if code in codes:
            raise cls(text, code=code, **extra)
    raise JimengError(text, code=code, retryable=False, **extra)


def _draft_preview(prompt: str, model: str, size: str, count: int) -> dict:
    w, h = parse_size(size)
    ratio = ratio_for_size(w, h)
    return {"model": model, "prompt": prompt, "size": size,
            "width": w, "height": h, "count": count,
            "image_ratio": ratio, "image_ratio_label": IMAGE_RATIOS[ratio][0]}


def _parse_cookie(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


__all__ = [
    "JimengClient", "TaskState", "GeneratedImage",
    "JimengError", "JimengAuthError", "JimengRateLimitError", "JimengQuotaError",
    "JimengRiskError", "JimengContentError", "JimengParamError", "JimengTimeout",
    "build_draft", "build_blend_draft", "build_post_edit_draft", "build_video_draft",
    "parse_task",
    "parse_size", "ratio_for_size", "raise_for_ret", "count_options", "resolve_count",
    "resolve_video_commerce",
    "COUNT_OPTIONS_BY_MODEL", "DEFAULT_COUNT_OPTIONS",
    "BLEND_ABILITY_NAME", "POST_EDIT_TOOLS", "POSTEDIT_GENERATE_TYPE",
    "classify_reject", "REJECT_VERDICTS",
    "TASK_STATUS", "TASK_STATUS_TERMINAL", "TASK_STATUS_OK",
    "IMAGE_RATIOS", "ERR_NO",
    "CODES_RATE_LIMIT", "CODES_QUOTA", "CODES_RISK", "CODES_CONTENT", "CODES_PARAM",
    "DEFAULT_MODEL", "DEFAULT_SIZE", "BASE",
    "PATH_SUBMIT", "PATH_HISTORY", "PATH_HISTORY_LIST", "PATH_IMAGE_BY_URI",
    "PATH_UPLOAD_TOKEN", "PATH_COMMON_CONFIG",
    "DEFAULT_VIDEO_MODEL", "DEFAULT_VIDEO_RESOLUTION", "DEFAULT_VIDEO_ASPECT_RATIO",
    "VIDEO_COMMERCE", "VIDEO_RESOLUTIONS", "VIDEO_ASPECT_RATIOS",
]
