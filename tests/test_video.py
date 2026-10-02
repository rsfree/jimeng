#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文生视频（jimeng-t2v / Seedance t2v）链路用例 —— **零真实上游调用**。

三条主线：
1. **提交侧逐字段对抓包**（2026-09-20 实抓：Seedance 4.0 Mini，720p×4s）；
2. **计费档位白名单**：没有抓包依据的 (resolution, duration) 一律拒绝 ——
   benefit_type/amount 写错 = 按错档位扣积分；
3. **全链路接线**：受理 → 协调器派发（假上游记录收到过什么）→ 轮询 → 响应形状。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from app.store import TaskStore
from app.upstream.jimeng import (
    DEFAULT_VIDEO_MODEL,
    JimengClient,
    JimengParamError,
    build_video_draft,
    parse_task,
    resolve_video_commerce,
)

from conftest import AUTH, ok_state


# ---------------------------------------------------------------------------
# 草稿构造
# ---------------------------------------------------------------------------


def test_build_video_draft_matches_capture():
    """草稿结构逐字段对 2026-09-20 实抓（t2v）。"""
    metrics = {"k": "v"}
    draft = json.loads(build_video_draft(
        prompt="iphone100", resolution="720p", duration_ms=4000,
        aspect_ratio="16:9", seed=1671729512, metrics=metrics))
    assert draft["type"] == "draft"
    assert draft["is_from_tsn"] is True
    comp = draft["component_list"][0]
    assert comp["type"] == "video_base_component"          # 不是 image_base_component
    assert comp["generate_type"] == "gen_video"
    assert comp["process_type"] == 1
    assert comp["id"] == draft["main_component_id"]
    abilities = comp["abilities"]
    gen = abilities["gen_video"]
    inp = gen["text_to_video_params"]["video_gen_inputs"][0]
    assert inp["prompt"] == "iphone100"
    assert inp["video_mode"] == 2
    assert inp["fps"] == 24
    assert inp["duration_ms"] == 4000
    assert inp["resolution"] == "720p"
    assert inp["idip_meta_list"] == []
    assert gen["text_to_video_params"]["video_aspect_ratio"] == "16:9"
    assert gen["text_to_video_params"]["seed"] == 1671729512
    assert gen["text_to_video_params"]["model_req_key"] == DEFAULT_VIDEO_MODEL
    # video_task_extra 是 metrics 的原样复本（字符串）
    assert json.loads(gen["video_task_extra"]) == metrics
    # 🔴 视频草稿**没有张数字段**（实抓确认无 gen_option）
    assert "gen_option" not in abilities


def test_build_video_draft_requires_prompt():
    with pytest.raises(JimengParamError):
        build_video_draft(prompt="  ")


# ---------------------------------------------------------------------------
# 计费档位白名单
# ---------------------------------------------------------------------------


def test_resolve_video_commerce_whitelist():
    # 🔴 计费档位**逐模型**（三份 UI 抓包）；amount = 输出秒 + 输入视频秒
    assert resolve_video_commerce("dreamina_seedance_40_mini", "720p", 4) == \
        ("seedance_20_mini_720p_output_5s", 4)
    assert resolve_video_commerce("dreamina_seedance_40_mini", "720p", 5,
                                  input_video_s=10.35) == \
        ("seedance_20_mini_720p_output_5s", 15.35)
    assert resolve_video_commerce("dreamina_seedance_40_vision", "720p", 5) == \
        ("dreamina_seedance_20_fast_5s", 5)
    assert resolve_video_commerce("dreamina_seedance_40_pro_vision", "720p", 5) == \
        ("seedance_20_pro_720p_output", 5)


def test_resolve_video_commerce_rejects_unverified_tier():
    # 未登记的模型/时长：拒绝构造计费字段，绝不猜
    with pytest.raises(JimengParamError):
        resolve_video_commerce("dreamina_seedance_40_mini", "720p", 8)
    with pytest.raises(JimengParamError):
        resolve_video_commerce("dreamina_seedance_40_mini", "1080p", 4)
    with pytest.raises(JimengParamError):
        resolve_video_commerce("dreamina_seedance_40_vision", "720p", 4)


def test_submit_video_dry_run_never_sends():
    """dry_run：走完构造与档位校验，但一个字节都不发（建任务=计费）。"""
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    with JimengClient(sessionid="s", transport=httpx.MockTransport(handler)) as c:
        sid = c.submit_video("一只猫", dry_run=True)
        assert sid
        assert c.last_draft                      # 草稿仍可取回（审计用）
    assert calls == []                           # 🔴 零上游往返


