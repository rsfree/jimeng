#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力解析门禁：默认能力只在**无歧义**时给；能力与请求形态必须自洽。"""
from __future__ import annotations

import pytest

from app.errors import InvalidParameterError
from app.models import (
    CAPABILITIES,
    DELIBERATE_ABSENCES,
    catalog,
    is_placeholder,
    resolve,
    take_fallback_note,
)


def test_no_image_defaults_to_t2i_unambiguously():
    cap, model = resolve(None, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model, "文生图必须带一个上游模型 key"


def test_with_image_refuses_to_guess():
    """带图时四个能力都能接 ⇒ **必须报错**，不能替调用方挑。

    它们的花费并不相同（实扣：hd 0 / i2i 0 / pro-hd 1；outpaint 未对账），
    挑错等于替调用方做了他没做的决定。
    ⚠️ 消息里的量级必须是**实扣口径**（曾误用 forecast 的 9 / 28 / 40 / 91）。
    """
    with pytest.raises(InvalidParameterError) as e:
        resolve(None, has_image=True)
    msg = e.value.message
    for api_id in ("jimeng-i2i", "jimeng-hd", "jimeng-pro-hd", "jimeng-outpaint"):
        assert api_id in msg, f"错误信息里必须列出候选，缺 {api_id}"
    assert "实扣" in msg and "pro-hd 1" in msg, \
        "应给出**实扣**量级（forecast 不许进对外消息），让人知道选择的代价"


@pytest.mark.parametrize("written,expect", [
    ("jimeng-t2i", "jimeng-t2i"),
    ("jimeng-hd", "jimeng-hd"),
    ("t2i", "jimeng-t2i"),
    ("hd", "jimeng-hd"),
    ("pro-hd", "jimeng-pro-hd"),
    ("outpaint", "jimeng-outpaint"),
    ("i2i", "jimeng-i2i"),
    ("即梦", "jimeng-t2i"),
    ("超清", "jimeng-hd"),
    ("智能超清", "jimeng-pro-hd"),
    ("扩图", "jimeng-outpaint"),
    ("JIMENG-HD", "jimeng-hd"),
])
def test_aliases_and_bare_names_resolve(written, expect):
    has_image = expect != "jimeng-t2i"
    cap, _ = resolve(written, has_image=has_image)
    assert cap.api_id == expect


def test_upstream_model_key_maps_to_t2i_with_that_model():
    cap, model = resolve("high_aes_general_v43", has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == "high_aes_general_v43"


def test_unknown_model_is_rejected_with_hint():
    with pytest.raises(InvalidParameterError) as e:
        resolve("gpt-4o", has_image=False)
    assert "未知 model" in e.value.message
    assert "jimeng-t2i" in e.value.message, "报错要给出可用清单"


@pytest.mark.parametrize("placeholder", ["", "auto", "default", "dall-e-3",
                                         "gpt-image-1",
                                         "doubao-seedream-5-0-pro-260628"])
def test_placeholders_count_as_unspecified(placeholder):
    """第三方 SDK 硬编码的占位名**不代表调用意图** ⇒ 走默认推导而不是报"未知模型"。

    ⚠️ `seedream-4-0` **已从这里移出**：它同时是即梦 web 面板上的正式模型名
    （Seedream 4.0），2026-09-23 起登记为**精确别名**。
    带厂商前缀 + 日期后缀的 `doubao-seedream-5-0-pro-260628` 仍算占位 ——
    它命不中别名，而且**绝不能**被映射到 Pro（那等于替调用方悄悄换到
    8 积分/张的链路）。
    """
    from app.models import DEFAULT_UPSTREAM_MODEL

    assert is_placeholder(placeholder)
    cap, model = resolve(placeholder, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == DEFAULT_UPSTREAM_MODEL, "占位名要落到默认上游模型"


def test_every_web_alias_maps_to_a_registered_upstream_model():
    """别名表**逐条**自检（不人工列举：表改了门禁跟着走，漏登记当场红）。

    别名是"web 面板名 → 上游 key"的派生数据，源是服务端能力表的
    `model_name` / `generation_category_name`（2026-09-23 实读）。
    """
    from app.models import UPSTREAM_MODEL_ALIASES, UPSTREAM_MODEL_KEYS

    assert UPSTREAM_MODEL_ALIASES, "别名表不许为空"
    for written, key in UPSTREAM_MODEL_ALIASES.items():
        assert key in UPSTREAM_MODEL_KEYS, f"{written!r} 指向未登记的 {key}"
        cap, model = resolve(written, has_image=False)
        assert (cap.api_id, model) == ("jimeng-t2i", key), written


@pytest.mark.parametrize("written,expect", [
    ("Seedream 5.0 Flash", "high_aes_general_v50_flash"),
    ("seedream 5.0 flash", "high_aes_general_v50_flash"),
    ("SEEDREAM_5.0_FLASH", "high_aes_general_v50_flash"),
    ("Seedream_5_0_Flash", "high_aes_general_v50_flash"),
    ("图片・5.0 Pro", "high_aes_general_v50p_large"),
    ("图片·5.0 Pro", "high_aes_general_v50p_large"),
    ("5.0 Lite", "high_aes_general_v50"),
    ("4.7", "high_aes_general_v43"),
    ("图片・4.5", "high_aes_general_v40l"),
])
def test_web_panel_name_variants_are_normalised(written, expect):
    """大小写与 `-`/`_`/空格/`.`/`・`/`·` 的差异都要归一。

    ⚠️ `・`(U+30FB) 与 `·`(U+00B7) 是**两个**字符，面板分类名用的是前者 ——
    少归一一种就会出现"看着一模一样、查表却查不中"的静默失效。
    """
    cap, model = resolve(written, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == expect, written


def test_unregistered_web_models_are_rejected_with_a_reason():
    """面板上点得到、本服务**没登记**的（Seedream 3.0 / 3.1）不许静默退化成默认模型。

    这条与"占位名走默认"是**相反**的处置，区别在于：占位名（`auto`/`dall-e-3`/
    `doubao-*`）不代表调用意图，而 `Seedream 3.1` 是调用方**指名道姓**。
    指名道姓就给明确理由（该系列实测 `ret=1006` 权益不足）。
    """
    from app.models import UNSUPPORTED_WEB_MODELS, UPSTREAM_MODEL_KEYS

    assert UNSUPPORTED_WEB_MODELS, "未登记面板名表不许为空"
    for written, key in UNSUPPORTED_WEB_MODELS.items():
        assert key not in UPSTREAM_MODEL_KEYS, f"{key} 不该同时又算已登记"
        with pytest.raises(InvalidParameterError) as e:
            resolve(written, has_image=False)
        assert "未登记" in e.value.message, written
        assert key in e.value.message, "报错要能追到上游 key"


@pytest.mark.parametrize("written,expect", [
    ("doubao-seedream-5-0-flash-260915", "high_aes_general_v50_flash"),
    ("Doubao-Seedream-5-0-Flash-260915", "high_aes_general_v50_flash"),
    ("doubao_seedream_5_0_flash_260915", "high_aes_general_v50_flash"),
    ("  doubao-seedream-5-0-flash-260915  ", "high_aes_general_v50_flash"),
])
def test_ark_image_name_resolves_to_the_real_model(written, expect):
    """🔴 火山方舟图片模型名（`doubao-seedream-*`）必须能**原样**当 `model` 传。

    这条是 2026-10-02 用户口径"图片族也对齐方舟命名"的落点。调用方手上
    拿到的往往就是方舟名（从方舟控制台/文档抄的），不该要求它先知道
    即梦的面板名。

    ⚠️ 判据不是"能 resolve"，而是**必须等于那个收费档的真模型**：
    方舟名自带 `doubao-seedream` 前缀（在 `PLACEHOLDER_PREFIXES` 里），
    一旦被当占位名吞掉，就会**静默降级成默认 Lite** ——
    症状是"调用方点了 Flash（3 积分）、拿到 Lite（0 积分）"，
    表现为免费，钱包没意见，但**拿到的图不是他要的模型**。
    所以这里断言的是**具体上游 key**，不是"没报错"。
    """
    cap, model = resolve(written, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == expect, f"{written!r} 落到了 {model!r}（占位降级？）"


def test_ark_image_names_are_registered_and_wired_into_the_alias_table():
    """`UPSTREAM_ARK_NAMES` 逐条自检：键必须是**已登记**的 key，且**已并进别名表**。

    两处都会导致"方舟名看起来支持、实际走占位降级"的静默失效：
      · 键指向未登记的 key ⇒ 别名派生出一个跑不通的上游模型；
      · 只加进别名表、忘了加 `UPSTREAM_ARK_NAMES` ⇒ `/v1/models` 不暴露、
        调用方无从发现（可发现性也是契约的一部分）。
    别名表由 ARK 表**反向派生**（不是手写字面量），这条门禁保证派生关系成立。
    """
    from app.models import (UPSTREAM_ARK_NAMES, UPSTREAM_MODEL_ALIASES,
                            UPSTREAM_MODEL_KEYS)

    assert UPSTREAM_ARK_NAMES, "方舟名表不许为空（Flash 已在册）"
    for upstream_key, ark_name in UPSTREAM_ARK_NAMES.items():
        assert upstream_key in UPSTREAM_MODEL_KEYS, \
            f"{ark_name!r} 指向未登记的 {upstream_key!r}"
        norm = ark_name.lower()
        assert norm in UPSTREAM_MODEL_ALIASES, \
            f"{ark_name!r} 没并进别名表 ⇒ 会被当占位名静默降级"
        assert UPSTREAM_MODEL_ALIASES[norm] == upstream_key, ark_name


def test_unregistered_ark_names_stay_placeholders_instead_of_silently_upgrading():
    """🔴 **未登记**的方舟名必须仍按**占位名**处理（落回默认 Lite），不许映射到收费档。

    这是本条改动**刻意留下的分叉**，不是遗漏：`PLACEHOLDER_PREFIXES` 收
    `doubao-seedream` 前缀，2026-10-02 之前所有方舟名都走占位。给 Flash
    开了口子之后，如果顺手把 `doubao-seedream-5-0-pro-260628` 也映射上，
    调用方会在**毫不知情**的情况下被切到 8 积分/张的 Pro 链路 ——
    比降级更坏：降级只给错图，升级要扣钱。

    所以这条钉住两件事：① 未登记名仍落回默认模型；② 它的落点必须是
    **免费的默认 Lite**（若哪天默认值变了，这里会红，提醒重新评估）。
    """
    from app.models import DEFAULT_UPSTREAM_MODEL

    for unregistered in ("doubao-seedream-5-0-pro-260628",
                         "doubao-seedream-4-5-251128",
                         "doubao-seedream-9-9-999999"):
        assert is_placeholder(unregistered), unregistered
        cap, model = resolve(unregistered, has_image=False)
        assert cap.api_id == "jimeng-t2i"
        assert model == DEFAULT_UPSTREAM_MODEL, \
            f"{unregistered!r} 被映射到了 {model!r} —— 那等于替调用方换收费档"


def test_catalog_exposes_ark_name_for_t2i_only():
    """`upstream_models[].ark_name` = 可原样传的方舟名；**没有就给 `None`**。

    🔴 未登记的档位必须给 `None` 而不是**空串** —— 空串会被调用方
    当成"有个名字叫空"，而 `None` 才表达"这一档没有方舟对应名"。
    """
    from app.models import UPSTREAM_ARK_NAMES, UPSTREAM_MODEL_KEYS

    t2i = [m for m in catalog() if m["id"] == "jimeng-t2i"][0]
    got = {u["key"]: u["ark_name"] for u in t2i["upstream_models"]}
    assert set(got) == set(UPSTREAM_MODEL_KEYS), "ark_name 没覆盖全部已登记 key"
    for k, ark in got.items():
        assert ark == UPSTREAM_ARK_NAMES.get(k), k
        if ark is None:
            assert k not in UPSTREAM_ARK_NAMES, k
    assert got["high_aes_general_v50_flash"] == "doubao-seedream-5-0-flash-260915", \
        "Flash 的方舟名缺失 —— 那是 2026-10-02 这次要加的东西"
    for m in catalog():
        if m["id"] != "jimeng-t2i":
            assert "upstream_models" not in m, m["id"]


MJ = "jm_image_model_yc_mj82"


def test_mj82_is_registered_across_all_four_tables():
    """mj82（图片美学模型 V8.2）四表齐全，且**实测价不是 None**。

    依据 = 2026-10-02 服务端能力表实读 + 端到端实跑（4 张实扣 20）。
    它的实测价必须钉住：`UPSTREAM_MODEL_CREDITS` 里填 `None`（未实测）时，
    调用方算不出成本 —— 而 mj82 是**按模型计价**里最容易被漏的一档。
    """
    from app.models import (UPSTREAM_MODEL_CREDITS, UPSTREAM_MODEL_KEYS,
                            UPSTREAM_WEB_NAMES)

    assert MJ in UPSTREAM_MODEL_KEYS, "mj82 未登记进白名单"
    assert UPSTREAM_WEB_NAMES[MJ] == "图片美学模型 V8.2", \
        "面板名必须逐字照抄能力表 model_name"
    assert UPSTREAM_MODEL_CREDITS[MJ] == 5, \
        "mj82 实测 5/张（4 张实扣 20）；None 会让调用方算错成本"


@pytest.mark.parametrize("written", [
    "图片美学模型 V8.2", "图片美学模型-V8.2", "图片美学模型 8.2",
    "mj-v8.2", "MJ-V8.2", "mj v8.2", "mj-v82", "mj82",
    "jm-8-2", "jm 8.2", "JM 8.2", MJ,
])
def test_mj82_name_variants_all_reach_it(written):
    """mj82 的各种写法都要能命中（面板名带空格与点，都要归一）。

    ⚠️ 归一化把 `.`/空格都换成 `-` 并**转小写**，所以 `mj-v8.2` → `mj-v8-2`、
    `图片美学模型 V8.2` → `图片美学模型-v8-2`。**别名表的键必须与归一输出逐字
    一致** —— 写成 `mj-8-2`（m/j 顺序颠倒）就永远查不中，且症状很隐蔽：
    表里明明有这条别名，resolve 却报"未知模型"。
    """
    cap, model = resolve(written, has_image=False)
    assert cap.api_id == "jimeng-t2i", written
    assert model == MJ, written


def test_every_alias_key_is_in_normalised_form():
    """🔴 别名表的**每个键**都必须等于 `_norm_model_name(该键)`。

    为什么这条值得单列：键写错（大小写、连字符、m/j 顺序）时
    `resolve` 查不中 ⇒ "表里有这个别名但报未知模型"。
    这是**沉默失配**—— 看代码觉得配了，运行时却没有；
    且 `test_every_web_alias_maps_to_a_registered_upstream_model`
    那条门禁**抓不到**（它遍历的是表里的键 —— 错的键自己撞自己，仍然"通过"）。

    这条门禁的作用就是把"错键"在导入期就照出来。
    """
    from app.models import _norm_model_name, UPSTREAM_MODEL_ALIASES

    for written in UPSTREAM_MODEL_ALIASES:
        norm = _norm_model_name(written)
        assert norm == written, (
            f"别名键 {written!r} 不是归一形态（应为 {norm!r}）—— "
            f"resolve 查不中，且既有门禁抓不到")


def test_mj82_declares_four_images_and_is_marked_uncontrollable():
    """🔴 mj82 **张数不可控** ⇒ 按恒 4 张设计与告知。

    证据（2026-10-02 七发真跑 + 原始报文四字段交叉核对）：
      · 服务端 `generate_count_options=[4]` / `default_generate_count=4`；
      · 传 2/3/4 一律出 4 张（回执草稿 `gen_count` 被归一成 4）；
      · 传 1 **多数被抬成 4**（仅在绕过吸附且恰好落 1 时出过 1 张，
        该路径不稳定、计费也随之变化，**不可依赖**）。
    ⇒ 唯一稳定的契约是"恒 4 张"。本门禁钉住三件事：
      ① 声明值是唯一取值 4；② 任何 n 都被吸附成 4 且留痕；
      ③ 受理层有"张数不可控"的响亮留痕（否则调用方误以为拿到 n 张）。
    """
    cap, model = resolve(MJ, has_image=False)
    assert (cap.api_id, model) == ("jimeng-t2i", MJ)
    from app.upstream.jimeng.client import (COUNT_OPTIONS_BY_MODEL,
                                            MJ82_COUNT_EFFECTIVE, resolve_count)

    opts = COUNT_OPTIONS_BY_MODEL[MJ]
    assert len(opts) == 1 and opts[0] == 4, \
        f"mj82 的服务端声明应是唯一取值 (4,)，实得 {opts}"
    for want in (1, 2, 3, 4, 8):
        n, _ = resolve_count(MJ, want, opts)
        assert n == 4, f"请求 {want} 张必须被吸附成 4，实得 {n}"
    # ⚠️ 留痕**不在** `resolve_count`：n=4 恰在声明取值内 ⇒ 它不产告警
    #（"没告警"≠"张数可控"，单值声明本身就是不可控）。
    # 真正的告知在 `service.create` 的 `len(declared)==1` 分支 ——
    # 那条已由test_count_uncontrollable_model_is_reported_not_silently_snapped 钉住。
    # 这里只钉"单值声明 + 给了 n（含n=4）⇒ 必走那条留痕"的判据本身。
    import inspect
    from app.service import Service
    src = inspect.getsource(Service.create)
    assert "elif len(declared) == 1 and n_raw is not None:" in src, \
        "单值声明 + 给了 n 时必须走『张数不可控』留痕（含 n 恰等于声明值的情形）"
    # 实测规律表也要在（它是"为什么恒 4 张"的证据载体）
    assert MJ82_COUNT_EFFECTIVE == (1, 4), MJ82_COUNT_EFFECTIVE


def test_mj82_credit_is_flagged_as_unsettled_per_image():
    """🔴 mj82 的"每张单价"**未解**：1k 4 张=20（5/张）但 1 张=7。

    实测：4 张 1k 扣 20、4 张 2k 扣 28、**1 张 1k 也扣 7**（不是 5）
    ⇒ 单价不是常数（疑似"档位最低消费"），**规则未解**。
    这里断言"登记表里的值必须是已知口径之一"，并在口径变化时强制更新注释 ——
    避免"填了个看着合理的数字"被当成实测值用（那会让调用方算错成本）。
    """
    from app.models import UPSTREAM_MODEL_CREDITS

    # 已知实测口径：1k 四张 ⇒ 20/4 = 5/张
    assert UPSTREAM_MODEL_CREDITS[MJ] == 5, \
        "1k 四张口径 = 5/张；若实测口径变了请同步 UPSTREAM.md §17.3 的表格"


def test_count_uncontrollable_model_is_reported_not_silently_snapped():
    """受理层：只声明一个取值的模型必须额外留痕"张数不可控"。

    为什么单列一条门禁：`count_options_declared is None` 那条分支**抓不到**
    `[4]` 这种（非空但唯一）声明 ⇒ 若无此留痕，mj82 的"恒 4 张"就成了
    静默行为。这是"让调用方算错成本"那类错，必须挡住。
    """
    from app.service import Service
    import inspect

    src = inspect.getsource(Service.create)
    assert "张数不可控" in src, \
        "受理路径必须对'只声明一个取值'的模型留痕（否则恒 4 张静默发生）"
    assert "len(declared) == 1" in src, \
        "判据必须是 len(声明)==1，而不是只判 None"


def test_i2i_accepts_upstream_model_and_t2i_does_not_swallow_it():
    """带输入图 + 上游模型名 ⇒ 落**i2i**（blend），不是 t2i。

    🔴 这条是接线门禁：原先 `resolve` 只在 t2i 族返回上游模型，
    于是 `model=mj82` + 带图会撞"t2i 不接受输入图"的 400 ——
    而 mj82 的 `feats` 明明含 `byte_edit`（服务端已宣告支持图生图）。
    """
    cap, model = resolve(MJ, has_image=True)
    assert cap.api_id == "jimeng-i2i", "带图时应解析为 i2i"
    assert model == MJ, "上游模型必须一起带下去（图生图也要换模型）"
    # 不带图时仍是 t2i —— 别把两条链路搞反
    cap2, model2 = resolve(MJ, has_image=False)
    assert (cap2.api_id, model2) == ("jimeng-t2i", MJ)


def test_mj82_blend_was_proven_end_to_end_not_just_wired():
    """🔴 mj82 走 blend（图生图）**已端到端真跑** —— 不只是"代码路径通"。

    这条门禁钉住一个容易被糊弄过去的区分：
      · `test_i2i_accepts_upstream_model_and_t2i_does_not_swallow_it` 只能证明
        **我们**会把模型名传下去；
      · 但"上游认不认这个模型走 byte_edit" **只有真跑能回答**。
    2026-10-02 首版探针的 i2i 用例**被跳过了**（依赖的产物没取到），
    等于"接线修好了但没验证过" —— 那种状态最容易自我欺骗。

    ✅ 2026-10-02 补测通过（submit_id `64a86c5c…`）：
      · 走生产同一条路（下载垫图 → ImageX 上传 → `blend(image_uris=…)`）；
      · 提交包 `model` 回读 = `jm_image_model_yc_mj82`（**不是**默认 Lite）；
      · 终态 status=50，出图 4 张，四字段一致；
      · 消耗记录 `amount=28`（4 张 2k = 7/张），submit_id 对得上。
    判据取自docs/UPSTREAM.md §17.5 的实测记录。
    """
    from app.models import UPSTREAM_MODEL_CREDITS

    # mj82 的图生图与文生图**同一计费档**（都是 4 张 2k = 28）
    # ⇒ 登记的单价（1k 四张 = 5/张）不能被当成 i2i 的口径
    assert UPSTREAM_MODEL_CREDITS[MJ] == 5, \
        "登记的是 1k 四张口径（5/张）；2k 档是 7/张，口径不同别混用"
    # 真跑结论必须留在文档里（代码之外的唯一证据载体）
    from pathlib import Path
    doc = Path(__file__).resolve().parent.parent / "docs" / "UPSTREAM.md"
    text = doc.read_text(encoding="utf-8")
    assert "64a86c5c" in text, \
        "i2i 真跑的 submit_id 证据丢失了 —— §17.5 必须留着可追溯的凭据"


def test_blend_capability_is_read_from_server_not_hardcoded():
    """`upstream_supports_blend` 只认**服务端能力表**，读不到 ⇒ False。

    "读得到 ≠ 用得了"（v30l 教训）的镜像：**表里没有也别乐观假设**。
    猜错的后果是建任务后才炸（上传完垫图、提交被打回）。
    """
    from app.models import set_blend_capable, upstream_supports_blend

    set_blend_capable({MJ: True})
    assert upstream_supports_blend(MJ) is True
    set_blend_capable({MJ: False})
    assert upstream_supports_blend(MJ) is False, "能力表说没有就是没有"
    set_blend_capable({})
    assert upstream_supports_blend(MJ) is False, \
        "读不到能力表时必须保守拒绝（不能默认 True）"
    assert upstream_supports_blend("never-heard-of-this-model") is False


def test_upstream_name_tables_do_not_drift():
    """`UPSTREAM_MODEL_KEYS` 与 `UPSTREAM_WEB_NAMES` 必须是**同一集合**。

    两张表漂移的典型症状：新模型登记进白名单、忘了补面板名 ⇒
    `/v1/models` 里 `web_name` 是空串，调用方按面板名传反而被拒。
    """
    from app.models import UPSTREAM_MODEL_KEYS, UPSTREAM_WEB_NAMES

    assert set(UPSTREAM_WEB_NAMES) == set(UPSTREAM_MODEL_KEYS)
    assert all(UPSTREAM_WEB_NAMES[k].strip() for k in UPSTREAM_MODEL_KEYS)


def test_upstream_model_credits_table_does_not_drift():
    """**按模型**的实测单价表必须与白名单同集合，且不许被能力级的值污染。

    🔴 这是最容易犯的错：`jimeng-t2i` 的能力级实测价是 0（**Lite 口径**），
    而同一个端点换模型就换价 —— Flash 实测 3、Pro 实测 8。
    把能力级的值抄给每个上游模型 = 对 Flash 报"免费"，调用方会算错成本。
    """
    from app.models import (UPSTREAM_MODEL_CREDITS, UPSTREAM_MODEL_KEYS,
                            UPSTREAM_WEB_NAMES)

    assert set(UPSTREAM_MODEL_CREDITS) == set(UPSTREAM_MODEL_KEYS)
    assert set(UPSTREAM_WEB_NAMES) == set(UPSTREAM_MODEL_KEYS)
    for key, cost in UPSTREAM_MODEL_CREDITS.items():
        assert cost is None or cost >= 0, key
    assert UPSTREAM_MODEL_CREDITS["high_aes_general_v50"] == 0, "Lite 实测免费"
    assert UPSTREAM_MODEL_CREDITS["high_aes_general_v50_flash"] == 3, \
        "Flash 2026-09-23 实跑实扣 3（2k/1 张）"


def test_catalog_exposes_upstream_models_only_for_the_t2i_family():
    """`upstream_models` 只挂文生图族 —— 别的能力没有"上游模型"这个维度。"""
    from app.models import UPSTREAM_MODEL_CREDITS, UPSTREAM_MODEL_KEYS

    for m in catalog():
        if m["id"] == "jimeng-t2i":
            assert [u["key"] for u in m["upstream_models"]] == list(UPSTREAM_MODEL_KEYS)
            assert all(u["web_name"] for u in m["upstream_models"]), \
                "面板名不许为空 —— 空说明门禁没跟上"
            #: 每项报的是**该模型**的实测价，不是能力级的 0
            for u in m["upstream_models"]:
                assert u["credits_measured"] == UPSTREAM_MODEL_CREDITS[u["key"]], u
        else:
            assert "upstream_models" not in m, f"{m['id']} 不该有 upstream_models"


def test_image_required_capability_without_image_is_400_before_upstream():
    with pytest.raises(InvalidParameterError) as e:
        resolve("jimeng-hd", has_image=False)
    assert "需要输入图" in e.value.message


def test_non_image_capability_with_image_is_400():
    with pytest.raises(InvalidParameterError) as e:
        resolve("jimeng-t2i", has_image=True)
    assert "不接受输入图" in e.value.message


def test_catalog_hides_deliberately_absent_capabilities():
    """「不制造假能力」：实测会失败的能力不许出现在清单里。"""
    ids = {m["id"] for m in catalog()}
    # jimeng-detail-fix 曾在此列表（两次 origin_image 形态失败）；
    # 2026-09-20 引用形态真跑成功后**转正**（免费，两证吻合）。
    assert DELIBERATE_ABSENCES == {}, "缺席必须被显式记录，不是忘了"
    # 五个已端到端验证过的能力都在；t2v/vfi/omni/detail-fix 见各自 notes
    from app.models import ARK_PUBLIC_MODEL_ID
    expect = {ARK_PUBLIC_MODEL_ID.get(c.api_id, c.api_id) for c in CAPABILITIES
              if not (c.media == "video" and c.name in ("omni-video", "vfi"))}
    assert ids == expect
    assert len(ids) == 10
    # 🔴 视频族对外用方舟模型名（内部名在 internal_id）；omni/vfi 是请求形态不单列
    assert {"doubao-seedance-2-0-mini-260615", "doubao-seedance-2-0-fast-260128",
            "doubao-seedance-2-0-260128", "doubao-seedance-2-5-260628"} <= ids
    assert not ({"jimeng-t2v", "jimeng-t2v-fast", "jimeng-t2v-pro",
                 "jimeng-t2v-2.5-draft", "jimeng-omni-video", "jimeng-vfi"} & ids)


def test_detail_fix_tool_description_is_kept_for_future_investigation():
    """工具描述要留着，否则下次得重新取证。转正后 registered 标志已删。"""
    from app.upstream.jimeng.client import POST_EDIT_TOOLS

    assert "detail" in POST_EDIT_TOOLS
    assert "registered" not in POST_EDIT_TOOLS["detail"]


def test_every_capability_has_measured_credits_where_claimed():
    """单价要么是**实测值**，要么就不写 —— 不许编一个"看起来合理"的数。

    2026-09-20 校准：原先 5 个能力的单价全部取自上游回执的 `forecast_generate_cost`，
    而按 `submit_id` 对账后实测**高估 4~9 倍**（i2i 报 59 / 实扣 **12**）。
    ⇒ 未实测的一律置 `None`：宁可"不报数"，也不能报一个让调用方算错成本的值。
    """
    measured = [c for c in CAPABILITIES if c.credits_measured is not None]
    assert measured, "至少要有一个实测价（i2i）"
    for c in measured:
        assert c.credits_measured >= 0, f"{c.api_id} 的实测价不能为负"
    # 实测免费的能力要**如实报 0**（而不是 None、也不是编一个数）
    free = [c.name for c in CAPABILITIES if c.credits_measured == 0]
    assert {"t2i", "i2i", "hd"} <= set(free), \
        f"Lite 上实测免费的应如实报 0，实得 {free}"
    for c in CAPABILITIES:
        assert c.notes, f"{c.api_id} 缺少依据说明"


def test_credits_measured_reflects_actual_charges_not_forecasts():
    """🔴 单价字段只放**实扣**（记录 / 余额差分核实），forecast 预报一律不进字段。

    2026-09-23 修正（两处历史混淆）：
      · `jimeng-hd` 曾被记成"实扣 1"—— 复核发现那条 `amount=1` 的消耗记录
        （`智能超清2.0-2k`）经 submit_id 归属核实属于 **pro-hd** ⇒ hd 维持
        "实扣 0（免费）"，pro-hd 从"未测"升级为**实扣 1**。
      · `jimeng-t2v` 的 notes 早已写明"实扣 24"（2026-09-20 三证），
        但字段一直是 `None` ⇒ 本次回填。
    ⇒ 本用例把四个"容易被 forecast 再次污染"的点钉死。
    """
    by = {c.api_id: c for c in CAPABILITIES}
    assert by["jimeng-hd"].credits_measured == 0, \
        "hd 实扣 0（免费）；勿按回执 forecast 9 改"
    assert by["jimeng-pro-hd"].credits_measured == 1, \
        "pro-hd 实扣 1（9-22 消耗记录 + submit_id 归属核实）"
    #: 2026-10-02 更新：outpaint **首次对账完成** —— 实扣 **1**
    #: （submit_id `0e12b55d`，余额差分 + 消耗记录 + 任务表三证吻合），
    #: 从 `None` 升级为实测值。**注意 forecast 报的是 35** ⇒ 高估 35 倍，
    #: 这正是本门禁要防的那类污染。旧的"必须保持 None"断言随之作废。
    assert by["jimeng-outpaint"].credits_measured == 1, \
        "outpaint 实扣 1（10-02 三证对账）；若再改回 None 说明有人把 "\
        "forecast/未对账当成了结论"
    assert by["jimeng-t2v"].credits_measured == 24, "t2v 实扣 24（9-20 三证对账）"
    for api_id in ("jimeng-hd", "jimeng-pro-hd", "jimeng-outpaint", "jimeng-t2v"):
        assert ("forecast" in by[api_id].notes) or ("预报" in by[api_id].notes), \
            f"{api_id} 的 notes 必须点明 forecast/预报口径（防再被当实扣）"


def test_new_upstream_model_flash_is_registered_with_declared_count_options():
    """2026-09-23 服务端能力表实读新增的 **Seedream 5.0 Flash** 必须可路由。

    证据等级要说清楚：这是**上游自己宣告**的（`is_new_model: true`、`feats`
    含 `t2i`、`generate_count_options` 1..4、`default_resolution_type` 2k、
    benefit_type `image_basic_v50_flash_2k` / `_15k`），**不是端到端实跑**。
    所以这里只钉三件事：能解析、张数快照与服务端一致、**不报单价** ——
    绝不钉任何积分数值（未实测就不许报，见上一条门禁）。
    """
    from app.models import UPSTREAM_MODEL_KEYS
    from app.upstream.jimeng.client import COUNT_OPTIONS_BY_MODEL

    key = "high_aes_general_v50_flash"
    assert key in UPSTREAM_MODEL_KEYS, "服务端宣告的新模型必须登记才能被路由"
    #: 能读的就不许猜：张数快照必须与服务端声明一致（服务端 = 1..4）
    assert COUNT_OPTIONS_BY_MODEL[key] == (1, 2, 3, 4)

    cap, model = resolve(key, has_image=False)
    assert cap.api_id == "jimeng-t2i", "新模型是 t2i 族的一个选项，不是新能力"
    assert model == key
    assert key not in {c.api_id for c in CAPABILITIES}, \
        "上游模型 key 不该被注册成一个独立能力"

    #: 🔴 单价纪律：`credits_measured` 是**能力级**的（t2i 按默认模型 Lite 实测 0），
    #: 本服务**没有**"按上游模型分档的单价"字段 ⇒ 新模型不可能、也不许"被报一个价"。
    #: 这条断言防的是"后人顺手把 flash 的 amount=1 抄成单价"（§12 已证 amount≠实扣）。
    assert cap.credits_measured == 0, "t2i 的实测值仍是 Lite 口径，不该被新模型改写"
    #: 但它已经在 **2026-09-23 端到端实跑过**（2k/1 张，实扣 3）——
    #: notes 必须如实标明"实跑"，而不是还停在"服务端宣告、未验证"。
    assert key in cap.notes and "实跑" in cap.notes, \
        "已跑过的模型，notes 必须写明实跑记录"


# ---------------------------------------------------------------------------
# 占位名兜底必须**留痕**（2026-10-02 实测缺陷的防回归门禁）
# ---------------------------------------------------------------------------

#: 实测会静默降级的三种写法：`PLACEHOLDER_PREFIXES` 收了 `seedream` 前缀，
#: 于是任何 `seedream-<不存在的型号>` 都被判成占位名 ⇒ 走默认推导 ⇒ 真出图。
#: 2026-10-02 线上实测：`seedream-9-9-ultra` 与 `seedream-4-9` 均返回 200
#: 并出图成功（submit_id 9154eaa2 / 第二次 34.9s），而响应体 `degradations` 为空。
UNREGISTERED_PLACEHOLDERS = ["seedream-9-9-ultra", "seedream-4-9", "seedream-5-0"]


@pytest.mark.parametrize("name", UNREGISTERED_PLACEHOLDERS)
def test_unregistered_seedream_falls_back_to_free_default(name):
    """兜底到**免费**默认档这件事本身是允许的（用户口径）。

    钉的是两件事：① 落点必须是**免费**的默认模型（悄悄升到收费档更坏）；
    ② 必须**留下痕迹** —— 调用方有权知道自己拿到的是兜底图。
    """
    cap, model = resolve(name, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == "high_aes_general_v50", \
        "未登记型号只能兜底到免费默认档；绝不能悄悄换成收费模型"

    note = take_fallback_note()
    assert note is not None, f"{name!r} 走了兜底却没有任何留痕 —— 这就是静默降级"
    assert name in note, "留痕必须写明调用方原始写了什么"
    assert "high_aes_general_v50" in note, "留痕必须写明实际生效成了哪个模型"


def test_known_model_names_are_not_reported_as_fallback():
    """反向门禁：**认得的名字不许被记成降级**，否则留痕会变成噪声。"""
    for name in ("Seedream 5.0 Flash", "high_aes_general_v50p_large",
                 "jimeng-t2i", "图片-5-0-Pro"):
        resolve(name, has_image=False)
        assert take_fallback_note() is None, \
            f"{name!r} 是已登记型号，不该产生兜底留痕"


def test_omitting_model_is_not_a_fallback():
    """「没写 model」的正常默认推导**不算降级** —— 它是文档承诺的行为。"""
    cap, model = resolve(None, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert take_fallback_note() is None, \
        "没写 model 是正常用法，不该被报成降级"


def test_fallback_note_does_not_leak_across_calls():
    """模块级状态的经典串味防护：取走即清，且认得的名字会主动清空。"""
    resolve("seedream-9-9-ultra", has_image=False)
    assert take_fallback_note() is not None
    assert take_fallback_note() is None, "取走即清 —— 同一请求不该读到两条"

    #: 上一次是兜底，这一次是认得的名字 ⇒ 不许把上一轮的留痕带过来
    resolve("seedream-9-9-ultra", has_image=False)
    resolve("Seedream 5.0 Flash", has_image=False)
    assert take_fallback_note() is None, "认得的名字必须清掉上一轮兜底记录"
