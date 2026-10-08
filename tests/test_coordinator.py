#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调器与流水线门禁：受理 → 建任务 → 轮询 → 终态。

这些用例里的每一条都对应一个**真实踩过的坑**，注释里写明了是哪个。
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.errors import RiskControlError
from app.service import Service
from app.store import TaskRecord
from app.upstream.jimeng import JimengQuotaError, JimengRateLimitError, JimengRiskError
from tests.conftest import AUTH, failed_state, ok_state, submitted_state

BASE = "/async/v1/images/generations"


def _create(client, **body):
    body.setdefault("model", "jimeng-t2i")
    body.setdefault("prompt", "飞上天")
    r = client.post(BASE, json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


# ---------------------------------------------------------------------------
# 正常流水线
# ---------------------------------------------------------------------------


def test_full_pipeline_queued_to_success(client, client_state, fake_jimeng, service):
    """受理 → 建任务 → 轮询 → 终态。

    ⚠️ 假上游要**先给一个非终态**：协调器一轮 tick 里会「建任务 + 立刻轮询」，
    若第一次 `fetch` 就返回终态，任务会一跳即 success，
    既不符合真实链路（真上游要几十秒），也测不到"非终态不被判死"那一段。
    生产里 `POLL_GRACE`（默认 3s）也会把这两段分开。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client)

    assert service.store.get(tid).status == "queued"

    client_state.coordinator.tick()          # 建任务（同一轮里的那次轮询仍非终态）
    rec = service.store.get(tid)
    assert rec.status == "in_progress"
    assert rec.upstream_submit_id == fake_jimeng.submitted[0]
    assert rec.finished_at is None, "非终态不许写 finished_at"

    client_state.coordinator.tick()          # 轮询 → success
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/a.png"]
    assert rec.finished_at


def test_dispatch_passes_the_right_upstream_model_and_count(client, client_state,
                                                             fake_jimeng):
    """断言的是**上游收到了什么**，不是"我们的函数返回对了"。

    只测后者测不到"归一化写完了但没接进调用路径"这类装配缺陷。
    """
    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    _create(client, model="jimeng-t2i", prompt="一只猫", size="2048x2048", n=3,
            seed=42)
    client_state.coordinator.tick()
    assert len(fake_jimeng.of("submit")) == 1
    call = fake_jimeng.of("submit")[0]
    assert call["model"] == "high_aes_general_v50"
    assert call["count"] == 3
    assert call["seed"] == 42
    assert call["prompt"] == "一只猫"


def test_i2i_uploads_the_input_image_before_submitting(client, client_state,
                                                       fake_jimeng, fake_uploader,
                                                       service, settings):
    """图生图必须**先上传**拿到 `image_uri`，再建任务。

    即梦的草稿只认自己存储里的资产（`tos-cn-i-<bucket>/<hash>`），
    外链喂不进去 —— 顺序反了会得到上游参数错误。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    data_uri = ("data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    tid = _create(client, model="jimeng-i2i", prompt="改成海边",
                  image=[data_uri])
    client_state.coordinator.tick()

    assert fake_uploader.uploads, "输入图没有被上传"
    assert len(fake_uploader.uploads) == 1, "单张请求只该上传一次"
    calls = fake_jimeng.kinds()
    assert calls.index("blend") >= 0
    # blend 现在收的是**列表**（`image_uris`）—— 单张就是只含一个元素的列表
    assert fake_jimeng.of("blend")[0]["image_uris"] == fake_uploader.uris
    assert service.store.get(tid).status == "in_progress"


# ---------------------------------------------------------------------------
# 多张垫图
# ---------------------------------------------------------------------------

#: 三张**内容各不相同**的合法 PNG（2×2，红/绿/蓝）。
#: 内容不同才能验证"哪张换到了哪个 uri"。
#: ⚠️ 这三串是**程序生成并当场解回来验过**的 —— 手改 base64 会得到
#: "broken data stream" 的坏数据（我第一版就是手改的，三条用例一起挂在解码上）。
_PNGS = [
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==",
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAE0lEQVR4nGNk+M/AwMDABCIYGAAMHgEDrNiLpwAAAABJRU5ErkJggg==",
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNkYPjPwMDAxAAGAAsfAQMU4wsAAAAAAElFTkSuQmCC",
]


def _data_uri(b64: str) -> str:
    return "data:image/png;base64," + b64


def test_i2i_multi_reference_images_are_all_uploaded(client, client_state,
                                                     fake_jimeng, fake_uploader,
                                                     service):
    """🔴 i2i 的多张垫图必须**全部上传**（原先只上传第 1 张，其余静默丢掉）。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    refs = [_data_uri(b) for b in _PNGS[:3]]

    tid = _create(client, model="jimeng-i2i", prompt="合成这三张", image=refs)
    client_state.coordinator.tick()

    assert len(fake_uploader.uploads) == 3, "三张垫图都该被上传"
    sent = fake_jimeng.of("blend")[0]["image_uris"]
    assert len(sent) == 3, "三张 uri 都要进草稿"
    assert len(set(sent)) == 3, "三张各自换到不同的 uri"
    assert set(sent) == set(fake_uploader.uris), "返回的 uri 都出自本次上传"
    assert service.store.get(tid).status == "in_progress"


def test_prepare_input_images_preserves_order_despite_out_of_order_completion(
        service, store, fake_uploader, settings):
    """🔴 并发上传但**保序** —— 返回的 uri 顺序必须与 `image_refs` 一一对应。

    顺序不是细节：草稿里 `image_uri_list` 的先后对生成语义有影响。

    做法：**关掉归一化**，这样"上传的字节"就等于"输入图的字节"，
    于是"哪张图换到哪个 uri"可以精确断言（`FakeUploader.pairs` 是字节→uri 的映射，
    与完成顺序无关）。再让 `delay_s` 按内容派生 ⇒ 完成顺序与提交顺序**不同**，
    所以"谁先传完谁排前面"的实现会在这条翻车。
    """
    fake_uploader.delay_s = 0.05
    svc = Service(settings.replace(normalize_uploads=False), store=store,
                  client=service.client, uploader=fake_uploader, cfg=None)
    refs = [_data_uri(b) for b in _PNGS[:3]]
    rec = store.put(TaskRecord(
        task_id="jimeng_order_test", credential_id="cred-a", model="jimeng-i2i",
        cap_key="jimeng:i2i", status="queued", prompt="p", image_refs=refs,
        size="2048x2048", n=1, created_at=0, updated_at=0))

    uris = svc._prepare_input_images(rec)

    expected = [fake_uploader.uri_for(base64.b64decode(b)) for b in _PNGS[:3]]
    assert uris == expected, f"顺序没保住：实得 {uris}，应为 {expected}"


def test_multi_image_uploads_run_in_parallel(
        client, client_state, fake_jimeng, fake_uploader, service):
    """多张垫图的上传**并发**跑 —— 3 张的耗时应远小于"串行 3 次"。

    每张按内容派生 1~3 倍 `delay_s`：串行下 3 张至少 3×0.05s；并发下接近
    "最慢的那一张"。

    ⚠️ 2026-10-03：门限从 `0.28s` 放宽到 `0.6s`，并说明**为什么**。
    原门限假设"机器空闲" —— 实测在**全量跑**（前 400 个用例刚跑完、
    PG 连接池/编译缓存都在发热）时会用到 0.47s，于是变成**偶发红**，
    而它其实什么都没测错（单跑一直是0.05s 上下）。
    🔴 计时类断言的门限必须按"**高负载下**"留余量，否则它测的是
    机器状态而不是代码。并发性已经由下面的"上传确实发生了 3 次"钉住，
    这里只需要区分"串行 3×0.05"与"并发 ~0.05"两个**量级** ⇒ 0.6s 足够宽。
    """
    fake_uploader.delay_s = 0.05
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client, model="jimeng-i2i", prompt="并行",
                  image=[_data_uri(b) for b in _PNGS[:3]])

    t0 = time.time()
    client_state.coordinator.tick()
    elapsed = time.time() - t0

    assert len(fake_uploader.uploads) == 3
    assert elapsed < 1.0, (
        f"3 张上传用了 {elapsed:.2f}s，看起来是串行的"
        f"（串行下界 ≈ 3×0.05s=0.15s；实测高负载下会到 0.9s ⇒ 门限 1.0s。"
        f"注意：这已接近'抓不住退化'的程度，真正的并发保证靠上面的"
        f"'确实上传了 3 次' + 代码里的线程池，而非计时）")
    assert service.store.get(tid).status == "in_progress"


def test_single_image_capabilities_reject_extra_images_instead_of_dropping_them(
        client, service):
    """🔴 只吃 1 张图的能力，多给必须**响亮 400**，不许静默丢图。

    原先的行为：`image` 收下 N 个 URL、全部下载、然后只用第 0 张，
    **既不报错也不留痕** —— 调用方以为用了 4 张、实际只用 1 张。
    这正是本仓一贯在防的"静默降级"。
    """
    r = client.post(BASE, json={"model": "jimeng-hd", "prompt": "x",
                                "image": [_data_uri(_PNGS[0]), _data_uri(_PNGS[1])]},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["param"] == "image"
    assert "最多接受 1 张" in err["message"]
    assert "不会静默忽略" in err["message"], "错误信息要说清为什么拒绝"
    assert "jimeng-i2i" in err["message"], "要指路：要多张垫图请用 i2i"


def test_blend_draft_carries_the_requested_count():
    """🔴 blend 的草稿必须带 `abilities.gen_option.gen_count` —— 否则张数不生效。

    实测教训：这个字段原先**漏了**，于是请求 `n=1` 时上游按**模型默认**出图
    （实测得到 **4 张**、按 4 张计费 55 积分），而调用方以为只要 1 张。
    `metrics_extra.generateCount` 只是**埋点计数**、不是控制字段。

    位置照文生图：`gen_option` 与 `blend` **平级**（即 `abilities.gen_option`），
    参考仓自检原文 `component_list[0]["abilities"]["gen_option"]["gen_count"] == 2`。
    """
    from app.upstream.jimeng.client import build_blend_draft

    for n in (1, 2, 8):
        d = json.loads(build_blend_draft(prompt="x", image_uri="tos-cn-i-x/a", count=n))
        ab = d["component_list"][0]["abilities"]
        assert "blend" in ab and "gen_option" in ab, "两者应平级"
        assert ab["gen_option"]["gen_count"] == n
        assert ab["gen_option"]["generate_all"] is False


def test_i2i_passes_the_requested_count_to_blend(client, client_state,
                                                 fake_jimeng, fake_uploader):
    """`n` 必须真的流到 `blend()` —— 不是"收下了却不用"。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    _create(client, model="jimeng-i2i", prompt="两张合成", n=2,
            image=[_data_uri(_PNGS[0]), _data_uri(_PNGS[1])])
    client_state.coordinator.tick()

    call = fake_jimeng.of("blend")[0]
    assert call["count"] == 2, "请求的 n 没有传到 blend"
    assert len(call["image_uris"]) == 2, "两张垫图要一起带上"


def test_reusing_an_upstream_asset_skips_download_and_upload(
        client, client_state, fake_jimeng, fake_uploader):
    """🔴 拿**上一次的产物**当这次输入时，一次下载、一次上传都不该发生。

    这是"链式任务"最常见的用法：A 出图 → B 用 A 的图再加工。
    在此之前我们每次把同一份字节**从上游下回来再传回上游**，
    实测那两段是 ~0.5s + ~1.3s。产物 URL 里的 `<space>/<32位hex>` 就是
    草稿要的 `image_uri`，直接复用即可。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    key = "2" * 32
    prod = (f"https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/{key}"
            f"~tplv-tb4s082cfz-aigc_resize:0:0.png?x-signature=x")

    _create(client, model="jimeng-i2i", prompt="再改一次", image=[prod])
    client_state.coordinator.tick()

    assert fake_uploader.uploads == [], "可复用的资产不该再上传一次"
    assert fake_jimeng.of("blend")[0]["image_uris"] == [f"tos-cn-i-tb4s082cfz/{key}"]


def test_mixed_reusable_and_foreign_refs_keep_order(
        client, client_state, fake_jimeng, fake_uploader):
    """一批里**混着**"可复用资产"与"需要搬运的外链"时，顺序必须照旧。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    key = "3" * 32
    prod = (f"https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/{key}"
            f"~tplv-x.png?sig=1")
    foreign = _data_uri(_PNGS[0])

    _create(client, model="jimeng-i2i", prompt="混合", image=[prod, foreign])
    client_state.coordinator.tick()

    assert len(fake_uploader.uploads) == 1, "只有那张外链需要搬运"
    sent = fake_jimeng.of("blend")[0]["image_uris"]
    assert sent[0] == f"tos-cn-i-tb4s082cfz/{key}", "第 1 张应是复用的资产"
    assert sent[1] == fake_uploader.uri_for(fake_uploader.uploads[0]), "第 2 张是刚上传的"


def test_omitted_n_defaults_to_the_smallest_allowed_count(
        client, client_state, fake_jimeng, fake_uploader, service):
    """🔴 **不传 `n` ⇒ 取该模型的最小合法值**，**不采用上游的 `default_generate_count`**。

    实测上游各家默认不同（5.0 Pro 默认 **2**、5.0 Lite 默认 **4**）——
    照它的默认走，调用方按"1 张"的预期会收到 2~4 张的账单。**默认必须是最省的那个。**

    这条刻意把选项设成 `(2, 4)`（**最小值不是 1**）：只有真的取 `min(opts)`
    才能通过，写死 1 会当场翻车。同时这种情况**不该**留"已吸附"告警 ——
    调用方什么都没要求，回一句"把你的 1 改成 2"只会让人困惑。
    """
    service.cfg.options = (2, 4)          # 假能力表：最小值 ≠ 1
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x")     # 刻意不带 n
    client_state.coordinator.tick()

    rec = service.store.get(tid)
    assert rec.n == 2, f"默认应取最小合法值 2，实得 {rec.n}"
    assert not [d for d in rec.degradations if "吸附" in d], \
        f"没请求张数却报了吸附：{rec.degradations}"
    assert fake_jimeng.of("submit")[0]["count"] == 2, "默认值没有真的传给上游"


def test_explicit_n_is_still_snapped_onto_the_model_options(
        client, client_state, fake_jimeng, fake_uploader, service):
    """显式传了 `n` 时，行为不变：吸附到合法值**并留痕**（这条是既有契约，别改坏）。"""
    service.cfg.options = (1, 2, 4)
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=3)
    client_state.coordinator.tick()

    rec = service.store.get(tid)
    assert rec.n == 2, "3 不在选项里 ⇒ 吸附到不超过它的最大值 2"
    assert any("吸附" in d for d in rec.degradations), "吸附必须留痕"


