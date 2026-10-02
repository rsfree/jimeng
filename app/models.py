#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力注册表：`model` 字符串 → 即梦的一个能力。

## 为什么用 `model` 承载能力

参考接口（`/async/v1/images/generations`）的 body 是
`{model, prompt, image}` —— `model` 是唯一能承载"要哪一个能力"的字段，
这与 OpenAI / seedream 的取向一致（同一个端点靠 model 分流）。

## 命名

即梦这条链路上"能力"与"上游模型"是**两个维度**：
  · **能力**决定走哪条草稿构造路径（文生图 / 图生图 blend / 后编辑四工具）；
  · **上游模型**（如 `high_aes_general_v50`）只在文生图族里有意义。

所以对外用 `<能力>`，并在需要时允许直接写上游模型 key（等价于"文生图 + 该模型"）。

## 🔴 默认能力：**只在无歧义时给默认**

没写 `model` 时：
  · 无输入图 → 只有 `jimeng-t2i` 能接 ⇒ 默认它；
  · 带输入图 → `i2i` / `hd` / `pro-hd` / `outpaint` 四个都能接 ⇒ **明确报错**，
    而不是随便挑一个。挑错等于替调用方做了他没做的决定，而**各家的花费并不相同**
    （实扣：`hd` 0 / `i2i` 0 / `pro-hd` 1 积分；`outpaint` 未对账）。
