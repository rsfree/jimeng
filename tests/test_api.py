#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外契约门禁（`docs/INTERFACE.md` 的冻结部分）。

这些用例断言的是**响应体的键集与形状**，不是"值大概对"。
多一个键、少一个键、类型不对，都要红 —— 契约保真是类型级的。
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import AUTH, AUTH_B, KEY_B, failed_state, ok_state

BASE = "/async/v1/images/generations"


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_create_returns_only_a_task_id(client):
    """受理**只回一个 id**（用户明确要求）。

    多回 `status`/`created_at`/`batch_key` 之类，会让调用方以为那些字段有语义。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "飞上天",
                                "image": []}, headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id"}, f"受理响应只应有 task_id，实得 {set(body)}"
    assert body["task_id"].startswith("jimeng_")
    assert len(body["task_id"]) == len("jimeng_") + 32


def test_create_sets_location_header(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    tid = r.json()["task_id"]
    assert r.headers["location"] == f"{BASE}/{tid}"


def test_create_does_not_touch_upstream(client, fake_jimeng):
    """受理在请求内**零上游往返** —— 建任务是计费动作，交给后台协调器。"""
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    assert fake_jimeng.calls == [], f"受理阶段不该调上游，实得 {fake_jimeng.kinds()}"


def test_placeholder_model_falls_back_to_t2i(client):
    """第三方 SDK 硬编码的占位名（dall-e-3 等）不代表调用意图。"""
    r = client.post(BASE, json={"model": "dall-e-3", "prompt": "x", "image": []},
                    headers=AUTH)
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# 输入校验：全部在**发出上游请求之前**
# ---------------------------------------------------------------------------


def test_image_must_be_an_array_with_actionable_message(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "必须是**数组**" in msg or "必须是数组" in msg
    assert "[]" in msg, "报错要给出正确写法，而不是只说「错了」"


def test_multiple_input_images_are_never_silently_truncated(client):
    """核心纪律不变：**绝不许**"收下 N 张、却只用 1 张"。

    契约按能力分（2026-09-20 起）：
      · `jimeng-i2i`（图生图）—— 上游草稿里 `image_uri_list` / `image_list`
        本来就是**列表**，所以**支持多张垫图**：全部上传、全部进草稿；
      · 后编辑三族（hd / pro-hd / outpaint）—— 上游用单个 `origin_image` 承载，
        多给必须**响亮 400**，不许静默丢；
      · 全局另有一道 4 张的合理性上限（挡住"一次塞几百个 URL"）。

    换句话说：**"支持"与"拒绝"必须是明确的两种行为，不允许有第三种（忽略）**。
    """
    # ① i2i：2 张是合法的（多张垫图）
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "改海边",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 202, r.text

    # ② 后编辑族：多给仍然明确报错
    r = client.post(BASE, json={"model": "jimeng-hd",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "最多接受 1 张" in msg
    assert "不会静默忽略" in msg, "报错要说清为什么拒绝"


def test_unknown_field_is_rejected(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "foo": 1},
                    headers=AUTH)
    assert r.status_code == 400
    assert "未知字段" in r.json()["error"]["message"]


def test_known_but_unsupported_field_becomes_a_degradation(client, client_state):
    """**认得但做不到**的字段 ≠ 写错。前者进 `degradations`，后者 400。

    分不清这两者，调用方就会去查上游能力表，而真正的问题在请求体。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "watermark": True}, headers=AUTH)
    assert r.status_code == 202
    tid = r.json()["task_id"]
    got = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert got["status"] == "queued"
    assert any("watermark" in d for d in got["degradations"])


def test_capability_requiring_image_without_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-hd", "prompt": ""}, headers=AUTH)
    assert r.status_code == 400
    assert "需要输入图" in r.json()["error"]["message"]


def test_t2i_with_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "不接受输入图" in r.json()["error"]["message"]


def test_prompt_required_for_i2i(client):
    r = client.post(BASE, json={"model": "jimeng-i2i",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "需要 prompt" in r.json()["error"]["message"]


def test_bad_size_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "size": "huge"}, headers=AUTH)
    assert r.status_code == 400
    assert "2048x2048" in r.json()["error"]["message"]


def test_bad_n_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 0},
                    headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "n"


# ---------------------------------------------------------------------------
# 查询：状态与形状
# ---------------------------------------------------------------------------


