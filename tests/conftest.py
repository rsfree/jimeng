#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具。

三条纪律：

1. **零真实上游调用**。所有用例都注入 `FakeJimeng` / `FakeUploader`，
   一个字节都不发出去 —— 建任务是**计费**动作，测试里绝不能碰真上游。
2. **协调器默认不启线程**（`COORDINATOR_ENABLED=0`），由用例显式调 `tick()`。
   否则用例会与后台线程抢同一个任务，出现"偶尔绿、偶尔红"。
3. **假上游要能记录"收到过什么"**。只断言"我们的函数返回对了"，
   测不到"翻译没接进调用路径"这类装配缺陷。
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.coordinator import Coordinator
from app.service import Service
from app.store import TaskStore
from app.upstream.jimeng import GeneratedImage, TaskState

SESSION = "test-sessionid-please-do-not-use-in-prod"
KEY_A = "sk-test-key-0000000000000000"
KEY_B = "sk-test-key-1111111111111111"
AUTH = {"Authorization": f"Bearer {KEY_A}"}
AUTH_B = {"Authorization": f"Bearer {KEY_B}"}

DB_DSN_ENV = "TEST_DATABASE_URL"


# ---------------------------------------------------------------------------
# 真库夹具：**每个用例一个独立 schema**
# ---------------------------------------------------------------------------


def _dsn() -> str:
    """取测试用 PostgreSQL DSN。

    🔴 **缺了就 fail，不 skip。** 本服务的任务库只支持 PostgreSQL，
    store / API / 协调器类用例必须打到真库上；静默跳过会让人把"没跑"当成"跑过了"
    —— 那正是本仓库最不接受的假绿。
    """
    dsn = (os.environ.get(DB_DSN_ENV) or "").strip()
    if not dsn:
        pytest.fail(
            f"缺少 {DB_DSN_ENV}。本服务的任务库只支持 PostgreSQL，"
            f"store / API / 协调器类用例必须打到真库上。\n"
            f"  起一个库：docker compose up -d db\n"
            f"  然后：  export {DB_DSN_ENV}="
            f"'postgresql+psycopg2://jimeng:<密码>@127.0.0.1:5432/jimeng_test'\n"
            f"（刻意用 fail 而不是 skip：跳过会让整套用例看起来是绿的。）",
            pytrace=False)
    return dsn


def _with_schema(dsn: str, schema: str) -> str:
    """把 `search_path` 塞进 DSN —— 于是 Service 内部自建的 store 也落在隔离 schema 里。"""
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3D{schema}"


@pytest.fixture(scope="session")
def _admin_engine():
    """连一遍确认可达 —— 连不上要**立刻**说清是库的问题，而不是让每个用例各自报错。"""
    eng = create_engine(_dsn(), pool_pre_ping=True)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:
        pytest.fail(f"{DB_DSN_ENV} 指向的 PostgreSQL 连不上："
                    f"{type(e).__name__}: {e}", pytrace=False)
    yield eng
    eng.dispose()


@pytest.fixture
def db_schema(_admin_engine) -> str:
    """一次用例 = 一个独立 schema；用完 CASCADE 删掉。

    比"跑完 truncate"更干净：表结构也一并重建，不会带着上个用例的残留。
    """
    name = f"t_{uuid.uuid4().hex[:12]}"
    with _admin_engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{name}"'))
    try:
        yield name
    finally:
        with _admin_engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


@pytest.fixture
def db_dsn(db_schema) -> str:
    return _with_schema(_dsn(), db_schema)


@pytest.fixture
def stats_engine(_admin_engine):
    """真实 PG 引擎（只为拿 dialect —— 断言 JSONB 这类方言差异时用）。"""
    return _admin_engine


# ---------------------------------------------------------------------------
# 假上游
# ---------------------------------------------------------------------------