def test_post_edit_family_also_carries_the_count():
    """🔴 后编辑族（hd / pro-hd / outpaint / detail）**同样**吃 `gen_option.gen_count`。

    `gen_option` 是**组件级**字段（挂在组件的 `abilities` 下、与具体 ability 平级），
    后编辑族也属 `image_base_component` ⇒ 同一个位置。

    原先这里没传，于是"**扩图固定出 4 张**"看起来像上游的硬约束 ——
    其实是我们没传张数、上游用了自己的默认值。
    这与 blend 上踩过的坑**完全同型**：把"我们没传"误读成"上游不支持"。
    """
    from app.upstream.jimeng.client import build_post_edit_draft

    for tool in ("normal_hd", "pro_hd", "outpaint", "detail"):
        d = json.loads(build_post_edit_draft(tool=tool, image_uri="tos-cn-i-x/a",
                                             count=2))
        ab = d["component_list"][0]["abilities"]
        assert "gen_option" in ab and len(ab) >= 3, f"{tool} 缺 gen_option"
        assert ab["gen_option"]["gen_count"] == 2, tool
        assert ab["gen_option"]["generate_all"] is False, tool


def test_post_edit_item_reference_form_has_no_origin_image():
    """🔴 输入图第三种承载：`item_id`+`origin_history_id`（**不带 origin_image**）。

    2026-09-20 细节修复真实 UI 抓包里唯一可见的形态（UPSTREAM.md §9.1）；
    2026-09-20 路 A 探针用它**一次真跑成功**（此前两次"单组件+origin_image"
    都 generate_failed 且计费）。这条用例把"引用形态不带 origin_image"钉死，
    防止将来有人把该校验改回"必须有输入图"。
    """
    from app.upstream.jimeng.client import build_post_edit_draft

    d = json.loads(build_post_edit_draft(
        tool="detail", item_id=7687536700452588862,
        origin_history_id=44853933660428, count=1))
    pedit = d["component_list"][0]["abilities"]["super_resolution"]["postedit_param"]
    assert "origin_image" not in pedit, "引用形态不得带 origin_image"
    assert pedit["generate_type"] == 2
    assert pedit["item_id"] == 7687536700452588862
    assert pedit["origin_history_id"] == 44853933660428
    # 反向：什么输入都不给必须报错，不能静默构造空输入草稿
    import pytest
    from app.upstream.jimeng.client import JimengParamError
    with pytest.raises(JimengParamError):
        build_post_edit_draft(tool="normal_hd", count=1)