def test_pending_task_returns_202_with_status(client):
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 202, "非终态必须是 202 —— 调用方据此继续轮询"
    assert r.json() == {"task_id": tid, "status": "queued"}


def test_success_body_matches_the_frozen_shape(client, client_state, fake_jimeng):
    """成功体 = `{status, data:[{url}], created, usage}`，且 `data[]` 里**只有 url**。

    2026-09-22 契约更新（用户指令）：成功态补顶层 `status: "success"` ——
    此前成功体只有 data/created/usage，是全链路唯一不带 status 的态，调用方
    只能按"有没有 data"推断终态。现五态（queued / in_progress / success /
    failure / canceled）**统一带顶层 status**。键集仍**精确相等**（防静默多键）。
    """
    fake_jimeng.states = [ok_state(["https://cdn/1.png", "https://cdn/2.png"],
                                   cost=44)]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 2},
                      headers=AUTH).json()["task_id"]

    client_state.coordinator.tick()   # 建任务
    client_state.coordinator.tick()   # 轮询到终态

    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "data", "created", "usage"}, \
        f"键集不符：{set(body)}"
    assert body["status"] == "success", "成功态必须带顶层 status=success"
    assert [d["url"] for d in body["data"]] == ["https://cdn/1.png",
                                                "https://cdn/2.png"]
    assert set(body["data"][0]) == {"url"}, "data[] 里只该有 url（与冻结契约逐字一致）"
    assert isinstance(body["created"], int)
    # 🔴 名字里带 forecast：它是上游的**预估**，不是实际扣费
    # （实测 i2i 报 55 / 实扣 12，高估 4~9 倍）
    assert body["usage"] == {"images": 2, "forecast_credits": 44}


def test_usage_omits_unknown_fields_instead_of_inventing_them(client,
                                                              client_state,
                                                              fake_jimeng):
    """上游没给 `forecast_generate_cost` 时，`credits` 键**不出现**。

    给个 0 更坏：那是在声称"本次消耗 0 积分"，而不是"不知道"。
    """
    st = ok_state(["https://cdn/1.png"])
    st.cost = None
    fake_jimeng.states = [st]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    usage = client.get(f"{BASE}/{tid}", headers=AUTH).json()["usage"]
    assert usage == {"images": 1}, "不知道的值不许编 —— 键不出现才对"
    assert "credits" not in usage and "forecast_credits" not in usage


def test_unknown_task_is_404_with_openai_error_envelope(client):
    r = client.get(f"{BASE}/jimeng_deadbeef", headers=AUTH)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "task_not_found"
    assert err["message"]


def test_cross_credential_read_is_404_and_touches_no_upstream(client, fake_jimeng):
    """跨凭证读取必须**本地拦死**，而不是丢给上游去判。

    放行到上游就是"用错的钥匙去查"，返回的 404/空无法区分
    "任务真没了"与"钥匙不对"，而且把跨凭证隔离交给了别人的实现去兜。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    before = len(fake_jimeng.calls)
    r = client.get(f"{BASE}/{tid}", headers=AUTH_B)
    assert r.status_code == 404
    assert len(fake_jimeng.calls) == before, "跨凭证读取不该产生任何上游请求"


def test_list_is_scoped_to_the_calling_key(client):
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "a"}, headers=AUTH)
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "b"}, headers=AUTH_B)
    a = client.get(BASE, headers=AUTH).json()
    assert a["total"] == 1, "列表只按服务过滤会把别人的任务列给你"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_non_terminal_task_fails_loudly(client):
    """即梦没有取消端点 ⇒ 对未终态任务的删除必须**响亮失败**。

    本地删掉只会让"还在跑并继续计费"变成看不见的事。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.delete(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 400
    assert "没有取消端点" in r.json()["error"]["message"]


def test_delete_terminal_task_removes_the_record(client, client_state, fake_jimeng):
    fake_jimeng.states = [ok_state(["https://cdn/1.png"])]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    assert client.delete(f"{BASE}/{tid}", headers=AUTH).json()["status"] == "deleted"
    assert client.get(f"{BASE}/{tid}", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_missing_key_is_401(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_missing_bearer_is_still_401(client):
    """🔴 **没有** Authorization 头依然 401（透传不改变这条）。

    2026-10-03 口径变更：Bearer 改成**即梦 sessionid 透传**、不再查白名单，
    所以"带一个不认识的 key"不再是 401（它会被当sessionid 收下，
    在**派发时**才因凭据无效而失败 —— 那是上游的判断，不是鉴权层的）。

    ⚠️ 代价（知情接受）：**任何持有即梦登录态的人都能调本服务**，
    计费与风控归他自己，我们的��务不代付。
    """
    assert client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}
                       ).status_code == 401
    assert client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                       headers={"Authorization": ""}
                       ).status_code == 401