@dataclass
class FakeJimeng:
    """鸭子类型的即梦客户端：记录**收到过什么**，并按剧本回什么。"""

    #: 每次 `fetch` 依次返回的 TaskState；用完后重复最后一个
    states: list[TaskState] = field(default_factory=list)
    #: 预审结果（用例可改成 `({"audit_decision": 2, ...},)` 测拒绝分支）
    audit_result: tuple = ({"audit_decision": 1},)
    submit_id: str = "upstream-submit-id"
    warnings: list[str] = field(default_factory=list)
    fail_submit: Exception | None = None
    common_config_response: dict = field(default_factory=dict)

    calls: list[dict] = field(default_factory=list)
    fetch_count: int = 0
    #: 历次建任务产生的 submit_id（**逐个不同**，如同真实客户端生成的 uuid4）
    submitted: list[str] = field(default_factory=list)
    #: 最近一次视频建任务的 draft_content（真实客户端有 `last_draft`，
    #: 补帧要引用源任务的草稿 ⇒ 假上游也得能留下它）
    last_draft: str | None = None

    _VIDEO_DRAFT = ('{"type":"draft","component_list":'
                    '[{"id":"parent-1","generate_type":"gen_video"}]}')

    def _new_submit_id(self) -> str:
        """每次建任务给一个**唯一** id —— 真实客户端生成的是 uuid4。

        假上游如果对所有任务回同一个 id，就测不出"批量查询结果有没有对号入座"
        这类缺陷（三个任务的 id 相同 ⇒ 错位也看不出来）。
        """
        sid = f"{self.submit_id}-{len(self.submitted)}"
        self.submitted.append(sid)
        return sid

    # ---- 建任务 ----
    def submit(self, prompt: str, **kw: Any) -> str:
        self._record("submit", prompt=prompt, **kw)
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    def blend(self, prompt: str, **kw: Any) -> str:
        self._record("blend", prompt=prompt, **kw)
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    def edit(self, tool: str, **kw: Any) -> str:
        self._record("edit", tool=tool, **kw)
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    def submit_video(self, prompt: str, **kw: Any) -> str:
        """文生视频 —— 与图片族同构：记录收到过什么，按剧本回 id。"""
        self._record("submit_video", prompt=prompt, **kw)
        self.last_draft = self._VIDEO_DRAFT
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    def submit_video_vfi(self, source_draft: str, **kw: Any) -> str:
        """视频补帧 —— 记录源草稿与引用三件套。"""
        self._record("submit_video_vfi", source_draft=source_draft, **kw)
        self.last_draft = source_draft
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    def submit_video_omni(self, instruction: str, **kw: Any) -> str:
        """全能参考视频 —— 记录指令与已上传素材。"""
        self._record("submit_video_omni", prompt=instruction, **kw)
        self.last_draft = self._VIDEO_DRAFT
        if self.fail_submit:
            raise self.fail_submit
        return self._new_submit_id()

    # ---- 取任务 ----
    def fetch_many(self, submit_ids: list[str]) -> dict[str, TaskState]:
        """批量查询 —— **一次调用 = 一轮上游查询**（与服务侧的真实实现同构）。

        `fetch_count` 每次调用只 +1：`states` 剧本表达的是"上游在第 N 次**查询**时
        答什么"，而不是"第 N 个任务答什么"。合并查询下所有 id 拿到同一份答案。
        """
        ids = [s for s in submit_ids if s]
        self._record("fetch_many", ids=list(ids))
        if not self.states:
            return {s: TaskState(submit_id=s, status=20, status_name="submitted")
                    for s in ids}
        i = min(self.fetch_count, len(self.states) - 1)
        self.fetch_count += 1
        st = self.states[i]
        return {s: st for s in ids}

    def fetch(self, submit_id: str) -> TaskState:
        return self.fetch_many([submit_id])[submit_id]

    def audit_materials(self, uris: list[str], **kw: Any) -> list[dict]:
        """素材预审替身 —— **默认全部通过**（`audit_decision=1`）。

        🔴 为什么需要它（2026-10-03）：生产链路在提交生成前会调
        `execute_generate_audit`（输入图违规在那儿拦）。假客户端缺这个方法
        ⇒ `getattr` 失败被 `_pre_audit_materials`的 **fail-open**兜住
        （放行 + 留痕）——测试照样能过，但会刷无意义的告警、且**测不到**
        "预审拒绝 ⇒ 不提交生成"这条真路径。

        ⚠️ 这里**默认放行**：绝大多数用例与审核无关。要测拒绝分支的用例
        自行设置 `fake_jimeng.audit_result = {"audit_decision": 2, ...}`。
        """
        return list(self.audit_result)

    def common_config(self, **kw: Any) -> dict:
        self._record("common_config", **kw)
        return self.common_config_response

    def image_by_uri(self, uris: Any) -> dict:
        self._record("image_by_uri", uris=uris)
        return {}

    def close(self) -> None:
        pass

    # ---- 观察 ----
    def _record(self, kind: str, **kw: Any) -> None:
        self.calls.append({"kind": kind, **kw})

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]

    @property
    def last_warnings(self) -> list[str]:
        return self.warnings