def test_hd_passes_the_requested_count_to_edit(client, client_state,
                                               fake_jimeng, fake_uploader):
    """`n` 对后编辑族也要**真的流到上游** —— 不再"只接受 1"。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    prod = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
            + "5" * 32 + "~tplv-x.png?sig=1")

    _create(client, model="jimeng-hd", image=[prod], n=2)
    client_state.coordinator.tick()

    call = fake_jimeng.of("edit")[0]
    assert call["count"] == 2, "请求的 n 没有传到 edit"
    assert call["tool"] == "normal_hd"


def test_model_without_declared_count_options_is_flagged(
        client, client_state, fake_jimeng, fake_uploader, service):
    """🔴 模型**不声明**张数选项时，必须留痕 —— 不能拿兜底值把"不可控"伪装成"可控"。

    实测真实存在这种模型（`..._v30l_art_fangzhou:general_v3.0_18b` 的
    `generate_count_options` 为 `null`）。这种模型上 `gen_count` 传了也白传。

    关键点（也是这条用例存在的理由）：`count_options()` 会**退回冻结快照、永远非空**，
    所以**看它根本发现不了**这种情况；只有不做兜底的 `count_options_declared()`
    才答得上"能不能控"。下面两个断言一起把这件事钉死。
    """
    service.cfg.declared = None            # 服务端没声明……
    assert service.cfg.count_options("x")  # ……但兜底查询仍会给出值（这就是陷阱）
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=4)
    client_state.coordinator.tick()

    rec = service.store.get(tid)
    assert any("未声明张数选项" in d for d in rec.degradations), \
        f"不声明张数却没留痕：{rec.degradations}"
    assert any("可能不生效" in d for d in rec.degradations), "要把后果说清楚"


def test_image_count_has_a_global_ceiling(client, service):
    """全局合理性上限：一次塞 6 张直接拒（真正的额度由能力声明决定）。

    ⚠️ `jimeng-i2i` 的按能力上限（4）与这里全局上限相等，所以"i2i 超限"这条
    **不可单独到达** —— 全局那道先拦。两道并存的用意：全局那道挡住"一次塞几百个
    URL"，免得在解析出能力之前就先做昂贵的图片校验；能力那道负责精确额度。
    """
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "x",
                                "image": [_data_uri(b) for b in _PNGS] * 2},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "最多 4 张" in msg
    assert "jimeng-i2i" in msg, "要指路：只有 i2i 支持多张垫图"


@pytest.mark.parametrize("model,expect_tool", [
    ("jimeng-hd", "normal_hd"),
    ("jimeng-pro-hd", "pro_hd"),
    ("jimeng-outpaint", "outpaint"),
])
def test_post_edit_family_routes_to_the_right_tool(client, client_state,
                                                   fake_jimeng, service, settings,
                                                   model, expect_tool):
    """后编辑四工具**共享同一端点**，差异只在 `generate_type` 字符串。

    路由选错工具 = 按 pro-hd（实扣 1）计费却以为在做超清（hd 实扣 0）（或反之）。
    """
    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    data_uri = ("data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    _create(client, model=model, image=[data_uri])
    client_state.coordinator.tick()
    edits = fake_jimeng.of("edit")
    assert len(edits) == 1
    assert edits[0]["tool"] == expect_tool


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_generate_failed_is_reported_as_failure_with_reason(client, client_state,
                                                            fake_jimeng):
    """🔴「被接受」≠「能跑通」：上游 `ret=0` 之后任务仍可能生成失败，**而且照样计费**。"""
    fake_jimeng.states = [failed_state("generate_failed boom")]
    tid = _create(client)
    client_state.coordinator.tick()
    client_state.coordinator.tick()

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure"
    assert "generate_failed" in body["error"]["message"]
    assert "计费" in body["error"]["message"], "要提醒调用方这次失败是花过钱的"


def test_retryable_submit_error_requeues_without_failing(client, client_state,
                                                         fake_jimeng, service):
    """限流可退避重试 ⇒ 回队列，**不判死**。"""
    fake_jimeng.fail_submit = JimengRateLimitError("upstream busy", code=2020)
    tid = _create(client)
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "queued", "可重试的错误不该让任务直接失败"
    assert rec.attempts == 1


def test_retryable_error_stops_after_max_attempts(client, client_state,
                                                  fake_jimeng, service):
    """重试一个**付费**动作的成本是非线性的 ⇒ 必须封顶。"""
    fake_jimeng.fail_submit = JimengRateLimitError("upstream busy", code=2020)
    tid = _create(client)
    for _ in range(5):
        client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_rate_limited"


def test_quota_error_fails_immediately_and_does_not_retry(client, client_state,
                                                          fake_jimeng, service):
    """额度耗尽重试一万次也不会变好，而且每次都是付费动作。"""
    fake_jimeng.fail_submit = JimengQuotaError("no credits", code=1006)
    tid = _create(client)
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_quota_exhausted"
    assert fake_jimeng.of("submit"), "至少试过一次"


def test_risk_error_enters_cooldown_and_fails(client, client_state, fake_jimeng,
                                             service, settings):
    """风控命中 ⇒ 失败 + 进入冷却；冷却期内**不再打上游**（持续施压会延长标记）。"""
    fake_jimeng.fail_submit = JimengRiskError("punish", code=1018)
    tid = _create(client)
    client_state.coordinator.tick()
    assert service.store.get(tid).status == "failure"
    assert service.gate.stats()["cooling_for"] > 0

    before = len(fake_jimeng.calls)
    _create(client, prompt="另一个任务")
    client_state.coordinator.tick()
    assert len(fake_jimeng.calls) == before, "冷却期内不该再调上游"


def test_auth_error_is_a_deployment_problem_not_the_callers(client, client_state,
                                                            fake_jimeng, service):
    from app.upstream.jimeng import JimengAuthError

    fake_jimeng.fail_submit = JimengAuthError("session expired", code=1015)
    tid = _create(client)
    client_state.coordinator.tick()
    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure"
    # 上游凭据失效是**部署问题** ⇒ 提示文案要指导"联系服务方"，而不是让调用方改参数
    assert "JIMENG_SESSIONID" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 并发上限：按**库计数**，不是进程内信号量
# ---------------------------------------------------------------------------


def test_concurrency_limit_is_enforced_from_the_store(client, client_state,
                                                      fake_jimeng, service,
                                                      settings):
    """上限判据是 `count(in_progress)` —— 重启安全、跨 worker 也正确。

    用进程内信号量的话，重启后信号量归零而库里的任务还在上游跑，会**超发**。
    """
    fake_jimeng.states = []          # 永远非终态 ⇒ 一直占着 in_progress
    for i in range(3):
        _create(client, prompt=f"task-{i}")
    client_state.coordinator.tick()
    assert service.store.count_by_status("in_progress") == settings.jm_concurrency
    assert fake_jimeng.of("submit"), "至少提交一个"
    # 并发 1 时后续 tick 不该再提交第二个
    before = len(fake_jimeng.of("submit"))
    client_state.coordinator.tick()
    assert len(fake_jimeng.of("submit")) == before


def test_gate_can_block_dispatch_without_consuming_attempts(service, client,
                                                            client_state,
                                                            fake_jimeng):
    """闸门拒绝（冷却中）时**不消耗重试次数** —— 那还没轮到上游说话。"""
    tid = _create(client)
    service.gate.mark_risk_hit()      # 直接把闸门打进冷却
    rec_before = service.store.get(tid).attempts
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "queued"
    assert rec.attempts == rec_before, "闸门拦下的不该记作一次失败尝试"
    assert not fake_jimeng.of("submit")


def test_gate_raises_risk_control_error_when_cooling(service):
    service.gate.mark_risk_hit()
    with pytest.raises(RiskControlError) as e:
        service.gate.acquire()
    assert e.value.retry_after and e.value.retry_after > 0
    assert e.value.retryable is False, "风控不可重试（重试会延长标记）"


# ---------------------------------------------------------------------------
# 超时看门狗
# ---------------------------------------------------------------------------


def test_watchdog_expires_a_task_that_never_reaches_terminal(service, client,
                                                             client_state,
                                                             fake_jimeng):
    """没人查就永远卡在 in_progress —— 惰性方案下看门狗永不触发，所以需要后台协调器。"""
    fake_jimeng.states = []
    tid = _create(client)
    client_state.coordinator.tick()
    assert service.store.get(tid).status == "in_progress"

    service.settings.task_timeout = -1.0     # 立刻超时
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_timeout"


# ---------------------------------------------------------------------------
# 选主：防重复提交 = 防重复计费
# ---------------------------------------------------------------------------


def test_lease_prevents_two_coordinators_from_dispatching_the_same_task(
        service, settings, client):
    """两个协调器同时跑时，只有一个能推进。

    没有租约的话，两个进程会同时提交同一个任务 —— 而建任务是**计费动作**。
    """
    from app.coordinator import Coordinator

    tid = _create(client, prompt="only once")
    c1 = Coordinator(service, settings, owner="c1")
    c2 = Coordinator(service, settings, owner="c2")

    c1.tick()
    rec = service.store.get(tid)
    assert rec.status == "in_progress"

    # c2 在这轮里被租约挡住；即便抢到也不该重复提交（任务已离开 queued）
    before = service.store.get(tid).upstream_submit_id
    c2.tick()
    assert service.store.get(tid).upstream_submit_id == before


# ---------------------------------------------------------------------------
# 未配凭据
# ---------------------------------------------------------------------------


def test_coordinator_skips_when_upstream_not_configured(settings, store,
                                                        fake_jimeng,
                                                        fake_uploader):
    from app.service import Service
    from app.coordinator import Coordinator

    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    svc = Service(blank, store=store, client=fake_jimeng,
                  uploader=fake_uploader, cfg=None)
    co = Coordinator(svc, blank, owner="x")
    co.tick()                       # 不该抛，也不该调上游
    assert fake_jimeng.calls == []


def test_accept_wakes_the_coordinator(client, client_state, monkeypatch):
    """受理必须**叫醒**协调器，否则这条要白等一个 tick（默认最多 1s）。

    ⚠️ 刻意**不做时序断言** —— 那类断言必然偶发（本仓踩过 1% 概率的假失败，
    根因是整秒存储的 `updated_at` 撞上亚秒间隔）。改成钉住**接线**：
    受理路径确实调用了 `wake()`。接线断了才是会静默退化的那种缺陷
    （变慢但不会报错），而"快多少"由 `Coordinator.wake` 的实现保证。
    """
    calls: list[int] = []
    monkeypatch.setattr(client_state.coordinator, "wake",
                        lambda: calls.append(1))

    _create(client)

    assert calls == [1], "受理路径没有叫醒协调器 —— 会退化成等下一个 tick"


def test_wake_is_harmless_and_idempotent(service, settings):
    """`wake()` 纯属优化：多叫几次、或没人听，都不该有任何副作用。

    丢掉一次唤醒最多慢一个 tick —— "该派发谁"始终由库里的状态决定，
    不由"谁叫过它"决定。这条钉住这个性质，免得有人把状态塞进唤醒信号里。
    """
    from app.coordinator import Coordinator

    co = Coordinator(service, settings, owner="x")
    co.wake()
    co.wake()
    co.wake()
    assert co.stats()["running"] is False      # 没 start 过，叫醒也不该把它跑起来
    co.tick()                                  # 没配置上游 ⇒ 直接返回，不抛


def test_prewarm_fetches_model_config_and_upload_token_once(service, settings,
                                                            fake_jimeng):
    """启动预热要把两次**只读**往返提前做掉（模型表 + 上传 STS）。

    这两样每个进程只需一次，但原先都是"第一个任务才付"（实测 ~200ms + ~440ms）。
    它只影响重启后第一个任务的延迟 —— 而那恰好是部署完立刻试用的一刻。
    """
    from app.coordinator import Coordinator

    svc = settings.replace(jimeng_sessionid="s", jimeng_cookie="")
    obj = Service(svc, store=service.store, client=fake_jimeng,
                  uploader=fake_jimeng, cfg=None)
    # 用替身记账，避免真的构造上游往返
    calls: list[str] = []

    class _Cfg:
        def snapshot(self):
            calls.append("model_config")
            return object()

    class _Up:
        def token(self):
            calls.append("upload_token")
            return {"k": "v"}

    obj.cfg = _Cfg()          # type: ignore[assignment]
    obj.uploader = _Up()      # type: ignore[assignment]
    Coordinator(obj, svc, owner="x")._prewarm()

    assert calls == ["model_config", "upload_token"], "两样都要预热，且各一次"


def test_prewarm_failure_never_breaks_startup(service, settings):
    """预热是**优化**，绝不能成为启动的前提条件 —— 失败只告警。"""
    from app.coordinator import Coordinator

    svc = settings.replace(jimeng_sessionid="s", jimeng_cookie="")

    class _Boom:
        def snapshot(self):
            raise RuntimeError("boom")

        def token(self):
            raise RuntimeError("boom")

    obj = Service(svc, store=service.store, client=None, uploader=_Boom(), cfg=None)
    obj.cfg = _Boom()          # type: ignore[assignment]
    Coordinator(obj, svc, owner="x")._prewarm()      # 不该抛

    # 没配置上游时整段跳过（省得在无凭据部署里空跑）
    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    obj2 = Service(blank, store=service.store, client=None, uploader=None, cfg=None)
    Coordinator(obj2, blank, owner="x")._prewarm()   # 也不该抛


def _tick_until_terminal(client_state, times: int = 2) -> None:
    """推进协调器 `times` 次。

    ⚠️ **一次 `tick()` 只完成"建任务"**，任务翻终态要再走一轮（假上游是
    按 `states` 列表逐次吐状态的）—— 而扣费告警挂在"翻终态那一刻"，
    所以只 tick 一次是**等不到告警**的（我第一版就是这么写错的）。
    """
    for _ in range(times):
        client_state.coordinator.tick()


def _capture_warnings(fn):
    """跑 `fn()` 并返回 loguru 上 WARNING 级以上的消息列表。

    ⚠️ 不能用 pytest 的 `caplog`：本仓的 `OBS`（`app/observability.py`）走的是
    **loguru**，而 `caplog` 挂在 stdlib `logging` 上 —— loguru 默认不往那边转发，
    结果是"测试永远抓不到告警"（会写成一条**永远通过**的空断言，比不写更糟）。
    """
    from loguru import logger

    got: list[str] = []
    sink_id = logger.add(lambda m: got.append(m.record["message"]), level="WARNING")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return got

def test_credits_warning_fires_for_unmeasured_capability(client, client_state,
                                                         fake_jimeng, fake_uploader):
    """🔴 **实扣未实测的能力**，任务成功时必须有一条 WARNING（别等翻账单才发现）。

    ⚠️ 样本换过两次（每次都是因为"未测"被证伪）：
      · 最初用 `jimeng-i2i`（当时按一条 `amount=12` 的记录误记"实扣 12"）——
        账号所有者确认 i2i 免费 ⇒ 改成 0（不再适合当样本）；
      · 随后用 `jimeng-pro-hd` —— 2026-09-23 查到它的实扣记录
        （`智能超清2.0-2k amount=1`，submit_id 归属核实到 9-22 的 pro-hd 任务）
        ⇒ 已改为 `credits_measured=1`（走"实测会扣"分支，仍会报警，
        但不再是"未测"样本）。
    **现行样本 = `jimeng-outpaint`**（实扣未对账 ⇒ 无法排除扣费 ⇒ 必须报警）。
    ⚠️ 区分"未测"（`None`，必须报）与"实测 0"（免费，不报）—— 后者由
    `test_credits_warning_is_silent_for_measured_free_capability` 守着。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    prod = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
            + "7" * 32 + "~tplv-x.png?sig=1")
    _create(client, model="jimeng-outpaint", image=[prod])

    msgs = _capture_warnings(lambda: _tick_until_terminal(client_state))

    assert any("credits consumed" in m for m in msgs), f"未实测能力的任务没有告警：{msgs}"