def test_submit_video_body_matches_capture():
    """真实路径（MockTransport）：请求体字段对实抓 —— extend 计费字段 /
    metrics_extra 视频专用形态 / draft_content 双重编码。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    with JimengClient(sessionid="s", workspace_id=22052346345484,
                      transport=httpx.MockTransport(handler)) as c:
        sid = c.submit_video("iphone100", resolution="720p", duration_ms=4000,
                             aspect_ratio="16:9", seed=1671729512)
        body = captured["body"]

    assert captured["url"].startswith("https://jimeng.jianying.com/mweb/v1/aigc_draft/generate")
    assert body["submit_id"] == sid
    # 计费字段：amount/benefit_type 按白名单档位
    commerce = body["extend"]["m_video_commerce_info"]
    assert commerce == {"amount": 4, "benefit_type": "seedance_20_mini_720p_output_5s",
                        "resource_id": "generate_video", "resource_id_type": "str",
                        "resource_sub_type": "aigc"}
    assert body["extend"]["m_video_commerce_info_list"] == [commerce]
    assert body["extend"]["root_model"] == DEFAULT_VIDEO_MODEL
    assert body["extend"]["workspace_id"] == 22052346345484
    assert body["http_common_info"] == {"aid": 513695}
    # draft_content 是「JSON 字符串」而不是对象（双重编码，写成对象会被 1002 拒）
    draft = json.loads(body["draft_content"])
    assert isinstance(body["draft_content"], str)
    assert draft["component_list"][0]["type"] == "video_base_component"
    assert body["draft_content"] == c.last_draft
    # metrics_extra：视频专用形态（与图片族的 enterFrom=click 不同）
    metrics = json.loads(body["metrics_extra"])
    assert metrics["enterFrom"] == "ai_feature"
    assert metrics["functionMode"] == "omni_reference"
    assert metrics["batchNumber"] == 1
    assert metrics["originSubmitId"] == sid
    scene = json.loads(metrics["sceneOptions"])[0]
    assert scene["resolution"] == "720p"
    assert scene["videoDuration"] == 4
    assert scene["modelReqKey"] == DEFAULT_VIDEO_MODEL


# ---------------------------------------------------------------------------
# 结果解析（回包结构未实抓 —— 尽力而为，解析不到当失败）
# ---------------------------------------------------------------------------


def test_parse_task_video_item():
    """视频产物解析 —— 按 2026-09-20 真实回包形态（status=50 实抓）。"""
    node = {
        "task": {"status": 50, "submit_id": "sid"},
        "item_list": [{
            "common_attr": {"id": ITEM_ID},
            "video": {
                "video_id": "v02870realvid0001",
                "duration": 4, "duration_ms": 4000, "has_audio": False,
                "transcoded_video": {
                    "360p": {"vid": "t1", "fps": 24, "width": 640,
                             "height": 360, "video_url": "https://cdn/360.mp4"},
                    "720p": {"vid": "t2", "fps": 24, "width": 1280,
                             "height": 720, "video_url": "https://cdn/720.mp4"},
                },
            },
        }],
    }
    st = parse_task("sid", node)
    assert st.ok
    assert len(st.images) == 1
    # 取**最高分辨率**档的下载 URL
    assert st.images[0].url == "https://cdn/720.mp4"
    assert st.images[0].width == 1280 and st.images[0].height == 720
    assert st.images[0].vid == "v02870realvid0001"
    assert st.images[0].item_id == ITEM_ID
    assert st.images[0].duration_ms == 4000


def test_parse_task_video_item_takes_highest_transcode():
    node = {
        "task": {"status": 50},
        "item_list": [{
            "video": {"video_id": "v1", "duration_ms": 4000,
                      "transcoded_video": {
                          "360p": {"width": 640, "height": 360,
                                   "video_url": "https://cdn/lo.mp4"}}},
        }],
    }
    st = parse_task("sid", node)
    assert st.ok
    assert st.images[0].url == "https://cdn/lo.mp4"


def test_parse_task_video_without_url_is_not_success_payload():
    """视频项存在但解析不出 URL ⇒ 零产物（上层会按失败处理，不伪装成功）。"""
    node = {
        "task": {"status": 50},
        "item_list": [{"video": {"width": 1280}}],
    }
    st = parse_task("sid", node)
    assert st.ok
    assert st.images == []


# ---------------------------------------------------------------------------
# 能力路由
# ---------------------------------------------------------------------------


def test_resolve_video_default_and_alias():
    from app import models

    # 🔴 视频池现有两个能力（t2v/vfi）⇒ resolve 的裸默认是**歧义报错**
    #（与图片族带输入图时同一纪律）；确定性默认由 service 层给
    #（无 source_task_id ⇒ t2v，有 ⇒ vfi，见 test_vfi_end_to_end）。
    with pytest.raises(Exception, match="无法确定用哪个"):
        models.resolve(None, has_image=False, video=True)
    cap, upstream = models.resolve("文生视频", has_image=False, video=True)
    assert cap.api_id == "jimeng-t2v"
    assert upstream is None                       # 视频模型 key 由 client 携带
    cap, _ = models.resolve("jimeng-t2v", has_image=False, video=True)
    assert cap.key == "jimeng:t2v"


def test_resolve_keeps_image_pool_untouched():
    """🔴 回归门禁：视频能力加入后，图片端点的默认推导**必须不变**。"""
    from app import models

    cap, upstream = models.resolve(None, has_image=False, video=False)
    assert cap.api_id == "jimeng-t2i"
    assert upstream == models.DEFAULT_UPSTREAM_MODEL
    # 视频能力混进图片端点 = 传错了地方
    with pytest.raises(Exception, match="视频"):
        models.resolve("jimeng-t2v", has_image=False, video=False)


# ---------------------------------------------------------------------------
# 全链路（假上游）
# ---------------------------------------------------------------------------


def _ark(text: str, **extra: Any) -> dict[str, Any]:
    """方舟门面受理体：content[] + 可选档位字段（2026-09-24 起视频唯一入口）。

    🔴 方舟契约 `model` **必填**（缺了门面当场 400）—— 默认带 mini 档。
    """
    body: dict[str, Any] = {"model": "doubao-seedance-2-0-mini-260615",
                            "content": [{"type": "text", "text": text}]}
    body.update(extra)
    return body


ARK_CREATE = "/api/v3/contents/generations/tasks"


def test_video_accept_and_dispatch(app_and_client, fake_jimeng, client_state):
    """门面受理 → 协调器派发：假上游收到 submit_video，参数透传正确。"""
    _, client, _ = app_and_client
    r = client.post(ARK_CREATE, headers=AUTH,
                    json=_ark("一只猫在跳舞", model="doubao-seedance-2-0-mini-260615",
                              resolution="720p", duration=4,
                              ratio="16:9", seed=7))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id"}                    # 方舟只回一个 id
    task_id = body["id"]

    client_state.coordinator.tick()               # 第 1 轮：建任务
    calls = fake_jimeng.of("submit_video")
    assert len(calls) == 1
    call = calls[0]
    assert call["prompt"] == "一只猫在跳舞"
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000
    assert call["aspect_ratio"] == "16:9"
    assert call["seed"] == 7
    rec = client_state.service.store.get(task_id)
    assert rec.status == "in_progress"
    # rec.model 存解析后的内部能力（对外查询回 extra_json.ark_model 原样方舟名）
    assert rec.model == "jimeng-t2v"
    assert rec.duration_ms == 4000
    assert rec.aspect_ratio == "16:9"
    # 分流留痕：方舟名 → 即梦侧档位（不出现内部名）
    assert any("doubao-seedance-2-0-mini-260615" in d and "mini" in d
               for d in rec.degradations)


def test_video_defaults_and_n_degradation(app_and_client, fake_jimeng, client_state):
    """不传可选参数 ⇒ 取实抓档位；n>1 ⇒ 降级留痕（草稿无张数字段）。"""
    _, client, _ = app_and_client
    r = client.post(ARK_CREATE, headers=AUTH, json=_ark("x"))
    assert r.status_code == 200
    client_state.coordinator.tick()
    call = fake_jimeng.of("submit_video")[0]
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000
    assert call["aspect_ratio"] == "16:9"
    rec = client_state.service.store.get(r.json()["id"])
    assert rec.n == 1
    # n 已无 HTTP 入口（方舟契约没有张数字段），受理层留痕逻辑仍要在：
    from conftest import KEY_A
    cred = client_state.service.credential_of(KEY_A)
    rec2 = client_state.service.create({"prompt": "x", "n": 3},
                                       credential=cred, video=True)
    assert rec2.n == 1
    assert any("n=3" in d and "n=1" in d for d in rec2.degradations)


def test_video_rejects_unverified_tier_at_accept(client):
    """没抓包依据的档位在**受理时**就 400 —— 不进队列、不碰上游、不扣积分。"""
    r = client.post(ARK_CREATE, headers=AUTH,
                    json=_ark("x", resolution="1080p"))
    assert r.status_code == 400
    assert "抓包" in r.json()["error"]["message"]


def test_video_rejects_image_fields_on_video_endpoint_and_vice_versa(client):
    # 视频门面：size 不属于方舟契约 → 降级留痕（认得但做不到），不 400
    r = client.post(ARK_CREATE, headers=AUTH,
                    json=_ark("x", size="2048x2048"))
    assert r.status_code == 200
    # 图片端点带视频字段 ⇒ 依旧 400
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"prompt": "x", "duration": 4})
    assert r.status_code == 400


def test_video_end_to_end_success(app_and_client, fake_jimeng, client_state):
    """派发 → 轮询到成功：门面查询返回方舟形态（content.video_url）。"""
    _, client, _ = app_and_client
    fake_jimeng.states = [ok_state(["https://tos.example.com/v.mp4"], cost=4)]
    r = client.post(ARK_CREATE, headers=AUTH, json=_ark("x"))
    task_id = r.json()["id"]
    for _ in range(2):
        client_state.coordinator.tick()
    r = client.get(f"{ARK_CREATE}/{task_id}", headers=AUTH)
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "succeeded"
    assert payload["content"] == {"video_url": "https://tos.example.com/v.mp4"}
    assert "forecast_credits" in payload["usage"]


def test_models_catalog_lists_video_capability(client):
    r = client.get("/v1/models", headers=AUTH)
    items = {m["id"]: m for m in r.json()["data"]}
    # 🔴 视频族对外**一律方舟模型名**（2026-09-24 拍板）；omni/vfi 是请求形态不单列
    for mid in ("doubao-seedance-2-0-mini-260615",
                "doubao-seedance-2-0-fast-260128",
                "doubao-seedance-2-0-260128",
                "doubao-seedance-2-5-260628"):
        assert items[mid]["media"] == "video"
        assert items[mid]["id"].startswith("doubao-seedance")
    assert "jimeng-t2v" not in items
    # 诚实边界写进 notes：实跑记录与**实扣**都要在
    assert "真跑验证" in items["doubao-seedance-2-0-mini-260615"]["notes"]
    assert "端到端实跑验证" in items["doubao-seedance-2-0-fast-260128"]["notes"]
    assert "端到端实跑验证" in items["doubao-seedance-2-0-260128"]["notes"]
    # 实扣值必须报成实测（不是 None、也不是 forecast）
    assert items["doubao-seedance-2-0-fast-260128"]["credits_measured"] == 30
    assert items["doubao-seedance-2-0-260128"]["credits_measured"] == 70
    assert items["doubao-seedance-2-5-260628"]["credits_measured"] == 45


# ---------------------------------------------------------------------------
# 存储迁移（老库补列）
# ---------------------------------------------------------------------------


def test_store_readds_video_columns_for_legacy_table(db_dsn):
    """模拟老库：列被删掉（= 老版本建的表）后重连，启动期必须自动补列。

    对应的真实事故面：`create_all` 只建表不加列，老库忘 ALTER 的症状是
    `store.patch()` 报 column does not exist（只在视频任务上炸）。
    """
    eng = create_engine(db_dsn)
    TaskStore(eng)                                # 首次：建全量表
    with eng.begin() as c:
        c.execute(text("ALTER TABLE tasks DROP COLUMN duration_ms"))
        c.execute(text("ALTER TABLE tasks DROP COLUMN aspect_ratio"))
    eng.dispose()

    store = TaskStore(db_dsn)                     # 重连：必须幂等补回两列
    rec = store.put(_TaskRecord_for_test())
    patched = store.patch(rec.task_id, duration_ms=4000, aspect_ratio="16:9")
    assert patched is not None
    assert patched.duration_ms == 4000
    assert patched.aspect_ratio == "16:9"


def _TaskRecord_for_test():
    from app.store import TaskRecord
    import time
    import uuid
    return TaskRecord(task_id=f"jimeng_{uuid.uuid4().hex}", credential_id="c",
                      model="jimeng-t2v", cap_key="jimeng:t2v", status="queued",
                      prompt="x", n=1, created_at=int(time.time()),
                      updated_at=int(time.time()))


# ---------------------------------------------------------------------------
# 视频补帧（jimeng-vfi，scene=insert_frame，amount=0 免费档）
# ---------------------------------------------------------------------------

from app.upstream.jimeng import GeneratedImage, TaskState, build_video_vfi_draft  # noqa: E402

VID = "v02870g10004danqpu27dld82i49g5r0"
ITEM_ID = "7687552710358420760"
HIST = "44854324452620"


def _vfi_metrics() -> dict:
    return {"promptSource": "custom", "enterFrom": "click"}


def _video_ok_state() -> TaskState:
    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False, cost=4)
    st.images = [GeneratedImage(url="https://tos.example.com/v.mp4",
                                width=1280, height=720, format="mp4",
                                item_id=ITEM_ID, vid=VID, note="视频产物")]
    st.history_record_id = HIST
    return st


def test_build_vfi_draft_matches_capture():
    """补帧草稿逐字段对 2026-09-20 实抓：父组件原样重放 + 子组件 insert_frame。"""
    src = ('{"type":"draft","id":"src-draft","min_version":"3.0.5",'
           '"main_component_id":"parent-1","component_list":'
           '[{"type":"video_base_component","id":"parent-1",'
           '"generate_type":"gen_video","metadata":{"created_time_in_ms":"111"},'
           '"abilities":{}}]}')
    draft = json.loads(build_video_vfi_draft(
        src, prompt="iphone100", vid=VID, origin_history_id=HIST,
        item_id=ITEM_ID, resolution="720p", duration_ms=4000,
        origin_fps=24, target_fps=60, metrics=_vfi_metrics()))
    assert draft["min_version"] == "3.1.0"           # 补帧实抓是 3.1.0
    comps = draft["component_list"]
    assert len(comps) == 2
    parent, child = comps
    assert parent["id"] == "parent-1"                # 🔴 父组件原样（连 id 都不变）
    assert parent["metadata"]["created_time_in_ms"] == "111"
    assert child["parent_id"] == "parent-1"
    assert child["process_type"] == 3                # t2v 组件是 1，补帧子组件是 3
    assert child["id"] == draft["main_component_id"]
    gen = child["abilities"]["gen_video"]
    assert gen["scene"] == "insert_frame"
    inp = gen["text_to_video_params"]["video_gen_inputs"][0]
    assert inp["vid"] == VID
    assert inp["origin_history_id"] == HIST          # 字符串形态
    assert inp["lens_motion_type"] == "" and inp["motion_speed"] == ""
    assert inp["template_id"] == 0
    ins = inp["v2v_opt"]["insert_frame"]
    assert ins["enable"] is True
    assert ins["target_fps"] == 60 and ins["origin_fps"] == 24
    assert ins["duration_ms"] == 4000
    # 🔴 子组件没有 seed / video_aspect_ratio / model_req_key / priority（实抓确认）
    t2p = gen["text_to_video_params"]
    assert "seed" not in t2p and "video_aspect_ratio" not in t2p
    assert "model_req_key" not in t2p and "priority" not in t2p
    # video_ref_params：item_id / origin_history_id 是**数字**形态
    ref = gen["video_ref_params"]
    assert ref["item_id"] == int(ITEM_ID)
    assert ref["origin_history_id"] == int(HIST)
    assert ref["generate_type"] == 0
    # video_task_extra 是 metrics 复本
    assert json.loads(gen["video_task_extra"]) == _vfi_metrics()


def test_build_vfi_draft_rejects_non_video_source():
    src = '{"component_list":[{"generate_type":"generate"}]}'
    with pytest.raises(JimengParamError, match="视频"):
        build_video_vfi_draft(src, prompt="x", vid="v", origin_history_id="1",
                              item_id="2")


def test_submit_vfi_body_matches_capture():
    """补帧请求体：计费 amount=0 + metrics 指向**源任务**。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    src_draft = ('{"type":"draft","component_list":[{"id":"parent-1",'
                 '"generate_type":"gen_video"}]}')
    with JimengClient(sessionid="s", workspace_id=22052346345484,
                      transport=httpx.MockTransport(handler)) as c:
        c.submit_video_vfi(src_draft, prompt="iphone100", vid=VID,
                                 origin_history_id=HIST, item_id=ITEM_ID,
                                 source_submit_id="src-submit-1",
                                 source_item_id=ITEM_ID)
        body = captured["body"]
    assert body["draft_content"] == c.last_draft
    commerce = body["extend"]["m_video_commerce_info"]
    # 🔴 补帧免费档：amount=0（实抓），benefit_type 与 t2v 完全不同
    assert commerce["amount"] == 0
    assert commerce["benefit_type"] == "video_frame_interpolation"
    metrics = json.loads(body["metrics_extra"])
    assert metrics["originSubmitId"] == "src-submit-1"   # 指向源任务
    assert metrics["previewSubmitId"] == "src-submit-1"
    assert metrics["originId"] == ITEM_ID
    assert metrics["promptSource"] == "custom"
    assert metrics["enterFrom"] == "click"               # t2v 是 ai_feature
    scenes = json.loads(metrics["sceneOptions"])
    assert [s["scene"] for s in scenes] == ["BasicVideoGenerateButton",
                                            "VideoFrameInterpolation"]