@dataclass
class FakeUploader:
    uri: str = "tos-cn-i-testbucket/abc123"
    last_cached: bool = False
    uploads: list[bytes] = field(default_factory=list)
    #: 每次上传**返回不同的 uri**（`<base>-<序号>`）。真实上传器同样不是内容寻址
    #: （`UPSTREAM.md`：同一份字节传两次得到两个**不同**的 uri），所以这里也不按内容去重。
    uris: list[str] = field(default_factory=list)
    #: `(字节, 返回的 uri)` 配对 —— 并发下 append 顺序**不确定**，
    #: 想断言"哪张图换到了哪个 uri"必须靠这张映射表，而不是靠列表顺序。
    pairs: list[tuple[bytes, str]] = field(default_factory=list)
    fail: Exception | None = None
    #: 每次上传的模拟耗时基数（秒）。默认 0：普通用例不该为了测并发而变慢。
    delay_s: float = 0.0

    def _delay(self, data: bytes) -> float:
        """按**内容**派生的耗时 —— 完成顺序因此与提交顺序**不同**。

        这样"结果仍按输入顺序返回"才成为一个真实的检验：
        若实现按"谁先完成谁先排"，顺序断言就会翻车。
        """
        if not self.delay_s:
            return 0.0
        return self.delay_s * (1 + hashlib.sha256(data).digest()[0] % 3)

    #: 🔴 并发观测（2026-10-03）：`inflight` 是**当前在飞**数、`peak_inflight`
    #: 是**历史峰值**。加它是为了让"是否并发上传"可以用**计数**断言，
    #: 而不是掐墙钟 —— 计时断言在负载高的机器上会假红（见下）。
    inflight: int = 0
    peak_inflight: int = 0

    def upload(self, data: bytes, **kw: Any) -> str:
        if self.fail:
            raise self.fail
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            time.sleep(self._delay(data))
            self.uploads.append(data)
            uri = f"{self.uri}-{len(self.uploads) - 1}"
            self.uris.append(uri)
            self.pairs.append((data, uri))
            return uri
        finally:
            self.inflight -= 1

    def uri_for(self, data: bytes) -> str:
        """这张字节最终换到的 uri（用于断言顺序与映射）。"""
        for blob, uri in self.pairs:
            if blob == data:
                return uri
        raise AssertionError("这份字节没有被上传过")

    def close(self) -> None:
        pass


@dataclass
class FakeVod:
    """假 VOD 上传器：记录字节，返回固定 vid（含 VideoMeta 形态）。"""

    vid: str = "v02870fake0001vid0000000000"
    uploads: list[bytes] = field(default_factory=list)

    def upload(self, data: bytes, **kw: Any) -> dict:
        self.uploads.append(data)
        return {"vid": self.vid, "store_uri": "tos-cn-v-fake/x",
                "width": 480, "height": 360, "duration_ms": 5042,
                "commit": {"Results": [{"Vid": self.vid,
                                        "VideoMeta": {"Width": 480,
                                                      "Height": 360,
                                                      "Duration": 5.041667}}]}}

    def close(self) -> None:
        pass


class FakeConfigCache:
    """假的能力表缓存：默认给 v50 服务端声明的 1..8。"""

    def __init__(self, options: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8),
                 note: str | None = None,
                 declared: tuple[int, ...] | None = None) -> None:
        self.options = options
        self.note = note
        #: **服务端声明值**（不做兜底的那个）。默认与 `options` 一致 = 已声明。
        #: 设成 None 可模拟"该模型就是不声明张数选项"（实测真实存在这种模型）。
        self.declared = options if declared is None else declared
        self.calls = 0

    def count_options(self, model: str) -> tuple[int, ...]:
        self.calls += 1
        return self.options

    def count_options_declared(self, model: str) -> tuple[int, ...] | None:
        return self.declared

    def input_image_limit(self, model: str) -> int | None:
        return None

    def resolution_map(self, model: str) -> dict | None:
        return None

    def feats(self, model: str) -> tuple[str, ...] | None:
        return None

    def known_models(self) -> tuple[str, ...]:
        return ()

    def degradation_note(self, model: str) -> str | None:
        return self.note

    def stats(self) -> dict:
        return {"models": 1, "age_s": 0.0, "refresh_attempts": self.calls,
                "refresh_failures": 0, "last_error": None}


# ---------------------------------------------------------------------------
# 基础夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(db_dsn) -> Settings:
    # 🔴 2026-10-03 透传：**不再有** jimeng_sessionid 配置项 ——
    # 凭据由每个请求的 Bearer 带来，服务自己不需要持有。
    return Settings(
        # 两把 Key：用来验证"跨凭证读不到别人的任务"
        api_keys=(KEY_A, KEY_B),
        jm_concurrency=1,
        jm_cooldown=600.0,
        # ⚠️ 必须是 **0.0**（"每个 tick 都轮到"，而不是 0.01 那种"很小的间隔"）。
        # 原因：`updated_at` 是**整秒**存的（`int(time.time())`），而间隔门比的是
        # 浮点 `now - updated_at` —— 于是"这一轮该不该轮询"取决于**当前秒的小数位**：
        # 两次 tick 都落在某秒的前 10ms 内时，间隔门会把两次轮询**都**挡掉，
        # 任务就停在 in_progress，用例偶发失败（实测约 1% 概率，连跑才看得出来）。
        # 生产里间隔是 2s ≫ 1s 的存储精度，所以这只是夹具要避开的坑。
        jimeng_poll_interval=0.0,
        poll_grace=0.0,
        task_timeout=60.0,
        coordinator_enabled=False,
        coordinator_lease=5.0,
        task_db=db_dsn,
        normalize_uploads=False,
        otel_token="",
    )