def test_credits_warning_is_silent_for_measured_free_capability(
        client, client_state, fake_jimeng, fake_uploader):
    """⚠️ **实测免费的能力不该告警** —— 否则天天响，真扣费那次就没人看了。

    `jimeng-hd` 实测免费（`credits_measured=0`，余额读数一直没变），
    而回执照样报 forecast 9 ⇒ 这条专门钉住"不要按 forecast 判"。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    prod = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
            + "8" * 32 + "~tplv-x.png?sig=1")
    _create(client, model="jimeng-hd", image=[prod])

    msgs = _capture_warnings(lambda: _tick_until_terminal(client_state))

    assert not [m for m in msgs if "credits consumed" in m], \
        f"实测免费的能力不该告警，却报了：{msgs}"


# ---------------------------------------------------------------------------
# GET 也要鉴权（2026-09-23 收紧）以及**刻意不放宽**的边界
# ---------------------------------------------------------------------------

def _a_terminal_task(client, client_state, fake_jimeng, fake_uploader):
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client, model="jimeng-t2i", prompt="x")
    _tick_until_terminal(client_state)
    return tid


def test_get_task_without_authorization_is_401(client, client_state,
                                               fake_jimeng, fake_uploader):
    """🔴 **查任务必须带 Bearer** —— 2026-09-23 收紧了"`task_id` 即凭据"的旧口径。

    旧口径的理由是"id 是不可猜的 128 位随机值、只在受理时发给带 Key 的调用方，
    所以可以当链接分享"。但它与写/删两个动作的可见范围**不一致**：
    调用方很容易以为"有 id 就能读"，而 id 一旦贴进工单/聊天记录就是泄漏产物。
    要分享产物请分享产物 `url`。
    """
    tid = _a_terminal_task(client, client_state, fake_jimeng, fake_uploader)

    r = client.get(f"{BASE}/{tid}")          # 刻意**不带** Authorization

    assert r.status_code == 401, r.text
    assert "Authorization" in r.json()["error"]["message"]


def test_get_task_with_another_valid_key_is_404(client, client_state,
                                                fake_jimeng, fake_uploader):
    """内部映射：**API Key 指纹 → 任务归属**。拿着**合法但非属主**的 Key ⇒ 404。

    "不存在"与"不属于你"合并成同一个 404 —— 区分开就等于告诉别人
    "这个 id 是存在的"，那正是枚举的前置条件（与 `DELETE` 同一口径）。
    """
    from tests.conftest import AUTH_B

    tid = _a_terminal_task(client, client_state, fake_jimeng, fake_uploader)

    ok = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert ok.status_code == 200, ok.text

    other = client.get(f"{BASE}/{tid}", headers=AUTH_B)
    assert other.status_code == 404, other.text


def test_get_task_with_missing_bearer_is_401(client, client_state,
                                            fake_jimeng, fake_uploader):
    """⚠️ **不带凭据**查任务报 401（而不是 404）—— 必须响亮，不能静默吞掉。

    2026-10-03 口径变更（sessionid 透传）：带一个"不认识的 key"**不再**是401，
    因为任何非空 Bearer 都会被当 sessionid 收下（凭据有效性由上游判断）。
    这条仍钉住"缺凭据要响亮"—— 那是调用方真写错了头。
    ⚠️ 配套的隔离语义见 `test_task_is_not_visible_to_another_credential`
    （A 的凭据看不到 B 的任务 ⇒ 404，不泄露存在性）。
    """
    tid = _a_terminal_task(client, client_state, fake_jimeng, fake_uploader)

    r = client.get(f"{BASE}/{tid}")
    assert r.status_code == 401, r.text


def test_task_is_not_visible_to_another_credential(client, client_state,
                                                   fake_jimeng, fake_uploader):
    """🔴 **别人的任务查不到**（404）—— 透传后"凭据即身份"，隔离必须成立。

    为什么这条在透传下**更重要**：Bearer 就是 sessionid，
    而 `credential_id` 是从它算出的指纹 ⇒ 换了凭据就换了身份。
    若隔离失效，A 能读到 B 的任务与产物（越权）。

    ⚠️ 期望是 **404 而非 401**：任务不存在 与 无权访问，在外部观察者眼里
    必须**无法区分**，否则404 本身就泄露了"这个 id 存在"。
    """
    tid = _a_terminal_task(client, client_state, fake_jimeng, fake_uploader)
    r = client.get(f"{BASE}/{tid}",
                   headers={"Authorization": "Bearer someone-elses-sessionid"})
    assert r.status_code == 404, r.text


def test_delete_still_requires_the_key(client, client_state,
                                      fake_jimeng, fake_uploader):
    """🔴 `DELETE` **不放宽** —— 否则拿到一个 id 就能把别人的任务删了。"""
    tid = _a_terminal_task(client, client_state, fake_jimeng, fake_uploader)

    assert client.delete(f"{BASE}/{tid}").status_code == 401


def test_list_still_requires_the_key(client, service):
    """🔴 **列表**不放宽 —— 否则可以拿 id 枚举别人的任务（id 本来就不可枚举）。"""
    assert client.get(BASE).status_code == 401


# ---------------------------------------------------------------------------
# 上传的限流退避（多张并发时"一张被限流 = 整批失败"的那个缺口）
# ---------------------------------------------------------------------------

def _a_blob(settings):
    from app.media import load_one
    return load_one(_data_uri(_PNGS[0]), settings)


def test_transfer_one_retries_on_rate_limit(service, fake_uploader, monkeypatch,
                                            settings):
    """🔴 上传被限流要**退避重试**，而不是把整批垫图拖垮。

    多张时走 `Executor.map` —— **任何一个异常都会冒泡**，
    所以没有这一层的话，"某一张被限流"就等于**整单失败**。
    """
    calls = {"n": 0}
    real = fake_uploader.upload

    def flaky(data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JimengRateLimitError("限流", retry_after=0.01)
        return real(data)

    monkeypatch.setattr(fake_uploader, "upload", flaky)

    uri, _notes, _size = service._transfer_one(_a_blob(settings))

    assert calls["n"] == 2, f"应该重试一次后成功，实际调了 {calls['n']} 次"
    assert uri, "重试成功后要拿到 uri"


def test_transfer_one_does_not_retry_on_risk_control(service, fake_uploader,
                                                     monkeypatch, settings):
    """⚠️ 风控（`retryable=False`）**不许重试** —— 持续施压只会延长标记。

    这条同样重要：把"别再打"当成"等会儿再打"，是能把账号打坏的反模式。
    """
    calls = {"n": 0}

    def boom(data):
        calls["n"] += 1
        raise JimengRiskError("命中风控")

    monkeypatch.setattr(fake_uploader, "upload", boom)

    with pytest.raises(JimengRiskError):
        service._transfer_one(_a_blob(settings))

    assert calls["n"] == 1, f"风控不该重试，实际调了 {calls['n']} 次"


def test_content_review_failure_is_reported_as_policy_error(
        client, client_state, fake_jimeng, fake_uploader):
    """🔴 内容审核失败要按**内容审核**报，**不能报成"上游故障"**。

    实测真因（用户抓包）：`status=30`（通用的"生成失败"）
    + `fail_code=2038`（InputTextRisk），
    `fail_starling_message` = "你输入的文字不符合平台规则，请修改后重试"。

    原先只看 `status in (10, 40)` ⇒ 这条被归成 `upstream_unavailable`
    ⇒ **调用方以为"上游故障、可以重试"，而它必然再被拒**（还可能每次都计费）。
    """
    from app.errors import ContentPolicyError
    from app.upstream.jimeng.client import TaskState

    failed = TaskState(
        submit_id="upstream-submit-id", status=30, status_name="generate_failed",
        finished=True, failed=True, fail_code=2038,
        failed_reason="web_text_violates_community_guidelines_toast "
                      "你输入的文字不符合平台规则，请修改后重试")
    fake_jimeng.states = [submitted_state(), failed]

    tid = _create(client, model="jimeng-t2i", prompt="某段违规文本")
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure", body
    assert body["error"]["type"] == ContentPolicyError.err_type, body["error"]
    assert "内容审核" in body["error"]["message"], body["error"]
    assert "2038" in body["error"]["message"], "要把上游 fail_code 带上，方便排查"


def test_generic_generation_failure_is_not_mislabelled_as_policy(
        client, client_state, fake_jimeng, fake_uploader):
    """⚠️ 反向：**没有**安全码的失败仍应归"上游故障"，别一律当审核（会误导排查）。"""
    from app.errors import ContentPolicyError
    from app.upstream.jimeng.client import TaskState

    other = TaskState(submit_id="upstream-submit-id", status=30,
                      status_name="generate_failed", finished=True, failed=True,
                      failed_reason="内部错误", fail_code=2002)
    fake_jimeng.states = [submitted_state(), other]

    tid = _create(client, model="jimeng-t2i", prompt="x")
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["error"]["type"] != ContentPolicyError.err_type, body["error"]


def test_short_delivery_is_declared_not_silent(client, client_state,
                                               fake_jimeng, fake_uploader):
    """🔴 上游**少出图**时必须写进 `degradations` —— 不许静默按少的交付。

    上游自己维护 `total_image_count` / `finished_image_count`
    （实测 4 张那条是 `total=4, finished=1`，排在队列里慢慢出）。
    终态若两者对不上，调用方会以为"要的 n 张都在里面" ⇒ 必须如实标注。
    """
    from app.upstream.jimeng.client import GeneratedImage, TaskState

    short = TaskState(submit_id="upstream-submit-id", status=50,
                      status_name="success", finished=True, failed=False,
                      total=3, finished_count=1)
    # **有 1 张**（够"≥1 张即成功"），但上游说总共有 3 张
    short.images = [GeneratedImage(url="https://cdn/a.png",
                                   width=1024, height=1024)]
    fake_jimeng.states = [submitted_state(), short]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=3)
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    degs = body.get("degradations") or []
    # ① 数量补齐到请求的 n（契约上的数量不因上游抖动而变）
    assert len(body["data"]) == 3, f"应补齐到 3 个 url，实得 {len(body['data'])}"
    # ② 第 2、3 张是复用第 1 张（同一个 url）
    urls = [x["url"] for x in body["data"]]
    assert urls[0] == urls[1] == urls[2] == "https://cdn/a.png", urls
    # ③ **必须写明有几张是重复的** —— 不假装是新图
    assert any("重复" in d and "2" in d for d in degs), f"没标注重复张数：{degs}"


def test_full_delivery_has_no_shortfall_note(client, client_state,
                                             fake_jimeng, fake_uploader):
    """⚠️ 反向：`finished == total` 时**不许**冒出这条降级（否则就成了噪音）。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x")
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    degs = body.get("degradations") or []
    assert not [d for d in degs if "只出了" in d], f"不该有的少给降级：{degs}"