def test_submit_vfi_dry_run_never_sends():
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    src = ('{"component_list":[{"id":"p","generate_type":"gen_video"}]}')
    with JimengClient(sessionid="s", transport=httpx.MockTransport(handler)) as c:
        c.submit_video_vfi(src, prompt="x", vid="v", origin_history_id="1",
                           item_id="2", dry_run=True)
    assert calls == []


def test_vfi_end_to_end(app_and_client, fake_jimeng, client_state):
    """t2v 成功 → 用它的产物补帧：引用三件套自动透传给假上游。

    🔴 2026-09-24 视频端点收敛到方舟门面后，vfi **没有 HTTP 入口**
    （方舟契约无补帧概念）—— 受理走 service.create 直调验证内部链路。
    """
    from conftest import KEY_A

    _, client, _ = app_and_client
    fake_jimeng.states = [_video_ok_state()]
    r = client.post(ARK_CREATE, headers=AUTH, json=_ark("iphone100"))
    src_id = r.json()["id"]
    for _ in range(2):
        client_state.coordinator.tick()               # 源任务到 success
    src = client_state.service.store.get(src_id)
    assert src.status == "success"
    assert src.images[0]["vid"] == VID and src.images[0]["item_id"] == ITEM_ID
    assert src.upstream_history_id == HIST and src.draft_json

    # 补帧受理（service 直调）：source_task_id 即路由到 vfi、prompt 沿用源
    fake_jimeng.states = [_video_ok_state()]
    cred = client_state.service.credential_of(KEY_A)
    rec = client_state.service.create(
        {"source_task_id": src_id, "target_fps": 60},
        credential=cred, video=True)
    assert rec.model == "jimeng-vfi"
    assert rec.prompt == "iphone100"                  # 沿用源任务提示词
    assert any("沿用源视频任务的提示词" in d for d in rec.degradations)

    client_state.coordinator.tick()
    call = fake_jimeng.of("submit_video_vfi")[0]
    assert call["source_draft"] == src.draft_json     # 父组件=源草稿原样重放
    assert call["vid"] == VID
    assert call["origin_history_id"] == HIST
    assert call["item_id"] == ITEM_ID
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000                # 沿用源任务档位
    assert call["target_fps"] == 60
    assert call["source_submit_id"] == "upstream-submit-id-0"