def test_any_bearer_is_accepted_as_sessionid(client):
    """透传的**正向**语义：任何非空 Bearer 都能建任务（凭据有效性在上游校验）。"""
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers={"Authorization": "Bearer 522ceab845a313502b72f5067534d191"})
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# 模型清单 / 运维端点
# ---------------------------------------------------------------------------


def test_models_endpoint_lists_only_verified_capabilities(client):
    data = client.get("/v1/models", headers=AUTH).json()
    ids = {m["id"] for m in data["data"]}
    # 🔴 2026-09-24：视频族对外**一律方舟模型名**（内部名退为路由别名，
    # 只出现在条目的 internal_id 字段）；全能参考/补帧是请求形态不单列。
    # 🔴 2026-10-02 新增 `jimeng-audit`（素材预审）：**成功时零产物**是预期结果
    # （用户拍板的口径：判定结论在 degradations 里），已豁免"零产物=失败"。
    assert ids == {"jimeng-t2i", "jimeng-i2i", "jimeng-hd",
                   "jimeng-pro-hd", "jimeng-outpaint",
                   "jimeng-detail-fix", "jimeng-audit",
                   "doubao-seedance-2-0-mini-260615",
                   "doubao-seedance-2-0-fast-260128",
                   "doubao-seedance-2-0-260128",
                   "doubao-seedance-2-5-260628"}
    assert "detail" not in ids, "旧的工具名不对外"


#: `/v1` 前缀下**允许**的端点 —— 全部是**同步语义**（一次请求一个最终响应）。
#: 2026-09-23 从 `{"/v1/models"}` 扩到当前集合：新增了**同步生成**端点
#: （创建+轮询合并），它同样属于"同步、无 `202 + task_id` 轮询语义"的正牌成员。
#: **异步**族（受理/查询/删除）必须留在 `/async/v1`（护栏拦的是"异步语义挂 /v1"）。
_V1_SYNC_ENDPOINTS = frozenset({"/v1/models", "/v1/images/generations"})


def test_models_endpoint_lives_only_under_v1(client):
    """模型清单**只在 `/v1/models`**；`/v1` 下只放**同步语义**端点（白名单钉死）。

    这条门禁是**双向**的：
      · `/v1/models` 必须在，且**免鉴权也 200**（2026-09-23 起刻意公开）；
      · `/async/v1/models` 必须**不存在** —— 否则有人"顺手加回来"就又成了双前缀，
        两边的文档/门禁/调用方认知会重新分叉。
    反向断言：`/v1` 下只允许白名单里的端点。生成/查询/删除的**异步**语义
    （`202 + task_id` + 轮询）挂到 `/v1` 会被误读成同步 OpenAI images/videos
    API —— `/async` 前缀就是那道护栏。而**同步生成端点**
    （`POST /v1/images/generations`，创建+轮询合并）本来就是同步语义，
    放 `/v1` 是正确的归属，不是护栏要拦的东西。
    """
    ok = client.get("/v1/models", headers=AUTH)
    assert ok.status_code == 200, ok.text
    # 🔓 2026-09-23 用户指令：免鉴权。不带任何凭据也必须 200，且内容与带 Key 一致 ——
    # 它是发现性端点，客户端要在配置 Key 之前先探"这服务有什么能力"。
    anon = client.get("/v1/models")
    assert anon.status_code == 200, \
        f"/v1/models 应免鉴权（发现性端点），实得 {anon.status_code}"
    assert anon.json() == ok.json(), "带不带 Key 拿到的清单必须一致"

    gone = client.get("/async/v1/models", headers=AUTH)
    assert gone.status_code == 404, \
        f"/async/v1/models 应当已取消，实得 {gone.status_code}"

    paths = {r.path for r in client.app.routes}
    assert "/v1/models" in paths
    assert "/async/v1/models" not in paths
    leaked = {p for p in paths if p.startswith("/v1/")} - _V1_SYNC_ENDPOINTS
    assert not leaked, \
        f"/v1 下只允许同步语义端点（白名单）：{sorted(leaked)}"