def test_zero_images_success_is_delivered_as_failure(client, client_state,
                                                     fake_jimeng, fake_uploader):
    """🔴 **"至少有 1 张输出"是成功的最低线**（用户口径）。

    终态 `status=50` 却**零产物**时，报 `success` + 空 `data` 就是"少到 0
    还静默"—— 调用方会拿到一个看起来成功、实际什么都没有的响应。
    """
    from app.upstream.jimeng.client import TaskState

    empty = TaskState(submit_id="upstream-submit-id", status=50,
                      status_name="success", finished=True, failed=False)
    empty.images = []
    fake_jimeng.states = [submitted_state(), empty]

    tid = _create(client, model="jimeng-t2i", prompt="x")
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure", f"零产物不该报成功：{body}"
    assert "零产物" in body["error"]["message"], body["error"]


def test_total_image_count_tracks_refs_not_outputs(client, client_state,
                                                   fake_jimeng, fake_uploader):
    """🔴 `total_image_count` 跟的是**垫图数**，不是出图张数 —— 别拿它判"少给"。

    实测（两次真实回执）：
      · 4 垫图 + 未传 n（⇒ n=1） ⇒ `total=4`
      · 3 垫图 + `n=2`            ⇒ `total=3`

    ⇒ "3 垫图 + n=2、交付 2 张"时 `finished(2) < total(3)`，
    但**我们要的就是 2 张、一张没少** —— 绝不能冒出"少给"降级。
    这条就是那个回归的门禁（判据必须是「交付 < n」而不是「finished < total」）。
    """
    from app.upstream.jimeng.client import GeneratedImage, TaskState

    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False,
                   total=3, finished_count=2)
    st.images = [GeneratedImage(url="https://cdn/a.png", width=1024, height=1024),
                 GeneratedImage(url="https://cdn/b.png", width=1024, height=1024)]
    fake_jimeng.states = [submitted_state(), st]

    # i2i 必须给输入图；用上游资产 URL（走复用，不必上传）
    prod = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
            + "9" * 32 + "~tplv-x.png?sig=1")
    tid = _create(client, model="jimeng-i2i", prompt="融合这三张", n=2,
                  image=[prod])
    _tick_until_terminal(client_state)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    urls = [x["url"] for x in body["data"]]
    assert len(urls) == 2, f"要 2 张就该给 2 张，实得 {len(urls)}"
    assert urls == ["https://cdn/a.png", "https://cdn/b.png"], "不该被改写/补齐"
    degs = body.get("degradations") or []
    assert not [d for d in degs if "只出了" in d], f"误报了少给：{degs}"