def test_vfi_requires_source(app_and_client, client_state):
    """不给 source_task_id / 源不存在 / 源未成功 —— 都在受理时 400（service 层）。"""
    from conftest import KEY_A
    from app.errors import AdapterError

    svc = client_state.service
    cred = svc.credential_of(KEY_A)
    with pytest.raises(AdapterError, match="source_task_id"):
        svc.create({"model": "jimeng-vfi", "prompt": "x"},
                   credential=cred, video=True)

    with pytest.raises(AdapterError, match="不存在"):
        svc.create({"source_task_id": "jimeng_nope", "prompt": "x"},
                   credential=cred, video=True)

    # 源任务是排队中的（非 success）⇒ 拒绝
    _, client, _ = app_and_client
    r = client.post(ARK_CREATE, headers=AUTH, json=_ark("src"))
    src_id = r.json()["id"]
    with pytest.raises(AdapterError, match="已成功"):
        svc.create({"source_task_id": src_id}, credential=cred, video=True)


# ---------------------------------------------------------------------------
# 全能参考视频（omni_reference / unified_edit_input：图+视频+音频混合参考）
# ---------------------------------------------------------------------------

from app.upstream.jimeng import build_video_omni_draft  # noqa: E402

# 1x1 PNG（走 ImageX 链路的图片素材）
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44"
    "ae426082")