#: FastAPI 自带的接口文档路由 —— 它们**不是**本服务的业务端点，
#: 天然没有业务鉴权（`/docs` 暴露整个 API 形态，属于**部署层**决定要不要关，
#: 见 README 的边界一节）。
_FRAMEWORK_ROUTES = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect",
                               "/redoc"})

#: 🔓 **刻意公开的业务端点**（不挂 `require_key`）—— 2026-09-23 用户指令：
#: `/v1/models` 是**发现性端点**（客户端在配置 Key 之前先探"这服务有什么能力"
#: 是常规做法），内容只有能力的公开描述、不含任何任务/凭据/内部状态。
#: ⚠️ 往这里加东西 = 对公网免鉴权，每一项都必须有一个说得出口的理由。
_PUBLIC_PATHS = frozenset({"/v1/models", "/", "/llms.txt"})   # 发现面：给人/给 LLM，刻意公开（2026-09-24）


def test_every_business_route_requires_a_bearer(client):
    """🔴 **所有业务端点（任何方法）都必须挂 `require_key`** —— 例外 = 探活 + 白名单。

    为什么要有这条**结构性**门禁（而不是只测几条具体路径）：靠人记着"新加路由
    时别忘了挂依赖"是记不住的。2026-09-23 收紧鉴权时就是这个原因暴露出来的 ——
    查单条的 GET 一直是"可选鉴权"（`task_id` 即凭据），`/v1/models` 与 `/stats`
    更是**完全开放**，而公网入口就是 80 端口。

    🔴 从"只看 GET"升级到**全方法**：新增的同步生成端点
    （`POST /v1/images/generations`）不在旧的扫描面里 —— 只盯 GET 的门禁，
    对 POST/DELETE/PUT 全是盲区，而它们才是会产生费用的写路径。
    判据只认路由级依赖表里有 `require_key`。

    两类例外：
      · **探活**：`/healthz`（容器 HEALTHCHECK）与 `/readyz`（编排层判活）
        必须在**没有任何凭据**时可用 —— 判活失败要说清是服务的问题，
        不能因为"没带 Key"而看起来像挂了；
      · **白名单**（`_PUBLIC_PATHS`）：`/v1/models` 是发现性端点，刻意公开
        （2026-09-23 用户指令）。
    """
    from app.observability import PROBE_PATHS

    assert set(PROBE_PATHS) == {"/healthz", "/readyz"}, \
        "探活路径表变了 —— 这里的不鉴权例外名单要跟着改"

    open_paths: set[str] = set()
    for route in client.app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", []) or ())
        if not path.startswith("/") or not methods:
            continue
        if path in PROBE_PATHS or path in _FRAMEWORK_ROUTES \
                or path in _PUBLIC_PATHS:
            continue
        dep = getattr(route, "dependant", None)
        if dep is None:
            # 非 FastAPI 业务路由（Starlette 原生 Route/Mount 等）—— 不适用本门禁；
            # 若真有人用它挂业务端点，契约用例（如 /v1/models）会先红。
            continue
        deps = {d.call.__name__ for d in dep.dependencies}
        if "require_key" not in deps:
            open_paths.add(path)

    assert not open_paths, \
        f"这些端点没挂鉴权（新加的？）：{sorted(open_paths)}"