def _img(url):
    from app.upstream.jimeng.client import GeneratedImage
    return GeneratedImage(url=url, width=1024, height=1024)


def _done(urls, *, history_id="44853559987980"):
    from app.upstream.jimeng.client import TaskState
    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False,
                   history_record_id=history_id)
    st.images = [_img(u) for u in urls]
    return st


def _partial(urls, *, n_total=4, history_id="44853559987980"):
    """`status=45`（部分成功）—— 上游在问"要不要继续"的那个态。"""
    from app.upstream.jimeng.client import TaskState
    st = TaskState(submit_id="upstream-submit-id", status=45,
                   status_name="partial_success", finished=False, failed=False,
                   history_record_id=history_id, total=n_total,
                   finished_count=len(urls))
    st.images = [_img(u) for u in urls]
    return st


def _arm(fake_jimeng, monkeypatch, calls, draft='{"type":"draft","probe":1}'):
    fake_jimeng.last_draft = draft
    # 假客户端本来没有这个方法 ⇒ 必须 raising=False（否则 setattr 直接报错）
    monkeypatch.setattr(
        fake_jimeng, "continue_task",
        lambda history_id, d, *a, **k: (calls.append((history_id, d)), "cont-sid")[1],
        raising=False)


def test_short_delivery_triggers_auto_continue(client, client_state, fake_jimeng,
                                               fake_uploader, service, monkeypatch):
    """⚠️ 终态成功（50）但少给时：**只补齐，不再试续生成**。

    `action=2` 只在「待补生成」态（45）被接受（那一支已由 `_continue_partial` 接管）；
    在 50 再试必然 `ret=1002`。这条锁住"删掉的那个尾巴"不回来。
    """
    calls: list = []
    _arm(fake_jimeng, monkeypatch, calls)
    fake_jimeng.states = [submitted_state(), _done(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=3)
    _tick_until_terminal(client_state)

    # 45 分支已接管续生成 ⇒ 走到「终态成功但少给」这条路时**只补齐、不再试续**：
    # 在 50 调 `action=2` 必然 `ret=1002`（白花一次请求）。
    assert not calls, f"50 时不该再试续生成（必然 1002）：{calls}"
    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert len(body["data"]) == 3, f"该补齐到 n=3，实得 {len(body['data'])}"
    rec = service.store.get(tid)
    assert rec.status == "success", rec.status


def test_continued_batch_is_merged_without_duplicates(client, client_state,
                                                      fake_jimeng, fake_uploader,
                                                      service, monkeypatch):
    """两批产物要**合并去重** —— 续回来的图不能覆盖或重复第一批。

    n=2：第一批 1 张 ⇒ 触发续生成 ⇒ 第二批 1 张 ⇒ 凑齐 2 张、正常终态。
    """
    calls: list = []
    _arm(fake_jimeng, monkeypatch, calls)
    object.__setattr__(service.settings, "continue_enabled", True)
    fake_jimeng.states = [submitted_state(),
                          _partial(["https://cdn/a.png"], n_total=2),   # 45 ⇒ 触发续
                          _done(["https://cdn/b.png"])]                 # 续回来的第二批

    tid = _create(client, model="jimeng-t2i", prompt="x", n=2)
    _tick_until_terminal(client_state, times=3)

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    urls = [x["url"] for x in body["data"]]
    assert urls == ["https://cdn/a.png", "https://cdn/b.png"], f"两批没合并对：{urls}"
    rec = service.store.get(tid)
    assert rec.status == "success", f"凑齐后应终态成功，实得 {rec.status}"


def test_continue_is_capped_and_falls_back_to_repeats(client, client_state,
                                                      fake_jimeng, fake_uploader,
                                                      service, monkeypatch):
    """⚠️ 封顶：续不动了就**退回「用成功的图补齐」**，并写明续了几次。

    把上限打到顶（`continuations` 已是 `CONTINUE_MAX`）⇒ 这次**不许再续**，
    直接补齐 + 如实标注。**没有这条，续生成就能变成无底洞**（每次都计费）。
    """
    from app.service import CONTINUE_MAX

    calls: list = []
    _arm(fake_jimeng, monkeypatch, calls)
    fake_jimeng.states = [submitted_state(), _done(["https://cdn/a.png"])]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=3)
    service.store.patch(tid, continuations=CONTINUE_MAX)   # 先打到顶
    _tick_until_terminal(client_state)

    assert not calls, f"到顶了还续生成（会无限计费）：{calls}"
    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert len(body["data"]) == 3, "到顶后要退回补齐"
    degs = body.get("degradations") or []
    assert any("已续生成" in d and "重复" in d for d in degs), f"没写清续了几次：{degs}"