IMG_REF = "data:image/png;base64," + __import__("base64").b64encode(
    PNG_1PX).decode()
VID_REF = "data:video/mp4;base64," + __import__("base64").b64encode(
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64).decode()


def _omni_metrics() -> dict:
    return {"enterFrom": "click"}


def test_build_omni_draft_matches_capture():
    """全能参考草稿逐字段对 2026-09-20 实抓（unified_edit_input）。"""
    materials = [
        {"kind": "video", "uri": "v0d870fake", "width": 864, "height": 480,
         "duration_ms": 5175},
        {"kind": "image", "uri": "tos-cn-i-x/abc", "width": 2048,
         "height": 2048, "name": "first_frame"},
        {"kind": "audio", "uri": "v02870fake", "duration_ms": 5976,
         "name": "bgm"},
    ]
    draft = json.loads(build_video_omni_draft(
        instruction="首帧，艾特音频", materials=materials,
        resolution="720p", duration_ms=5000, aspect_ratio="16:9",
        seed=2225192095, metrics=_omni_metrics()))
    assert draft["min_version"] == "3.3.9"
    assert draft["min_features"] == ["AIGC_Video_UnifiedEdit"]
    comp = draft["component_list"][0]
    inp = (comp["abilities"]["gen_video"]["text_to_video_params"]
           ["video_gen_inputs"][0])
    assert inp["prompt"] == ""                       # 🔴 照抄抓包：空
    assert inp["duration_ms"] == 5000 and inp["resolution"] == "720p"
    ue = inp["unified_edit_input"]
    ml = ue["material_list"]
    assert [m["material_type"] for m in ml] == ["video", "image", "audio"]
    assert ml[0]["video_info"]["vid"] == "v0d870fake"
    assert ml[1]["image_info"]["image_uri"] == "tos-cn-i-x/abc"
    assert ml[2]["audio_info"]["vid"] == "v02870fake"
    meta = ue["meta_list"]
    # 🔴 照抄抓包：视频素材不进 meta_list；图/音各一条 + 指令文本一条
    assert [m["meta_type"] for m in meta] == ["image", "audio", "text"]
    assert meta[0]["material_ref"]["material_idx"] == 1
    assert meta[1]["material_ref"]["material_idx"] == 2
    assert meta[2]["text"] == "首帧，艾特音频"