@pytest.fixture
def store(settings: Settings) -> TaskStore:
    return TaskStore(settings.db_target)


@pytest.fixture
def fake_jimeng() -> FakeJimeng:
    return FakeJimeng()


@pytest.fixture
def fake_uploader() -> FakeUploader:
    return FakeUploader()


@pytest.fixture
def fake_vod() -> FakeVod:
    return FakeVod()


@pytest.fixture
def service(settings: Settings, store: TaskStore, fake_jimeng: FakeJimeng,
            fake_uploader: FakeUploader, fake_vod: FakeVod) -> Service:
    return Service(settings, store=store, client=fake_jimeng,
                   uploader=fake_uploader, vod=fake_vod,
                   cfg=FakeConfigCache())


@pytest.fixture
def coordinator(service: Service, settings: Settings) -> Coordinator:
    return Coordinator(service, settings, owner="test-owner")


# ---------------------------------------------------------------------------
# HTTP 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def app_and_client(service: Service, settings: Settings, fake_jimeng: FakeJimeng,
                   fake_uploader: FakeUploader, monkeypatch):
    """返回 (app, TestClient, client_factory)。**不启协调器线程**，用例自己调 tick()。

    🔴 基础 app **复用 `service` 夹具那个实例**，不另建一个 —— 否则
    `service.gate` 与 app 里真正干活的那把闸门是**两个对象**，
    测试里"把闸门打进冷却再 tick"这类断言会**静默失效**（实测踩过：
    `mark_risk_hit()` 打的是另一把闸门，任务照样被派出去，测试却只在最后一步报错，
    让人以为是业务逻辑坏了）。
    ⇒ 夹具之间只允许存在**一个真相**。

    `client_factory(**overrides)` 用改了配置的 Settings 另建 app（测"未配凭据"这类
    部署状态）；只有那种情况才新建 Service。所有实例在 teardown 一并关闭。
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.service import Service as RealService

    def _fake_service(settings_: Settings) -> RealService:
        if settings_ is settings:
            return service          # 基础 app：复用同一实例（同一 gate / 同一 store）
        return RealService(settings_, client=fake_jimeng, uploader=fake_uploader,
                           vod=fake_vod, cfg=FakeConfigCache())

    opened: list[Any] = []

    def _build(settings_: Settings):
        monkeypatch.setattr(main_mod, "Service", _fake_service)
        app = main_mod.create_app(settings_)
        c = TestClient(app)
        c.__enter__()
        opened.append(c)
        return app, c

    def client_factory(**overrides):
        st = settings.replace(**overrides) if overrides else settings
        _app, c = _build(st)
        return c

    app, client = _build(settings)
    try:
        yield app, client, client_factory
    finally:
        for c in opened:
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


@pytest.fixture
def client_state(app_and_client):
    """`app.state` —— 里面有 `service` 与 `coordinator`，用例据此手动推进任务。"""
    return app_and_client[0].state


@pytest.fixture
def client_factory(app_and_client):
    return app_and_client[2]


# ---------------------------------------------------------------------------
# 剧本辅助
# ---------------------------------------------------------------------------


def ok_state(urls: list[str], *, cost: int | None = 44) -> TaskState:
    st = TaskState(submit_id="upstream-submit-id", status=50, status_name="success",
                   finished=True, failed=False, cost=cost)
    st.images = [GeneratedImage(url=u, width=2048, height=2048, format="png")
                 for u in urls]
    return st


def submitted_state() -> TaskState:
    """上游已受理但**还没出图**。

    为什么需要它：协调器**一轮 tick 里会先建任务再轮询**，
    所以假上游第一次 `fetch` 就返回终态的话，任务一跳就到 success ——
    那不符合真实链路（真上游要几十秒），也测不到"非终态不被判死"这一段。
    """
    return TaskState(submit_id="upstream-submit-id", status=20,
                     status_name="submitted", finished=False, failed=False)


def failed_state(reason: str = "generate_failed") -> TaskState:
    return TaskState(submit_id="upstream-submit-id", status=30,
                     status_name="generate_failed", finished=True, failed=True,
                     failed_reason=reason)


def drive(client_state, times: int = 2) -> None:
    """手动跑协调器：第 1 轮建任务，第 2 轮轮询到终态。"""
    for _ in range(times):
        client_state.coordinator.tick()