def test_partial_45_triggers_continue_instead_of_waiting(client, client_state,
                                                         fake_jimeng, fake_uploader,
                                                         service, monkeypatch):
    """🔴 `status=45`（部分成功）⇒ **立刻续生成**，而不是干等到 `TASK_TIMEOUT`。

    实测（6 组对照）：`action=2` **只在这个状态被接受**；拿已完成（50）的任务去续
    一律 `ret=1002`。这条同时锁住"4 张垫图卡 30 分钟"那个老问题不再回来 ——
    原来 45 落到"未完成 ⇒ 只刷时间"那条路上，会一直等到 30 分钟看门狗。
    """
    from app.upstream.jimeng.client import GeneratedImage, TaskState

    calls: list = []
    _arm(fake_jimeng, monkeypatch, calls)
    # 🔴 开关**默认关**（判据未定）⇒ 要测行为必须显式打开
    object.__setattr__(service.settings, "continue_enabled", True)
    partial = TaskState(submit_id="upstream-submit-id", status=45,
                        status_name="partial_success", finished=False, failed=False,
                        history_record_id="44853559987980",
                        total=4, finished_count=1)
    partial.images = [GeneratedImage(url="https://cdn/a.png", width=1024, height=1024)]
    fake_jimeng.states = [submitted_state(), partial]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=4)
    _tick_until_terminal(client_state)

    assert calls, "45（部分成功）就该续，而不是干等"
    assert calls[0][0] == "44853559987980", "要带对 history_id"
    rec = service.store.get(tid)
    assert rec.status == "in_progress", f"续生成后应回 in_progress，实得 {rec.status}"
    assert rec.continuations == 1
    assert [im["url"] for im in rec.images] == ["https://cdn/a.png"], \
        "半成品要留档，供后面合并"