def test_build_omni_draft_requires_instruction_and_materials():
    with pytest.raises(JimengParamError):
        build_video_omni_draft(instruction="  ", materials=[
            {"kind": "image", "uri": "x"}])
    with pytest.raises(JimengParamError):
        build_video_omni_draft(instruction="x", materials=[])


def test_submit_omni_commerce_amount():
    """计费口径：amount = 输出秒数 + 输入视频秒数（实抓 5s+10.35s ⇒ 15.35）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    materials = [{"kind": "video", "uri": "v1"}, {"kind": "video", "uri": "v2"},
                 {"kind": "image", "uri": "tos-cn-i-x/abc"},
                 {"kind": "audio", "uri": "a1"}]
    with JimengClient(sessionid="s", workspace_id=1,
                      transport=httpx.MockTransport(handler)) as c:
        c.submit_video_omni("指令", materials=materials, resolution="720p",
                            duration_ms=5000, seed=1, input_video_s=10.35)
    commerce = captured["body"]["extend"]["m_video_commerce_info"]
    assert commerce["amount"] == 15.35
    assert commerce["benefit_type"] == "seedance_20_mini_720p_output_5s"
    metrics = json.loads(captured["body"]["metrics_extra"])
    scene = json.loads(metrics["sceneOptions"])[0]
    assert scene["hasInputVideo"] is True
    assert scene["inputVideoDuration"] == 10.35
    assert scene["materialTypes"] == [2, 2, 1, 3]    # 实抓同款顺序编码
    assert metrics["functionMode"] == "omni_reference"


def test_omni_end_to_end(app_and_client, fake_jimeng, fake_uploader,
                         fake_vod, client_state):
    """门面带 image/video/audio 素材 → 素材语义覆盖模型分流 → 全能参考。"""
    _, client, _ = app_and_client
    fake_jimeng.states = [_video_ok_state()]
    r = client.post(ARK_CREATE, headers=AUTH,
                    json={"model": "doubao-seedance-2-0-260128", "content": [
                        {"type": "text", "text": "用参考视频的构图，图片做首帧"},
                        {"type": "image_url", "image_url": {"url": IMG_REF}},
                        {"type": "video_url", "video_url": {"url": VID_REF}},
                        {"type": "audio_url", "audio_url": {"url": VID_REF}},
                    ], "duration": 5})
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]
    rec = client_state.service.store.get(task_id)
    # rec.model 存受理端解析后的内部能力（对外查询回 extra_json.ark_model 原样方舟名）
    assert rec.model == "jimeng-omni-video"
    assert any("全能参考" in d for d in rec.degradations)
    client_state.coordinator.tick()
    call = fake_jimeng.of("submit_video_omni")[0]
    mats = call["materials"]
    assert [m["kind"] for m in mats] == ["image", "video", "audio"]
    assert mats[0]["uri"].startswith("tos-cn-i-")     # 图片走 ImageX（假上传器）
    assert fake_uploader.uploads and fake_vod.uploads  # 两条上传链都走到了
    assert mats[1]["uri"].startswith("v0")            # VOD vid
    assert mats[1]["duration_ms"] == 5042             # VOD Duration 探测
    assert call["duration_ms"] == 5000
    # 🔴 计费：amount = 5s 输出 + 5.042s 输入视频（音频不计）
    assert call["input_video_s"] == 5.04


# ---------------------------------------------------------------------------
# 细节修复（jimeng-detail-fix：引用形态，与补帧同构）
# ---------------------------------------------------------------------------


def _image_ok_state() -> TaskState:
    """成功的图片任务产物（带 item_id，供引用）。"""
    st = TaskState(submit_id="up-submit-img", status=50, status_name="success",
                   finished=True, failed=False, cost=0)
    st.images = [GeneratedImage(url="https://tos.example.com/a.png",
                                width=2048, height=2048, format="png",
                                item_id="7687568450923007257")]
    st.history_record_id = "44903636599052"
    return st


def test_detail_fix_end_to_end(app_and_client, fake_jimeng, client_state):
    """图片任务成功 → source_task_id 引用修复：edit 收到 item 引用三件套。"""
    _, client, _ = app_and_client
    fake_jimeng.states = [_image_ok_state()]
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"model": "jimeng-t2i", "prompt": "一只猫"})
    src_id = r.json()["task_id"]
    for _ in range(2):
        client_state.coordinator.tick()
    src = client_state.service.store.get(src_id)
    assert src.status == "success"
    assert src.images[0]["item_id"] and src.upstream_history_id

    # 细节修复：不带 model（source_task_id 即路由）、不带 image（引用形态）
    fake_jimeng.states = [_image_ok_state()]
    r2 = client.post("/async/v1/images/generations", headers=AUTH,
                     json={"source_task_id": src_id})
    assert r2.status_code == 202, r2.text
    rec = client_state.service.store.get(r2.json()["task_id"])
    assert rec.model == "jimeng-detail-fix"
    client_state.coordinator.tick()
    call = fake_jimeng.of("edit")[0]
    assert call["tool"] == "detail"
    assert call["item_id"] == 7687568450923007257      # 数字形态
    assert call["origin_history_id"] == 44903636599052
    assert not call.get("image_uri") and not call.get("image_url")
    rec0 = client_state.service.store.get(r2.json()["task_id"])
    assert rec0 is not None and rec0.upstream_submit_id


def test_detail_fix_requires_source_and_rejects_image(client):
    """无 source_task_id ⇒ 400；贴 image（origin_image 形态）⇒ 400。"""
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"model": "jimeng-detail-fix"})
    assert r.status_code == 400
    assert "source_task_id" in r.json()["error"]["message"]
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"model": "jimeng-detail-fix", "source_task_id": "x",
                          "image": ["https://x/a.png"]})
    assert r.status_code == 400


def test_detail_fix_default_routing_kept_t2i(client):
    """🔴 回归门禁：图片端点不带 model、不带 source_task_id ⇒ 仍是 t2i。"""
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"prompt": "x"})
    assert r.status_code == 202


def test_every_capability_has_a_submit_route():
    """**注册表里每个能力都必须有提交路径** —— 这是 2026-09-23 那个真 bug 的门禁。

    症状：能力在 `models.CAPABILITIES` 里注册了、`/v1/models` 也宣告了、受理也通，
    但 `Service._submit` 没有它的分支 ⇒ 任务在**派发那一刻**炸。
    `jimeng-t2v-fast` / `jimeng-t2v-pro` 就是这么坏的（掉进后编辑族 `else`，
    在那里 `assert cap.jimeng_tool` —— 视频能力没有它）。
    更糟的是它被兜底逻辑报成"上游不可用·**可重试**"，把排障方向带偏。

    纯静态对照：改注册表 / 改路由表任一边忘了同步，这里都会红。
    """
    from app.models import CAPABILITIES, T2V_VARIANTS
    from app.service import NON_SUBMIT_CAPABILITIES, SUBMIT_ROUTES

    # 🔴 2026-10-02：能力分两类 —— 走 `_submit` 的（SUBMIT_ROUTES）
    # 与**不走**的（NON_SUBMIT_CAPABILITIES，必须显式登记原因）。
    # 两边都要覆盖：新能力漏登记任一侧都会红。
    names = {c.name for c in CAPABILITIES}
    non_submit = set(NON_SUBMIT_CAPABILITIES)   # 它是 dict（能力名 → 原因）
    assert not (non_submit - names), \
        f"登记了不存在的能力：{sorted(non_submit - names)}"
    routed = names - non_submit
    assert not (routed - SUBMIT_ROUTES), \
        f"这些能力没有提交实现：{sorted(routed - SUBMIT_ROUTES)}"
    assert not (SUBMIT_ROUTES - routed), \
        f"路由表里有注册表没有的能力（改完注册表忘删了？）：{sorted(SUBMIT_ROUTES - routed)}"

    assert T2V_VARIANTS <= SUBMIT_ROUTES, "t2v 变体必须共用同一条视频提交路径"


@pytest.mark.parametrize("ark_model,api_id", [
    ("doubao-seedance-2-0-mini-260615", "jimeng-t2v"),
    ("doubao-seedance-2-0-fast-260128", "jimeng-t2v-fast"),
    ("doubao-seedance-2-0-260128", "jimeng-t2v-pro"),
])
def test_each_t2v_variant_dispatches_with_its_own_video_model(
        app_and_client, fake_jimeng, client_state, ark_model, api_id):
    """方舟名分流：各档走**同一条**提交路径，但各带各的 `video_model`（计费按它查）。"""
    from app.models import _BY_API_ID

    _, client, _ = app_and_client
    r = client.post(ARK_CREATE, headers=AUTH,
                    json=_ark("一只猫在跳舞", model=ark_model,
                              resolution="720p", duration=5, ratio="16:9"))
    assert r.status_code == 200, r.text
    client_state.coordinator.tick()

    calls = fake_jimeng.of("submit_video")
    assert len(calls) == 1, f"{ark_model} 没走到视频提交路径：{fake_jimeng.calls}"
    assert calls[0]["model"] == _BY_API_ID[api_id].video_model
    assert calls[0]["resolution"] == "720p"
    assert calls[0]["duration_ms"] == 5000
    assert calls[0]["aspect_ratio"] == "16:9"
    rec = client_state.service.store.get(r.json()["id"])
    assert rec.status == "in_progress", rec.status
    assert rec.model == api_id


def test_unwired_capability_is_reported_as_internal_not_as_upstream(client_state):
    """接线遗漏必须报成**内部错误·不可重试**，不许伪装成"上游不可用·可重试"。"""
    from app.errors import CapabilityNotWiredError
    from app.models import Capability
    from app.store import TaskRecord

    svc = client_state.service
    cap = Capability(key="jimeng:ghost", name="ghost", title="幽灵能力（未接线）",
                     accepts_image=False, image_required=False)
    rec = TaskRecord(task_id="jimeng_ghost", credential_id="c",
                     model="jimeng-ghost", cap_key="jimeng:ghost",
                     status="queued", prompt="p")
    with pytest.raises(CapabilityNotWiredError) as e:
        svc._submit(rec, cap, [])
    assert e.value.retryable is False, "内部接线 bug 重试没有意义"
    assert e.value.err_code == "capability_not_wired"
    assert e.value.status_code == 500