def test_healthz_is_dependency_free_and_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_reports_upstream_configuration(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.service import Service

    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    monkeypatch.setattr(main_mod, "Service", lambda s: Service(s))
    app = main_mod.create_app(blank)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        body = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()
        assert body["error"]["code"] == "upstream_not_configured"


def test_unconfigured_upstream_is_503_not_401(client_factory):
    """上游凭据缺失是**部署问题**，不是调用方的身份问题 ⇒ 503 而非 401。"""
    c = client_factory(jimeng_sessionid="", jimeng_cookie="")
    r = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_not_configured"


# ---------------------------------------------------------------------------
# 同步生成：POST /v1/images/generations（创建+轮询合并）
# ---------------------------------------------------------------------------

SYNC = "/v1/images/generations"


class _InlineCoordinator:
    """把协调器换成"`wake()` 时就地推进"的替身 —— 同步接口用例专用。

    为什么需要它：同步端点在**一个请求内**等到终态，而测试纪律是
    "协调器不启线程、用例显式 tick"（见 conftest 纪律 #2）。这个替身把
    两者接起来：请求里调 `wake()` 时直接跑 N 轮 `tick()`，等待循环的第一轮
    查库就能看到终态 —— 确定性最高，不引入后台线程的时序不确定性。
    """

    def __init__(self, real: Any, *, advances: int = 3,
                 running: bool = True) -> None:
        self.real = real
        self.advances = advances
        self._running = running
        self.wakes = 0

    @property
    def running(self) -> bool:
        return self._running

    def wake(self) -> None:
        self.wakes += 1
        for _ in range(self.advances):
            self.real.tick()

    def stats(self) -> dict:
        return self.real.stats()

    def __getattr__(self, name: str) -> Any:
        # 其余接口（stop/start/…）转发给真协调器 —— 用例只替换
        # running/wake/stats 三个，lifespan 退出时的 stop() 也能照常工作。
        return getattr(self.real, name)


@pytest.fixture
def sync_client(client, client_state):
    """默认 app 的"同步版"：协调器换成 inline 替身（wake 即推进）。"""
    client_state.coordinator = _InlineCoordinator(client_state.coordinator)
    return client


def test_sync_single_call_returns_final_result(sync_client, client_state,
                                               fake_jimeng):
    """一次 POST 拿到最终结果 —— 这就是"创建+轮询合并"的全部意义。"""
    fake_jimeng.states = [ok_state(["https://cdn/a.png", "https://cdn/b.png"])]
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x",
                                     "n": 2}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # 与异步查询成功体**逐键一致**：同一个 `view()` 出口，不许长出第二套形状
    assert set(body) == {"status", "data", "created", "usage"}
    assert body["status"] == "success"
    assert [d["url"] for d in body["data"]] == ["https://cdn/a.png",
                                                "https://cdn/b.png"]
    assert client_state.coordinator.wakes == 1, "受理后应叫醒协调器"


def test_sync_failure_is_a_200_with_failure_status(sync_client, fake_jimeng):
    """任务失败也是"完整的答案"：直接回 `failure` 体，调用方无需再轮询。"""
    fake_jimeng.states = [failed_state("generate_failed")]
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failure"
    assert r.json()["error"]


def test_sync_timeout_degrades_to_async_polling(client_factory):
    """预算耗尽 ⇒ 202 + task_id + Location 指向异步端点；任务不受影响。

    这就是"超时降级"路径：调用方无缝转异步轮询 —— 任务不会被取消
    （即梦根本没有取消端点），只是这一个 HTTP 请求不再等它。
    """
    import time as _time

    c = client_factory(sync_max_wait=0.05)
    st = c.app.state
    # advances=0 ⇒ wake() 不推进 ⇒ 任务停在 queued，预算一到就降级
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    t0 = _time.monotonic()
    r = c.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    elapsed = _time.monotonic() - t0

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    tid = body["task_id"]
    assert r.headers["location"] == f"/async/v1/images/generations/{tid}"
    # 🔴 防"配置没生效、真等 300s"把用例挂死：耗时必须远小于默认预算
    assert elapsed < 5, f"降级应当及时，实耗 {elapsed:.1f}s"

    # 降级后任务还在（没被取消/删除），异步端点照常可轮询
    got = c.get(f"/async/v1/images/generations/{tid}", headers=AUTH)
    assert got.status_code == 202
    assert got.json()["status"] == "queued"


def test_sync_503_without_a_running_coordinator(client):
    """没有推进者 ⇒ **快速 503**，而不是白等一整个预算。

    夹具默认 `COORDINATOR_ENABLED=0` ⇒ 协调器线程没起 ⇒ running=False。
    这个部署形态下任务根本不会被推进，同步等待注定熬到超时 —— 快速说清。
    同时断言：**没有落下任何任务**（503 发生在受理之前）。
    """
    before = client.get(BASE, headers=AUTH).json()["total"]
    r = client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers=AUTH)
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["code"] == "sync_unavailable"
    assert "协调器" in err["message"]
    after = client.get(BASE, headers=AUTH).json()["total"]
    assert after == before, "503 发生在受理之前，不该留下孤儿任务"


def test_sync_requires_bearer(sync_client):
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401


def test_sync_validates_like_the_async_endpoint(sync_client):
    """参数校验复用同一套（同一个 `Service.create`）—— 报错逐字一致。"""
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x",
                                     "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 400
    assert "必须是**数组**" in r.json()["error"]["message"] or \
        "必须是数组" in r.json()["error"]["message"]


_ = (pytest, KEY_B)