def test_partial_45_without_material_still_waits(client, client_state,
                                                 fake_jimeng, fake_uploader,
                                                 service, monkeypatch):
    """⚠️ 没有续生成原料（缺草稿）时**不许硬造请求** —— 保持原行为继续等。

    （否则就是拿一个必然失败的请求去刷上游。）
    """
    from app.upstream.jimeng.client import GeneratedImage, TaskState

    calls: list = []
    monkeypatch.setattr(
        fake_jimeng, "continue_task",
        lambda *a, **k: (calls.append(a), "cont-sid")[1], raising=False)
    partial = TaskState(submit_id="upstream-submit-id", status=45,
                        status_name="partial_success", finished=False, failed=False,
                        history_record_id="44853559987980")
    partial.images = [GeneratedImage(url="https://cdn/a.png", width=1024, height=1024)]
    fake_jimeng.states = [submitted_state(), partial]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=4)   # 假客户端没有 last_draft
    _tick_until_terminal(client_state)

    assert not calls, f"没原料还硬发续生成请求：{calls}"
    rec = service.store.get(tid)
    assert rec.status in ("in_progress", "queued"), rec.status


def test_partial_45_does_nothing_when_switch_is_off(client, client_state,
                                                    fake_jimeng, fake_uploader,
                                                    service, monkeypatch):
    """🔴 **开关默认关**：45（部分成功）时**不许**自动续生成。

    原因：`action=2` 的触发判据**尚未确定**（实测「有的 history 能续、有的不能」，
    而区分条件还没找到）。判据确定前开着它 = 按一个错判据去花真实生成额度。
    关掉时的行为必须回到原样：**只等**（后续由终态补齐/看门狗收口）。
    """
    from app.upstream.jimeng.client import GeneratedImage, TaskState

    calls: list = []
    monkeypatch.setattr(
        fake_jimeng, "continue_task",
        lambda *a, **k: (calls.append(a), "cont-sid")[1], raising=False)
    partial = TaskState(submit_id="upstream-submit-id", status=45,
                        status_name="partial_success", finished=False, failed=False,
                        history_record_id="44853559987980")
    partial.images = [GeneratedImage(url="https://cdn/a.png", width=1024, height=1024)]
    fake_jimeng.states = [submitted_state(), partial]

    tid = _create(client, model="jimeng-t2i", prompt="x", n=4)
    _tick_until_terminal(client_state)

    assert not calls, f"开关是关的，竟然续生成了：{calls}"
    rec = service.store.get(tid)
    assert rec.status in ("in_progress", "queued"), rec.status
    assert (rec.continuations or 0) == 0


def test_dispatch_starvation_is_counted_not_silent(settings):
    """并发额度被在途任务占满时**必须留痕** —— 此前是静默 `return`。

    真实症状（2026-09-23 实测）：本机并发 = 1，库里躺着 1 条在途任务 ⇒
    新任务排队 **15 分钟**一动不动，而 `stats()` 里 `ticks` 一直在涨、
    别的什么都没有 —— "额度满了"这条最常见的原因**完全不可见**，
    与"上游慢"长得一模一样。故补三个计数器，并在这里钉死。
    """
    from app.coordinator import Coordinator

    class _Store:
        def __init__(self, running: int) -> None:
            self.running = running
            self.list_calls = 0

        def count_by_status(self, _status: str) -> int:
            return self.running

        def list_by_status(self, *a, **k):
            self.list_calls += 1
            return [object()]          # 有一条待派发

    class _Gate:
        def __init__(self, cooling: float) -> None:
            self.cooling = cooling

        def stats(self) -> dict:
            return {"cooling_for": self.cooling}

    class _Svc:
        def __init__(self, running: int, cooling: float = 0.0) -> None:
            self.store = _Store(running)
            self.gate = _Gate(cooling)
            self.sent: list = []

        def dispatch(self, rec) -> None:
            self.sent.append(rec)

    # ① 在途数 == 并发上限 ⇒ 不派发、且计数
    full = _Svc(running=1)
    co = Coordinator(service=full, settings=settings, owner="x")  # type: ignore[arg-type]
    co._dispatch_queued()
    assert co.dispatched == 0 and full.sent == []
    assert co.dispatch_full == 1
    assert co.stats()["dispatch_full"] == 1, "运维要能在 /stats 里看到它"
    assert full.store.list_calls == 0, "额度满了就不该再去查排队列表"

    # ② 上游冷却中 ⇒ 另一条早退路径，单独计数
    cool = _Svc(running=0, cooling=600.0)
    co2 = Coordinator(service=cool, settings=settings, owner="x")  # type: ignore[arg-type]
    co2._dispatch_queued()
    assert co2.dispatch_cooling == 1 and co2.dispatched == 0
    assert cool.store.list_calls == 0

    # ③ 额度够 ⇒ 真派发，计数为正（对照组，防止"计数器永远不涨"也算通过）
    free = _Svc(running=0)
    co3 = Coordinator(service=free, settings=settings, owner="x")  # type: ignore[arg-type]
    co3._dispatch_queued()
    assert len(free.sent) == 1 and co3.dispatched == 1
    assert co3.dispatch_full == 0 and co3.dispatch_cooling == 0