"""
from __future__ import annotations

import re

from dataclasses import dataclass

from .errors import InvalidParameterError

# ---------------------------------------------------------------------------
# 上游模型 key（用于文生图族；即梦服务端会下发能力表，见 capabilities.py）
# ---------------------------------------------------------------------------

#: 抓包实测用的默认模型（Seedream 5.0 Lite）。
DEFAULT_UPSTREAM_MODEL = "high_aes_general_v50"

#: 允许直接作为 `model` 写的上游 key。**只登记实测过的**：
#: `high_aes_general_v30l:general_v3.0_18b` 实测 `ret=1006` 权益不足，
#: 故不登记（写它会得到"未知模型"而不是一个会失败的模型）。
#: 完整取值仍可由 `GET /v1/models` 从服务端能力表读回（见 capabilities.py）。
#:
#: 🔴 `high_aes_general_v50_flash`（**Seedream 5.0 Flash**）的证据等级与其余
#: 不同，单独说明：它是 **2026-09-23 服务端能力表实读**新增的模型
#: （`is_new_model: true`，`feats` 含 `new_model`/`t2i`/`byte_edit`，
#: `generate_count_options` 1..4，默认 2k，benefit_type
#: `image_basic_v50_flash_15k` / `image_basic_v50_flash_2k`，`amount` 均 1）。
#: ⇒ 登记依据是**上游自己宣告**（"能读的就不许猜"），**不是端到端实跑**：
#: 文档/notes 里必须标明这一点，别把它当"已验证"。
#: 反例警示：v30l 那两条也是表里有的，但实跑 `ret=1006` —— 读得到 ≠ 用得了。
#: 🔴 `jm_image_model_yc_mj82`（**图片美学模型 V8.2**）是**异类**：它不属��
#: Seedream 系，命名风格完全不同（`jm_image_model_*`），但**提交包结构与 t2i
#: 完全同构**（同为 `image_base_component` + `abilities.generate.core_param`），
#: ⇒ **不需要新的提交流程**，登记进白名单即可走现成的 t2i 分支。
#: 证据等级（2026-10-02）：**能力表实读 + 端到端实跑**（用户 UI 抓包 + 本仓探针
#: 四发全通，见 docs/UPSTREAM.md）。
#: ⚠️ **张数不可控**：`generate_count_options=[4]` / `default_generate_count=4`
#: ⇒ 恒出 4 张。这是服务端声明，不是我们的实现选择。
UPSTREAM_MODEL_KEYS: tuple[str, ...] = (
    "jm_image_model_yc_mj82",
    "high_aes_general_v50_flash",
    "high_aes_general_v50",
    "high_aes_general_v50p_large",
    "high_aes_general_v43",
    "high_aes_general_v42",
    "high_aes_general_v41",
    "high_aes_general_v40l",
    "high_aes_general_v40",
)


def _norm_model_name(raw: str) -> str:
    """把用户写的模型名规范化成查表键。

    规则：小写 → 把**空格 / 下划线 / 点 / 中点 `·`** 一律换成 `-` → 折叠连续
    `-` → 去掉首尾 `-`。⇒ `Seedream 5.0 Flash` / `seedream 5.0 flash` /
    `SEEDREAM_5.0_FLASH` / `seedream-5-0-flash` 全部归一到 `seedream-5-0-flash`。

    ⚠️ 规范化**只用于别名查表**：上游 key 分支仍是**逐字精确匹配**
    （`HIGH_AES_GENERAL_V50` 依旧被判未知模型，见 docs/INTERFACE.md 的三条纪律）。
    两套键不会互相污染 —— 上游 key 只含 `_` 和 `:`，规范化后不再等于任何一个 key。
    """
    s = (raw or "").strip().lower()
    #: ⚠️ `·`(U+00B7) 与 `・`(U+30FB) 是**两个**不同字符 —— 面板分类名用的是后者，
    #: 少写一个就会出现"看着一模一样、查表却查不中"的静默失效。
    for ch in (" ", "_", ".", "·", "・", "．", "\u00a0"):
        s = s.replace(ch, "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


#: 上游 key → **火山方舟模型名**（2026-10-02 用户口径"图片族也对齐方舟命名"）。
#:
#: 背景：视频族 2026-09-24 起已全面改用方舟模型名对外（见 `ARK_PUBLIC_MODEL_ID`），
#: 图片族这次跟上。调用方手上拿到的往往就是方舟的模型名（从方舟控制台/文档抄来的），
#: 让它**原样可传**，省掉"方舟名 → 即梦面板名 → 上游 key"的人工换算。
#:
#: 🔴 **只登记调用方点名要的那几个**，不做前缀通配：
#: 方舟图片模型名形如 `doubao-seedream-<版本>-<档位>-<日期>`，而即梦侧只有
#: **已实测过**的档位能跑（见 `UPSTREAM_MODEL_KEYS`）。**没登记的**方舟名
#: 仍按**占位名**处理（走默认 Lite）—— 那是"刻意不认"，不是漏认：
#: 认一个没对账过的名字 = 替调用方悄悄切到另一条计费链路（Pro 是 8 积分/张）。
#: 缺哪个档位要显式加进本表，并在 `UPSTREAM_MODEL_CREDITS` 补实测价。
#:
#: 2026-10-02 用户点名补登Pro 与 Lite 两条（此前只有 Flash）：
#: ⚠️ `doubao-seedream-5-0-pro-260628` 此前是"**刻意不认**"的反例
#: （见 `test_unregistered_ark_names_stay_placeholders_...` 的原注释）——
#: 现在由用户明确指定为 `high_aes_general_v50p_large` 的方舟名，
#: 该门禁的样本已相应换成**仍未登记**的其它名字（保护机制本身保留）。
#:
#: 门禁：`tests/test_models.py::test_ark_image_names_are_registered_and_wired_...`
#: 逐条断言"键集合 ⊆ 已登记 key"+"逐条能 resolve"+"已并进别名表"。
UPSTREAM_ARK_NAMES: dict[str, str] = {
    "high_aes_general_v50p_large": "doubao-seedream-5-0-pro-260628",
    "high_aes_general_v50_flash": "doubao-seedream-5-0-flash-260915",
    "high_aes_general_v50": "doubao-seedream-5-0-260128",
}


#: **web 面板名 → 上游模型 key**。数据源 = 服务端能力表的 `model_name` /
#: `generation_category_name`（2026-09-23 实读 10 条），**不是猜的**；
#: 每种写法都给三条：完整名、版本短名、面板分类名。查表前先过 `_norm_model_name`。
#:
#: 🔴 三条红线：
#: 1. **只在 t2i 族内换上游模型，绝不跨能力** —— 别名一旦改指到别的能力，
#:    就是把既有调用方静默换到另一条链路（计费与手感都变）。
#: 2. **只映射已登记进 `UPSTREAM_MODEL_KEYS` 的模型**。未登记的（3.0/3.1）
#:    进 `UNSUPPORTED_WEB_MODELS`：点它的名要给**明确理由**，不许静默退化成默认模型。
#: 3. **认得的面板名必须判在"占位名"之前**（`resolve` 里那条 `not is_placeholder`
#:    的判断被显式放宽）—— 否则 `Seedream 5.0 Flash` 会被 `PLACEHOLDER_PREFIXES`
#:    的 `seedream` 前缀吃掉，变成"调用方点了 Flash、拿到 Lite"。
UPSTREAM_MODEL_ALIASES: dict[str, str] = {
    # ---- 图片美学模型 V8.2（mj 系列，命名风格与 Seedream 完全不同）----
    # 面板上显示的是 "图片美学模型 V8.2"（能力表 `model_name`，逐字照抄），
    "图片美学模型-v8-2": "jm_image_model_yc_mj82",
    "图片美学模型-8-2": "jm_image_model_yc_mj82",
    # 🔴 归一化后的键是 `jm-8-2`（**m 在前 j 在后**）—— 别名键必须与
    # `_norm_model_name` 的输出**逐字**一致，写成 `mj-8-2` 会永远查不中
    # （症状：手动看着"有这条别名"，实际 resolve 报未知模型）。
    "jm-8-2": "jm_image_model_yc_mj82",
    # 🔴 `mj-v8.2` 归一后是 `mj-v8-2`（点→连字符，**v 保留**），
    # 与上面 `mj-8-2` 是**两个不同的键** —— 只登记后者会漏掉用户原话里的写法。
    "mj-v8-2": "jm_image_model_yc_mj82",
    "mj-v82": "jm_image_model_yc_mj82",
    # 归一化会小写 ⇒ 大写写法命中的是同一个键；这里显式登记 `jm82` 简写
    "mj82": "jm_image_model_yc_mj82",
    # ---- Seedream 5.0 家族 ----
    "seedream-5-0-pro": "high_aes_general_v50p_large",
    "seedream-5-pro": "high_aes_general_v50p_large",
    "5-0-pro": "high_aes_general_v50p_large",
    "图片-5-0-pro": "high_aes_general_v50p_large",       # category: 图片 5.0 Pro
    "seedream-5-0-flash": "high_aes_general_v50_flash",
    "seedream-5-flash": "high_aes_general_v50_flash",
    "5-0-flash": "high_aes_general_v50_flash",
    "seedream-5-0-lite": "high_aes_general_v50",
    "seedream-5-lite": "high_aes_general_v50",
    "5-0-lite": "high_aes_general_v50",                  # category: 5.0 Lite
    # ---- 4.x 家族 ----
    "seedream-4-7": "high_aes_general_v43",
    "4-7": "high_aes_general_v43",
    "图片-4-7": "high_aes_general_v43",
    "seedream-4-6": "high_aes_general_v42",
    "4-6": "high_aes_general_v42",
    "图片-4-6": "high_aes_general_v42",
    "seedream-4-5": "high_aes_general_v40l",
    "4-5": "high_aes_general_v40l",
    "图片-4-5": "high_aes_general_v40l",
    "seedream-4-1": "high_aes_general_v41",
    "4-1": "high_aes_general_v41",
    "图片-4-1": "high_aes_general_v41",
    "seedream-4-0": "high_aes_general_v40",
    "4-0": "high_aes_general_v40",
    "图片-4-0": "high_aes_general_v40",
}

#: 🔴 **方舟模型名并进别名表**（2026-10-02）—— 这一步是让方舟名能"穿过"
#: `PLACEHOLDER_PREFIXES` 里 `doubao-seedream` 前缀的**唯一**机关：
#: `resolve` 判别名时用的是 `norm in UPSTREAM_MODEL_ALIASES`，
#: 它在 `is_placeholder()` **之前**生效（见下面 `resolve` 的注释）。
#: 不并进来 = 方舟名被当占位名静默降级成默认 Lite（"点了 Flash 拿到 Lite"）。
#:
#: 归一后写入（`_norm_model_name`），所以查表键是小写连字符形态。
#: 这里**不写死字面量**，改为从 `UPSTREAM_ARK_NAMES` 反向派生：
#: 两张表一旦漂移（加了 ARK 名忘了加别名，或反之），门禁立刻红。
UPSTREAM_MODEL_ALIASES.update({
    _norm_model_name(ark_name): upstream_key
    for upstream_key, ark_name in UPSTREAM_ARK_NAMES.items()
})

#: 面板上有、**本服务未登记**的模型名 → 未登记的理由（key 保留完整以便追溯）。
#: 依据：`high_aes_general_v30l*` 系列实测 `ret=1006` 权益不足
#: （见上方 `UPSTREAM_MODEL_KEYS` 注释与 `docs/UPSTREAM.md` §11）。
UNSUPPORTED_WEB_MODELS: dict[str, str] = {
    "seedream-3-1": "high_aes_general_v30l_art_fangzhou:general_v3.0_18b",
    "3-1": "high_aes_general_v30l_art_fangzhou:general_v3.0_18b",
    "图片-3-1": "high_aes_general_v30l_art_fangzhou:general_v3.0_18b",
    "seedream-3-0": "high_aes_general_v30l:general_v3.0_18b",
    "3-0": "high_aes_general_v30l:general_v3.0_18b",
    "图片-3-0": "high_aes_general_v30l:general_v3.0_18b",
}

#: 上游 key → **面板正式名**（服务端能力表的 `model_name`，逐字照抄）。
#: 单一真相：`GET /v1/models` 的 `upstream_models` 由它渲染，
#: 门禁断言"已登记 key 集合 == 本表键集合"，防止两张表漂移。
UPSTREAM_WEB_NAMES: dict[str, str] = {
    "jm_image_model_yc_mj82": "图片美学模型 V8.2",  # 逐字照抄能力表 model_name
    "high_aes_general_v50p_large": "Seedream 5.0 Pro",
    "high_aes_general_v50_flash": "Seedream 5.0 Flash",
    "high_aes_general_v50": "Seedream 5.0 Lite",
    "high_aes_general_v43": "Seedream 4.7",
    "high_aes_general_v42": "Seedream 4.6",
    "high_aes_general_v40l": "Seedream 4.5",
    "high_aes_general_v41": "Seedream 4.1",
    "high_aes_general_v40": "Seedream 4.0",
}

#: 上游模型 → **实测**单价（积分/张）。`None` = 未实测（**不等于免费**）。
#:
#: 🔴 为什么必须与能力级 `Capability.credits_measured` **分开**：
#: `jimeng-t2i` 的能力级实测价是 **0** —— 那是 **Lite 口径**；而同一个 t2i 端点
#: 换模型就换价：Flash 实测 **3**、Pro 实测 **8**。把能力级的值抄给每个上游模型，
#: 等于对 Flash 报"免费"（那是会让调用方算错成本的那种错）。
UPSTREAM_MODEL_CREDITS: dict[str, int | None] = {
    # 2026-10-02 实测：4 张实扣 **20**（=5/张）；提交包预扣 amount=1 只是档位标记
    "jm_image_model_yc_mj82": 5,
    "high_aes_general_v50_flash": 3,      # 2026-09-23 实跑（2k/1 张，三证吻合）
    # 🔴 2026-10-02 更正：Lite **不是"免费"**，是**按分辨率档**收的：
    # · **2k 实测 0**（2026-09-20 余额差分 + 消耗记录，长期未变）；
    # · **4k 实测 4/张**（`submit_id=07144215…`，余额 5526→5522 +
    #   消耗记录 `图片生成 amount=4` 两证吻合）。
    # 服务端也这么声明：`blend` 段有**两条独立计费项**
    # （2k = `image_basic_v5_2k` / 4k = `image_basic_v5_4k`，各 amount=1）。
    # ⚠️ **这里只登记 2k 口径**（默认档、最常用、也是"免费"口碑的来源），
    # `credits_measured` 是**单值**字段、表达不了"按档定价"——
    # 调用方若拿它当"这个模型恒免费"就会在 4k 上踩坑。
    # 4k 的真实单价见 docs/UPSTREAM.md 与 `service.create` 的分档留痕。
    "high_aes_general_v50": 0,
    "high_aes_general_v50p_large": 8,     # 2026-09-20 实测（三尺寸同价）
    "high_aes_general_v43": None,
    "high_aes_general_v42": None,
    "high_aes_general_v40l": None,
    "high_aes_general_v41": None,
    "high_aes_general_v40": None,
}

#: 文生图族才吃上游模型 —— 目录里只给这一个能力挂 `upstream_models`。
_UPSTREAM_FAMILY = "t2i"

#: 🔴 **提交路径完全相同的 t2v 变体**（彼此只差 `video_model`）。
#:
#: 新增 t2v 变体时**必须加进这个集合**：否则它会掉进 `Service._submit` 的
#: 后编辑族分支（`else`），而那条分支要求 `jimeng_tool` —— 视频能力没有它，
#: 于是建任务当场炸。2026-09-23 实测：`jimeng-t2v-fast` / `jimeng-t2v-pro`
#: **正是这么坏的**（注册了、受理也通，一派发就 AssertionError，
#: 还被误报成"上游不可用 可重试"）—— 这就是"视频从未端到端跑过"的真因。
#: 现在由 `tests/test_video.py::test_every_capability_has_a_submit_route` 钉住。
T2V_VARIANTS: frozenset[str] = frozenset({"t2v", "t2v-fast", "t2v-pro", "t2v-2.5-draft"})


# ---------------------------------------------------------------------------
# 能力
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    key: str                       # "jimeng:hds" 形态的稳定 id（对外用 `jimeng-hd`）
    name: str                      # "t2i"
    title: str                     # 中文能力名
    accepts_image: bool = True
    image_required: bool = True
    prompt_required: bool = False
    #: 即梦后编辑工具名（对应 upstream/jimeng/client.py::POST_EDIT_TOOLS 的键）；
    #: `blend` 是图生图的特例（它不是 `POST_EDIT_TOOLS` 的成员）。
    jimeng_tool: str | None = None
    #: 本部署**已实测**的积分单价（张）。None = 未测，不报数。
        #: ⚠️ **只有实测过的才填。** 2026-09-20 校准：原先那批数（t2i 44 / i2i 59 /
        #: hd 9 / pro-hd 91 / outpaint 28）全部取自上游回执的 `forecast_generate_cost`，
        #: 而按 `submit_id` 对账（`/commerce/v1/benefits/user_credit_history`）实测**高估 4~9 倍**
        #: （i2i 报 55 / 实扣 **12**）。
        #: 🔴 **`0` 是「实测不扣分」，不是占位符**：t2i / hd 在 Seedream 5.0 Lite 上
        #: **没有产生任何消耗记录 ⇒ 一分没扣**（余额读数也一直没变）。
        #: 未实测的仍置 None —— 宁可「不报数」，也不报一个会让调用方算错成本的值。
    credits_measured: int | None = None
    #: 本次请求最多接受几张输入图（垫图）。
    #:
    #: 🔴 默认 **1**，且**超出必须响亮 400** —— 绝不许静默丢掉多余的图。
    #: 原先的行为是：`image` 收下 N 个 URL、全部下载，然后只用第 0 张，
    #: **既不报错也不留痕**；调用方以为用了 4 张、实际只用 1 张。
    #: 现在能力没声明支持几张，就只允许 1 张，多给的当场说清楚。
    max_images: int = 1
    #: 媒体类型：`image`（图片族）| `video`（视频族）。
    #: 它决定 resolve() 的**候选池**：图片端点只在 image 池里推导默认，
    #: 视频端点只在 video 池里 —— 两条池子互不污染，
    #: 否则"不写 model"就会在 t2i / t2v 之间产生歧义。
    media: str = "image"
    #: 🔴 True = 只能引用**已有产物**（source_task_id），不能凭空发起。
    #: 默认推导必须排除它们（否则"不写 model"就会在 t2i/detail-fix 之间
    #: 产生歧义 —— 那会让既有图片调用方突然 400）。
    needs_source_ref: bool = False
    #: 视频族专用：上游 `model_req_key`/`root_model`（计费档位按它查表）。
    video_model: str | None = None
    notes: str = ""

    @property
    def api_id(self) -> str:
        """对外 `model` 取值形态：`jimeng-t2i`。"""
        return f"jimeng-{self.name}"


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="jimeng:t2i", name="t2i", title="文生图",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=0,
        notes="上游模型 high_aes_general_v50（Seedream 5.0 Lite）；异步建任务→轮询，"
              "⚠️ **能力级的 0 只对 2k 档成立**（4k 是独立计费项 image_basic_v5_4k，"
              "**实测 4/张**，submit_id 07144215）⇒ 传 4k 尺寸会在 degradations "
              "里留痕提示额外扣费；"
              "**建任务即计费**。"
              "实测一次出图端到端 17.9-19s（1 张 2048×2048）。"
              "本能力也接受**直接写上游模型 key 或 web 面板名**来换模型，"
              "已登记 8 个（每个的**实测单价见 catalog 的 `upstream_models`**——"
              "能力级那个 0 只是 Lite 口径）：`high_aes_general_v50_flash`"
              "（Seedream 5.0 Flash，3/张，2026-09-23 实跑）、"
              "`high_aes_general_v50`（5.0 Lite，0，默认，"
              "**方舟对应名 `doubao-seedream-5-0-flash-260915` 可原样传**），"
              "`high_aes_general_v50p_large`（5.0 Pro，8/张）、"
              "4.7 / 4.6 / 4.5 / 4.1 / 4.0（未测）、"
              "`jm_image_model_yc_mj82`（**图片美学模型 V8.2**，5/张）。"
              "⚠️ **mj82 的张数不可控**（服务端只声明 [4]）⇒ 恒出 4 张、"
              "按 4 张计费，请求 n 会被吸附并在 degradations 里留痕；"
              "带输入图时同一个 model名会走 i2i（blend），**t2i/i2i 都已真跑验证**。",
    ),
    Capability(
        key="jimeng:i2i", name="i2i", title="图生图（blend）",
        accepts_image=True, image_required=True, prompt_required=True,
        #: 🔴 2026-09-20 用户（账号所有者）确认：**i2i 实际也是免费的**。
        #: 我先前按一条消耗记录（09-20 11:05，`amount=12`，`submit_id` 对得上）
        #: 把它标成 12 —— **那是错的**，已更正为 0。
        #: ⚠️ 证据确有冲突（那条记录真实存在），可能是**免费期开始前**的调用、
        #: 或属于别的计费口径。**以账号所有者的口径为准**，但冲突本身记在这里，
        #: 免得后人再看到那条记录又改回去。
        #:
        #: 🔴 **定价取决于账号的会员状态，不是模型本身**（2026-10-03 坐实）：
        #: 同一个 `high_aes_general_v50` + blend：
        #:   旧号（**会员**）→ i2i 实扣 **0**
        #:   新号（非会员）→ i2i 实扣 **3**/张（`ae992ac0…`垫图×2、
        #:     `27b372e7…` 垫图 ×4，两笔 `amount=3`，余额 20→17→14→11）
        #: ⇒ 本字段记的是**会员号**的实测值（0）。非会员号跑同一请求会扣 3，
        #: **这不是本服务的计费 bug**，别看到账单对不上就改这里。
        #: （**垫图张数不影响定价**：×2 与 ×4 同为 3。）
        #:
        #: ⚠️ 上一轮（2026-10-02）"三笔i2i 零记录"之所以** inconclusive**：
        #: 那轮在旧号上做，而账号所有者**同时在网页端并发使用同一账号**，
        #: 我方消耗记录可能**根本查不到** —— **"零记录" ≠ "免费"**。
        #: 要在**独占账号**上重测才能得出定价结论。
        jimeng_tool="blend", credits_measured=0,
        #: blend 是**唯一**原生用「列表」承载输入图的能力：
        #: 草稿里是 `abilities.blend.ability_list[0].image_uri_list`（列表）
        #: 与 `image_list`（列表）—— 所以多张垫图就是往这两个列表里多放元素，
        #: 不需要额外的组件串链（后编辑那三族才需要）。
        #: ⚠️ 4 是**保守值**：上游声明的比它宽 —— `get_common_config` 的
        #: `input_image_limit` = `[{'max_image_num': 10, 'ability_name': 'byte_edit'}]`
        #: （`byte_edit` 就是 blend）⇒ 5.0 Pro 允许 **10** 张垫图。
        #: 但那个字段我们**读不出来**（`capabilities.py` 用 `_as_int()` 读数组 ⇒ 恒 None），
        #: 所以先钉 4。要放开得先把解析改成按数组取 `max_image_num`，
        #: 并同步抬高全局 `MAX_INPUT_IMAGES`。详见 `docs/UPSTREAM.md` §11。
        max_images=4,
        notes="输入图由本服务自动上传成即梦资产 uri；**必须给 prompt**"
              "（描述要怎么改）——这是它与后编辑三工具的关键区别。"
              "支持**多张垫图**（最多 4 张，超出会明确报错）。"
              "支持**指定张数** `n`（走 `abilities.gen_option.gen_count`）。"
              "⚠️ 上游**预报**积分与张数不成正比：n=1 报 59 / n=4 报 55 —— 那是 "
              "`forecast`（高估口径），**实扣为 0**（免费，账号所有者确认；"
              "2026-10-02 三笔实跑 submit_id 零出账复核）⇒ "
              "别把预报当账单，也别按「张数 × 单价」估算成本。",
    ),
    Capability(
        key="jimeng:hd", name="hd", title="超清（SuperDefinition）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="normal_hd", credits_measured=0,
        notes="实测 2048×2048 → **4096×4096**；**实扣 0（免费）**"
              "（余额差分多次未见变化）。回执 forecast 报 9 —— **预报不作数**。"
              "同一族里最便宜的，且出图最大 —— 别按名字选工具。",
    ),
    Capability(
        key="jimeng:pro-hd", name="pro-hd", title="智能超清（ProHD）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="pro_hd", credits_measured=1,
        notes="实测 2048×2048 → **2160×2160**；**实扣 1 积分**"
              "（2026-09-22 消耗记录 `智能超清2.0-2k amount=1`，submit_id 归属已核实）。"
              "回执 forecast 报 91 —— **高估 91 倍**，别拿它算账。"
              "**又贵又小**（超清 4096 反而免费）。",
    ),
    Capability(
        key="jimeng:outpaint", name="outpaint", title="扩图（OutPaint）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="outpaint", credits_measured=1,
        notes="**✅ 2026-10-02 首次对账：实扣 1 积分**（submit_id `0e12b55d`，"
              "余额差分 + 消耗记录 `图片生成 amount=1` + 任务表 submit_id 三证吻合）。"
              "回执 forecast 报 **35** ⇒ **高估 35 倍**，别拿它算账。"
              "⚠️ **张数与旧记载不符**：旧 notes 写「一次出 4 张」，"
              "本次实跑 `n=1` **只交付 1 张**（200/success，43.0s）。"
              "尺寸仍是 **4000×4000**（下载 PIL 实测，6.07MB）—— 尺寸没变、"
              "张数变了。「张数由上游决定、与 n 无关」这个说法**尚未二次验证**"
              "（只跑过 n=1 这一档），要下结论需再跑 n=2/n=4 对照。",
    ),
    Capability(
        key="jimeng:t2v", name="t2v", title="文生视频（Seedance）",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=24, media="video",
        video_model="dreamina_seedance_40_mini",
        notes="上游模型 dreamina_seedance_40_mini（网页端 Seedance 4.0 Mini，t2v）。"
              "**✅ 2026-09-20 真跑验证**（114s 出片 1280×720，实扣 24；"
              "回执 forecast 166 高估 ~7 倍）。"
              "已实抓档位 720p×4s/5s、4:3/16:9（计费 amount=输出秒+Σ输入视频秒）。"
              "视频草稿没有张数字段（抓包无 gen_option）⇒ 一次一条。"
              "⚠️ 查询侧复用图片同一套 get_history_by_ids 轮询（已实证）。"
              "⚠️ 服务端 get_common_config 只下发图片模型表，视频模型清单"
              "按场景单独下发 —— 新模型要靠补抓提交包登记（已补 vision/pro 两档）。",
    ),
    Capability(
        key="jimeng:t2v-fast", name="t2v-fast",
        title="文生视频·Fast（Seedance 4.0 Vision）",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=30, media="video",
        video_model="dreamina_seedance_40_vision",
        notes="上游模型 dreamina_seedance_40_vision（网页端 Seedance 4.0 Vision，"
              "2026-09-20 晚 UI 抓包）。计费 benefit_type="
              "**dreamina_seedance_20_fast_5s**（前缀带 dreamina_，照抄）。"
              "**✅ 2026-09-23 端到端实跑验证**：720p × 5s × **16:9**（比例是"
              "**提交侧亲传**）→ 93s 出片 **1280×720**（正是 16:9），"
              "**实扣 30 积分**（余额 5962→5932 差分 + 消耗记录 "
              "`视频生成720P 5秒 amount=30` + `submit_id` 三证吻合；回执 "
              "forecast 156 高估 5.2 倍）。"
              "⚠️ UI 标的 `useSeedanceFast5sFreeTrial: true` **没有生效** —— "
              "5s 照样扣 30。",
    ),
    Capability(
        key="jimeng:t2v-pro", name="t2v-pro",
        title="文生视频·Pro（Seedance 4.0 Pro Vision）",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=70, media="video",
        video_model="dreamina_seedance_40_pro_vision",
        notes="上游模型 dreamina_seedance_40_pro_vision（网页端 "
              "Seedance 4.0 Pro Vision，2026-09-20 晚 UI 抓包）。计费 "
              "benefit_type=**seedance_20_pro_720p_output**（无 _5s 尾巴，照抄）。"
              "**✅ 2026-09-23 端到端实跑验证**：720p × 5s × **16:9** → "
              "167s 出片，**实扣 70 积分**（余额差分 + 消耗记录 "
              "`视频生成 amount=70` + `submit_id` 三证吻合；回执 forecast 453 "
              "高估 6.5 倍）。比 t2v-fast（30）贵 **2.3 倍**。",
    ),
    Capability(
        key="jimeng:t2v-2.5-draft", name="t2v-2.5-draft",
        title="文生视频·2.5 样片（Seedance 2.5 Draft 480P）",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=45, media="video",
        video_model="dreamina_seedance_45_pro_draft",
        notes="上游模型 dreamina_seedance_45_pro_draft（网页端 "
              "『即梦 Seedance 2.5 (样片模式)』，2026-09-24 UI 实读）。"
              "**✅ 2026-09-24 端到端真跑 + 三证对账**：480p × 5s × 4:3 → "
              "**实扣 45 积分**（余额差分 + 消耗记录 `Seedance2.5` amount=45 "
              "+ submit_id 三证吻合；提交包预扣 amount=5 只是占位）。"
              "提交包与 t2v 同构（aigc_draft/generate），差异："
              "min_version 3.3.28 / min_features AIGC_Video_Seedance25ResultAction / "
              "is_draft_mode=true / 计费 benefit_type="
              "**seedance_25_draft_480p_no_input_video_output**（照抄）。"
              "样片 = 先出 480P 低清版，网页端确认后升级高清正片（本服务只出样片）。"
              "⚠️ `dreamina_seedance_45_pro`（2.5 正式版 480p/720p/1080p）"
              "**刻意未登记**：benefit_type 无提交包依据，按纪律拒绝猜。"
              "UI 命名对照：2.0 mini=40_mini / 2.0 Fast VIP=40_vision / "
              "2.0 VIP=40_pro_vision（9-20 的 4.0 Vision 改名，key 未变）。",
    ),
    Capability(
        key="jimeng:vfi", name="vfi", title="视频补帧（插帧 insert_frame）",
        accepts_image=False, image_required=False, prompt_required=False,
        credits_measured=None, media="video",
        notes="上游模型同 seedance（根模型 dreamina_seedance_40_mini），"
              "scene=insert_frame：把**本服务已生成的视频**插帧到 60fps。"
              "**必须给 source_task_id**（指向本服务一个成功的视频任务）—— "
              "补帧要引用源视频的 vid / item_id / origin_history_id，"
              "只有走本服务产物链才拿得到；target_fps 默认 60。"
              "提交包计费字段 amount=0（UI 口径免费，**未对账**）。"
              "提交侧已按 2026-09-20 实抓适配；**端到端实跑未验证**，"
              "结果解析同 t2v（尽力而为）。draft min_version 3.1.0，"
              "父组件为源任务草稿原样重放（实抓里连组件 id 都没变）。"
              "Ark 方舟契约没有补帧概念 —— 且 2026-09-24 视频端点收敛到"
              "方舟门面后，本能力暂无 HTTP 入口（内部链路保留，待补帧入口设计）。",
    ),
    Capability(
        key="jimeng:audit", name="audit", title="素材预审（只判能不能用，不出图）",
        accepts_image=True, image_required=True, prompt_required=False,
        credits_measured=0, max_images=4,
        notes="🔴 **唯一一个成功时没有产物的能力**（2026-10-02 用户口径）。"
              "它只回答『这张垫图/ 参考图能不能用』，**不生成任何图**。"
              "走上游 `execute_generate_audit` 同步判定："
              "`audit_decision` **1=通过 / 2=拒绝**（拒绝带 `reason_detail`，"
              "实测有『可能包含低俗内容』）。"
              "⚠️ **`ret=0` 不代表通过** —— 拒绝时也回 `ret=0/errmsg=success`，"
              "只看 `ret` 会全漏判。"
              "**成功时 `data` 为空数组**，判定结论在 `degradations` 里"
              "（如『素材预审通过（decision=1）』）—— 这是本服务**唯一**"
              "豁免『终态成功却零产物 = 失败』的能力，因为预审通过时"
              "本来就没有产物。"
              "💡 用途：**花钱前先验素材**。生成链路已在提交前自动预审"
              "（fail-open），本能力是把它**显式暴露**给调用方"
              "（批量筛素材、或只想知道某图能否用）。",
    ),
    Capability(
        key="jimeng:detail-fix", name="detail-fix", title="细节修复（SuperResolution）",
        accepts_image=False, image_required=False, prompt_required=False,
        jimeng_tool="detail", credits_measured=0, needs_source_ref=True,
        notes="**只支持『引用形态』**：source_task_id 指向本服务一个已成功的"
              "**图片**任务（引用其 item_id+origin_history_id，不带 origin_image）。"
              "9-19 两次「单组件+origin_image」提交 generate_failed；"
              "9-20 路 A 探针证实死因是 origin_image：引用形态**一次真跑成功**"
              "（status=50，出图与源图同尺寸，UPSTREAM.md §9.1——勿回退）。"
              "**免费**（UI 标价 + 实测零出账两证吻合）。"
              "🔴 本地图/外部图的 origin_image 形态：同日 4 样本全败"
              "（纯单组件 / +自造父组件重放），且失败零扣费——稳定不支持。"
              "✅ **本地图正解（真跑全通，两步均免费）**：先 i2i（blend）"
              "生成产物，再 source_task_id 引用修复。"
              "直接贴 image 字段会被拒绝——origin_image 形态实测必失败。",
    ),
    Capability(
        key="jimeng:omni-video", name="omni-video", title="全能参考视频（图/视频/音频）",
        accepts_image=True, image_required=False, prompt_required=True,
        credits_measured=None, media="video", max_images=4,
        notes="上游模型同 seedance，unified_edit_input（全能参考）："
              "**混合参考素材** —— 图片走 ImageX 上传（与图生图同链路）、"
              "视频/音频走 VOD 上传（ApplyUploadInner/CommitUploadInner → vid）。"
              "**计费口径（实抓解出）**：amount = 输出秒数 + Σ输入视频秒数"
              "（4s⇒4；5s+10.35s 输入⇒15.35；音频不计）。"
              "输入视频时长由 VOD Duration 探测（✅真实验证）。"
              "已实抓档位 720p×4s/5s。"
              "提交侧已按实抓逐字段适配；Ark 门面的 image_url/video_url/"
              "audio_url 角色翻译到本能力。",
    ),
)

REGISTRY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}

#: 对外 `model` → 能力。`jimeng-t2i` / `jimeng-i2i` / `jimeng-hd` …
_BY_API_ID: dict[str, Capability] = {c.api_id: c for c in CAPABILITIES}
#: 裸能力名 → 能力（本服务只有一个上游，故裸名无歧义）
_BY_NAME: dict[str, Capability] = {c.name: c for c in CAPABILITIES}

#: 中文别名。**只给本服务确实拥有的能力加**，且刻意不做"改指"：
#: 别名一旦指向另一个能力，就会把既有调用方静默换到另一条链路（换个链路 = 换计费与手感）。
ALIASES: dict[str, str] = {
    "即梦": "jimeng-t2i",
    "jimeng": "jimeng-t2i",
    "文生图": "jimeng-t2i",
    "图生图": "jimeng-i2i",
    "超清": "jimeng-hd",
    "智能超清": "jimeng-pro-hd",
    "扩图": "jimeng-outpaint",
    "文生视频": "jimeng-t2v",
    "细节修复": "jimeng-detail-fix",
    "视频": "jimeng-t2v",
    # 常见英文写法
    "text2image": "jimeng-t2i",
    "image2image": "jimeng-i2i",
    "upscale": "jimeng-hd",
    "text2video": "jimeng-t2v",
    "t2v": "jimeng-t2v",
    "seedance": "jimeng-t2v",
    "t2v-fast": "jimeng-t2v-fast",
    "t2v-pro": "jimeng-t2v-pro",
    "补帧": "jimeng-vfi",
    "插帧": "jimeng-vfi",
    "vfi": "jimeng-vfi",
}

#: 第三方 SDK 常硬编码的占位模型名 —— 它们**不代表**调用意图。
#: 见到它们等价于"没写 model"，走默认能力推导（而不是报"未知模型"）。
PLACEHOLDER_MODELS = {
    "auto", "", "default", "none",
    "dall-e", "dall-e-2", "dall-e-3",
    "gpt-image-1", "gpt-image-1-mini", "gpt-image-2",
    "stable-diffusion", "sd", "sd3", "flux", "flux-1",
    "image-1", "openai",
}
PLACEHOLDER_PREFIXES = ("dall-e", "gpt-image", "sd-", "flux", "seedream", "doubao-seedream")


def is_placeholder(model: str | None) -> bool:
    low = (model or "").strip().lower()
    if low in PLACEHOLDER_MODELS:
        return True
    return any(low.startswith(p) for p in PLACEHOLDER_PREFIXES)


#: 🔴 **最近一次 `resolve()` 走的兜底路径**：`{原始写法: 实际生效的模型}`。
#:
#: 2026-10-02 实测的缺陷：`seedream-9-9-ultra` 这种**不存在的型号**，
#: 因为带 `seedream` 前缀被判成占位名 ⇒ 走默认推导 ⇒ **真出图**（200，
#: submit_id 9154eaa2，23s）。**兜底到免费模型这件事本身没问题**（用户口径：
#: 不存在的型号用免费的兜底出图可以接受），问题在于它是**静默**的 ——
#: 调用方拿到的是它没点的模型，响应体里却没有任何痕迹。
#:
#: ⇒ 修法不是改成 400，而是**留痕**：`Service` 读 `take_fallback_note()`
#: 写进 `degradations`，让"降级"在响应体里可见。
#:
#: 为什么用模块级而不是改返回值：`resolve()` 的返回类型 `(Capability, str|None)`
#: 被 tests/e2e/Service/Ark 门面多处消费，改成三元素会波及全链路；用一个
#: 显式「取走即清」的函数保持等价信息量，且**调用方不取也不会串味**
#: （每次 `resolve` 成功路径都会重写它）。
_LAST_FALLBACK: dict[str, str] = {}


def note_fallback(requested: str, effective: str) -> None:
    """登记一次「占位名 ⇒ 兜底模型」。由 `resolve` 内部调用。"""
    _LAST_FALLBACK.clear()
    _LAST_FALLBACK[requested] = effective


def take_fallback_note() -> str | None:
    """取走并清空最近一次兜底记录（取走即清，避免跨请求串味）。"""
    if not _LAST_FALLBACK:
        return None
    requested, effective = next(iter(_LAST_FALLBACK.items()))
    _LAST_FALLBACK.clear()
    return (f"model={requested!r} 不是本服务已登记的型号（也不在面板名里）"
            f"，已按占位名兜底到 {effective!r}（免费档）。"
            f"要指定具体型号请用 GET /v1/models 里的名字。")


def _hint() -> str:
    ids = ", ".join(c.api_id for c in CAPABILITIES)
    return (f"model 取值：{ids}；"
            f"也可只写能力名（t2i / i2i / hd / pro-hd / outpaint / t2v / "
            f"t2v-fast / t2v-pro / vfi / detail-fix）、"
            f"中文别名、web 面板名（如 `Seedream 5.0 Flash` / `5.0 Lite` / `4.7`），"
            f"或直接写上游模型 key（如 {DEFAULT_UPSTREAM_MODEL}）。"
            f"完整清单见 GET /v1/models")


#: 🔴 **model 名可用后缀指定分辨率档**（2026-10-02 用户口径，方案 B）。
#:
#: 动机：`credits_measured` / "某模型免费"这类说法**只对某个档成立**
#: （Lite 2k 免费 / 4k 收费），调用方需要一种**不依赖 `size`** 就能指名档位的写法。
#:
#: 🔴 **只认精确后缀 `-1k` / `-1.5k` / `-2k` / `-4k`**，绝不做"尾数字推断"：
#: 方舟模型名**本来就以数字结尾**（`doubao-seedream-5-0-pro-260628`、
#: `...-flash-260915`、`...-260128`）⇒ "取末段数字当档位"会把它们全判错。
#: 精确后缀 + 末尾锚定（`$`）⇒ 上面这些**一律不匹配**（已验证）。
#: 也不做裸 `4k`（无连字符）：`4-7` / `4-0` 这类 4.x 面板名会撞。
#:
#: 档位取值**不是全集**，只有服务端 `resolution_map` 实际声明的才算 ——
#: Flash（1.5k/2k）与 mj82（1k/2k）**没有 4k**，写 `-4k` 会明确 400，
#: **绝不静默退回 2k**（否则"以为买了 4k、实际拿 2k 并按 2k 计费"）。
#: ⚠️ 匹配**原始串**（不预先归一）：归一会把 `high_aes_general_v50` 变成
#: `high-aes-general-v50`，而上游 key 分支是**逐字精确匹配** ⇒ 归一后的 base
#: 会查不中（实测报"未知 model"）。所以只切**末尾**那一段，前段保持原样。
_TIER_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)[\s_-]+(?P<tier>1\.5k|1k|2k|4k)$", re.IGNORECASE)

#: 后缀 → 服务端 `resolution_map` 里的键（大小写已归一）。
_TIER_CANON = {"1k": "1k", "1.5k": "1.5k", "2k": "2k", "4k": "4k"}


def split_tier_suffix(model: str | None) -> tuple[str | None, str | None]:
    """拆出 `model` 尾部的分辨率档后缀。

    返回 `(base_model, tier)`：
      · 没带后缀 ⇒ `(原样model, None)`；
      · 带了 ⇒ `(去掉后缀的部分, "2k"/"4k"/…)`。

    ⚠️ 这是**纯语法拆分**，**不查表、不判存在性** —— 该档位这个模型到底
    支不支持，由调用方拿服务端 `resolution_map` 校验（见 `tier_supported`）。
    拆开两段是为了让"解析"与"校验"各自独立、可单独测试。
    """
    raw = (model or "").strip()
    if not raw:
        return None, None
    m = _TIER_SUFFIX_RE.match(raw)
    if not m:
        return raw, None
    return m.group("base").strip(), _TIER_CANON[m.group("tier").lower()]


def tier_supported(tier: str | None) -> bool:
    """该档位是否是**任何**已登记模型声明过的（粗筛，避免对明显不存在的档报错）。

    真正的"这个模型支不支持这一档"要读服务端 `resolution_map` ——
    那是运行期数据、此处不联网。调用方应优先用 `self.cfg.resolution_map(model)`。
    """
    return tier in _TIER_CANON.values()


def upstream_supports_blend(upstream_key: str) -> bool:
    """该上游模型**是否声明支持 blend（图生图）**。

    🔴 **只认服务端能力表**（`capabilities.py` 读 `get_common_config` 的
    `feats`），读不到就返回 **False**（= 不许用）——
    "读得到 ≠ 用得了"（v30l 的教训：表里有，实跑 `ret=1006`）。
    反过来也成立：**表里没有 ≠ 一定不行**，只是我们没有依据 ⇒ 保守拒绝，
    并让报错说清"去用不带模型名的 jimeng-i2i"。
    """
    return _BLEND_CAPABLE.get(upstream_key, False)


#: 由 `service.Service` 在启动/首次使用时从能力表回填：
#: `{上游 key: feats 里含 byte_edit}`。**不写死** —— 上游随时会改这张表。
_BLEND_CAPABLE: dict[str, bool] = {}


def set_blend_capable(mapping: dict[str, bool]) -> None:
    """回填"哪些上游模型支持 blend"（由 Service 调，测试可直接注入）。"""
    _BLEND_CAPABLE.clear()
    _BLEND_CAPABLE.update({k: bool(v) for k, v in mapping.items()})


def resolve(model: str | None, *, has_image: bool,
            n_images: int = 1, video: bool = False) -> tuple[Capability, str | None]:
    """解析 `model`，返回 (能力, 上游模型 key 或 None)。

    `video` 选择**候选池**（图片族 / 视频族）—— 两个池子的默认推导互不可见，
    否则"不写 model"就会在 t2i / t2v 之间产生歧义。给视频能力传 `has_image=True`
    会在形态校验处被拒（t2v 不吃输入图；i2v 未适配）。

    `has_image` 参与两件事：① 默认能力推导；② **能力与请求形态的一致性校验**
    （没给图却指定了吃图的能力 → 400，而不是跑到上游才发现）。

    `n_images` 参与**张数**校验：每个能力声明自己最多接受几张垫图
    （`Capability.max_images`），**超出当场 400** —— 绝不静默丢掉多余的图。

    上游模型 key 只在文生图族有意义；其余能力返回 None（草稿构造里不带 `model`）。
    **web 面板名**（`Seedream 5.0 Flash` / `5.0 Lite` / `4.7` …）等价于写上游 key，
    归一见 `_norm_model_name` 与 `UPSTREAM_MODEL_ALIASES`。
    """
    raw = (model or "").strip()
    norm = _norm_model_name(raw)

    #: 🔴 别名与"未登记面板名"必须判在**占位判之前**：面板名 `Seedream 5.0 Flash`
    #: 自带 `seedream` 前缀（在 `PLACEHOLDER_PREFIXES` 里），若先判占位就会被
    #: 静默降级成默认模型 —— 表现为"调用方点了 Flash、拿到 Lite"。
    #:
    #: 🔴 2026-10-02：`doubao-seedream-*`（方舟图片模型名）同样靠这条从占位里
    #: 救回来 —— **已登记进 `UPSTREAM_MODEL_ALIASES` 的**（目前 Flash，
    #: 由 `UPSTREAM_ARK_NAMES` 派生）按别名走真模型；**未登记的**
    #: （如 `doubao-seedream-5-0-pro-260628`）命不中别名，**仍按占位处理**。
    #: 这个分叉是刻意的：认一个没实测过的方舟名 = 替调用方悄悄换到
    #: 8 积分/张的 Pro 链路（"占位名走默认 Lite"只差 8 积分，但不能这么坑人）。
    #: 🔴 2026-10-02：占位判必须用**小写**后的 `raw`。
    #: 原来传的是原始 `raw` ⇒ `JM-8-2` 这类**大写**写法既不被认成占位
    #: （`PLACEHOLDER_PREFIXES` 里是小写 `mj-`/`seedream-`）、
    #: 又因别名表键是小写而查不中 ⇒ 落进"未知模型" 400。
    #: 症状：同一模型 `mj-8-2` 能用、`JM-8-2` 报未知 —— 纯大小写之差。
    if raw and (not is_placeholder(raw.lower())
                or norm in UPSTREAM_MODEL_ALIASES
                or norm in UNSUPPORTED_WEB_MODELS):
        #: 走"认得"这条路 ⇒ 不是兜底 ⇒ 清掉可能存在的上一轮记录，
        #: 否则调用方不取 `take_fallback_note()` 时会读到**上一次请求**的留痕
        #: （模块级状态的经典串味）。下方只有兜底分支会重新写。
        _LAST_FALLBACK.clear()
        low = raw.lower()
        cap: Capability | None = None
        upstream_model: str | None = None

        if low in ALIASES:
            cap = _BY_API_ID[ALIASES[low]]
        elif low in _BY_API_ID:
            cap = _BY_API_ID[low]
        elif low in _BY_NAME:
            cap = _BY_NAME[low]
        elif raw in UPSTREAM_MODEL_KEYS:
            cap = _BY_NAME["i2i"] if has_image else _BY_NAME["t2i"]
            upstream_model = raw
        elif norm in UNSUPPORTED_WEB_MODELS:
            # 面板上点得到、本服务**没登记** —— 给明确理由，不静默退化
            raise InvalidParameterError(
                f"{raw!r} 是即梦 web 面板上的模型（上游 key "
                f"{UNSUPPORTED_WEB_MODELS[norm]}），但本服务**未登记**它："
                f"该系列实测 `ret=1006` 权益不足（见 docs/UPSTREAM.md §11）。"
                f"要用图像生成请改用已登记的模型。{_hint()}", param="model")
        elif norm in UPSTREAM_MODEL_ALIASES:
            cap = _BY_NAME["i2i"] if has_image else _BY_NAME["t2i"]
            upstream_model = UPSTREAM_MODEL_ALIASES[norm]
        else:
            raise InvalidParameterError(
                f"未知 model {raw!r}。{_hint()}", param="model")
        assert cap is not None
        if (cap.media == "video") != video:
            where = ("方舟视频门面 /api/v3/contents/generations/tasks"
                     if cap.media == "video"
                     else "图片接口 /async/v1/images/generations")
            raise InvalidParameterError(
                f"model {raw!r}（{cap.title}）属于{'视频' if cap.media == 'video' else '图片'}族，"
                f"请走 {where}。", param="model")
        _check_shape(cap, raw=raw, has_image=has_image, n_images=n_images)
        return cap, upstream_model

    # ---- 默认能力：只在无歧义时给（**在声明的媒体池内**推导）----
    #: 🔴 走到这里且 `raw` 非空 ⇒ 调用方**显式写了一个占位名**（`seedream-9-9-ultra`
    #: / `gpt-image-1` / `doubao-seedream-5-0-pro-260628` …），而我们把它按占位处理
    #: ⇒ **兜底**。`raw` 为空则是"没写 model"的正常推导，**不算降级、不留痕**。
    if raw:
        _LAST_FALLBACK.clear()
        _LAST_FALLBACK[raw] = "（按无歧义默认推导）"

    pool = "video" if video else "image"
    cands = [c for c in CAPABILITIES
             if c.media == pool
             and not c.needs_source_ref          # 引用型能力不参与默认推导
             and c.accepts_image is has_image
             and not (has_image and not c.image_required)]
    if len(cands) == 1:
        # 默认分支同样要过形态校验（含张数上限）—— 否则"省掉 model"就成了绕过校验的口子
        _check_shape(cands[0], raw=cands[0].api_id, has_image=has_image,
                     n_images=n_images)
        eff = (DEFAULT_UPSTREAM_MODEL if cands[0].name == "t2i" else None)
        if raw and _LAST_FALLBACK:
            _LAST_FALLBACK[raw] = eff or cands[0].api_id
        return cands[0], eff

    kind = "带输入图" if has_image else "无输入图"
    if not cands:
        raise InvalidParameterError(
            f"本服务没有任何能力能处理该形态（{kind}），请检查请求。", param="model")
    raise InvalidParameterError(
        f"未指定 model，且{kind}时本服务有多个能力可选（"
        f"{', '.join(c.api_id for c in cands)}），无法确定用哪个 —— "
        f"它们的花费并不相同（实扣：hd 0 / i2i 0 / pro-hd 1 积分；outpaint 未对账），"
        f"故不替你挑。请显式指定 model。",
        param="model")


def _check_shape(cap: Capability, *, raw: str, has_image: bool,
                 n_images: int = 1) -> None:
    """能力与请求形态必须自洽 —— 不自洽就在**发出上游请求之前**报 400。"""
    if cap.image_required and not has_image:
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）需要输入图，但本次请求的 image 为空。"
            f"若想做文生图请用 jimeng-t2i。",
            param="image")
    if has_image and not cap.accepts_image:
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）不接受输入图，但本次请求带了 image。"
            f"请改用 jimeng-i2i / jimeng-hd / jimeng-pro-hd / jimeng-outpaint。",
            param="image")
    if n_images > cap.max_images:
        multi = ("支持多张垫图，但" if cap.max_images > 1 else "")
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）{multi}最多接受 {cap.max_images} 张输入图，"
            f"本次给了 {n_images} 张。"
            f"（本服务**不会静默忽略多余的图** —— 那会让你以为用了 {n_images} 张、"
            f"实际只用了 {cap.max_images} 张。"
            + ("要多张垫图请用 jimeng-i2i。" if cap.max_images == 1 else "")
            + "）",
            param="image")


#: 🔴 **视频族对外一律方舟模型名**（2026-09-24 用户拍板"全部用方舟 model"）：
#: `jimeng-t2v` 等内部名退为**路由别名**（受理仍接受，文档不再宣传）。
#: `jimeng-omni-video` / `jimeng-vfi` 不单列 —— 它们是**请求形态**不是模型
#: （前者 = 门面 content[] 带素材；后者 = 带 `source_task_id`/`target_fps`）。
ARK_PUBLIC_MODEL_ID: dict[str, str] = {
    "jimeng-t2v": "doubao-seedance-2-0-mini-260615",
    "jimeng-t2v-fast": "doubao-seedance-2-0-fast-260128",
    "jimeng-t2v-pro": "doubao-seedance-2-0-260128",
    "jimeng-t2v-2.5-draft": "doubao-seedance-2-5-260628",
}


def catalog() -> list[dict]:
    """`GET /v1/models` 用：本服务对外宣告的模型清单。

    ⚠️ 只列**没有已知缺陷**的能力。刻意缺席的（如细节修复 `super_resolution`
    两次 `status=30 generate_failed`）不出现在这里 —— 那就是"制造假能力"。

    🔴 视频族条目的 `id` 是**方舟模型名**（调用方直接当 `model` 传给门面
    `/api/v3/contents/generations/tasks`）；内部能力名在 `internal_id` 字段
    留作排查对照。全能参考（带素材）与补帧（source_task_id/target_fps）是
    方舟门面的**请求形态**，不单列条目。
    """
    out: list[dict] = []
    for c in CAPABILITIES:
        if c.media == "video":
            if c.name in ("omni-video", "vfi"):
                continue          # 请求形态，不单列（见 docstring）
            public_id = ARK_PUBLIC_MODEL_ID[c.api_id]
        else:
            public_id = c.api_id
        item = {
            "id": public_id,
            "object": "model",
            "created": 0,
            "owned_by": "jimeng",
            "title": c.title,
            "media": c.media,
            "accepts_image": c.accepts_image,
            "requires_image": c.image_required,
            "requires_prompt": c.prompt_required,
            "credits_measured": c.credits_measured,
            "notes": c.notes,
            "internal_id": c.api_id,
        }
        if c.name == _UPSTREAM_FAMILY:
            #: 🔴 `ark_name` = **可原样当 `model` 传**的方舟模型名（2026-10-02）。
            #: 没有方舟名的档位给 `None`（**不是空串**）—— 空串会被调用方
            #: 当成"有个名字叫空"，`None` 才能表达"这一档没有方舟对应名"。
            item["upstream_models"] = [
                {"key": k, "web_name": UPSTREAM_WEB_NAMES.get(k, ""),
                 "ark_name": UPSTREAM_ARK_NAMES.get(k),
                 #: 🔴 用**按模型**的实测价，不是能力级的值 —— 见
                 #: `UPSTREAM_MODEL_CREDITS` 的注释（能力级是 Lite 口径）
                 "credits_measured": UPSTREAM_MODEL_CREDITS.get(k)}
                for k in UPSTREAM_MODEL_KEYS
            ]
        out.append(item)
    return out


#: 刻意缺席的能力 —— 出现在文档与门禁里，不出现在 catalog 里。
DELIBERATE_ABSENCES: dict[str, str] = {}


__all__ = [
    "Capability", "CAPABILITIES", "REGISTRY", "ALIASES", "catalog",
    "resolve", "is_placeholder", "DELIBERATE_ABSENCES",
    "DEFAULT_UPSTREAM_MODEL", "UPSTREAM_MODEL_KEYS", "PLACEHOLDER_MODELS",
    "UPSTREAM_MODEL_ALIASES", "UNSUPPORTED_WEB_MODELS", "UPSTREAM_WEB_NAMES",
    "UPSTREAM_ARK_NAMES",
]
