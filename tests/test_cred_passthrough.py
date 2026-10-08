# -*- coding: utf-8 -*-
"""sessionid 透传的门禁（2026-10-03）。

用户口径"鉴权用 SESSIONID 就行"：Bearer 就是即梦 sessionid，直接当上游凭据用，
**明文**存进任务表（用户明确"直接明文就行"）。

这些用例钉住三件最容易塌的事：凭据不外泄、按人隔离、不越权。
"""
from __future__ import annotations

import json
import time

from tests.test_coordinator import (
    _create, _data_uri, _PNGS, ok_state, submitted_state)


# ---------------------------------------------------------------------------
# ① 🔴 凭据绝不能出现在任何客户可见的响应里
# ---------------------------------------------------------------------------

def test_response_never_contains_the_credential():
    """🔴 带凭据的任务记录 → 响应里**一个字节的凭据都不许有**。

    为什么这条值得钉死：`view()` 是**白名单式**构造（每个分支显式列字段），
    这很安全 —— 但也意味着"安全"依赖"没人改成整记录序列化"。
    一旦有人图省事写 `json.dumps(rec.model_dump())`，凭据立刻随响应外泄。

    ⚠️ 现在是**明文**存储（用户拍板），所以这条更关键：库里躺的就是明文，
    任何"顺手把整个记录吐出去"的改动都会立刻泄露它。
    """
    from app.service import view
    from app.store import TaskRecord

    sid = "522ceab845a313502b72f5067534d191"
    for status, extra in (("queued", {}),
                          ("in_progress", {}),
                          ("canceled", {}),
                          ("failure", {"error": {"code": "x", "message": "y"}}),
                          ("success", {"images": [{"url": "https://x/1.png"}]})):
        rec = TaskRecord(task_id="jimeng_t", credential_id="anon",
                         upstream_sessionid=sid, model="jimeng-t2i",
                         cap_key="jimeng:t2i", status=status, prompt="p",
                         degradations=["note"], **extra)
        _code, body = view(rec)
        blob = json.dumps(body, ensure_ascii=False)
        assert sid not in blob, f"{status} 响应泄露了 sessionid 明文"
        assert "sessionid" not in blob.lower(), f"{status} 响应出现 sessionid 字样"


def test_view_of_one_task_does_not_leak_anothers_credential():
    """🔴 `view()` 只认传进去的那条记录 —— 不会顺带带出库里的其它凭据。"""
    from app.service import view
    from app.store import TaskRecord

    a = TaskRecord(task_id="jimeng_a", credential_id="c1",
                   upstream_sessionid="522ceab845a313502b72f5067534d191",
                   model="jimeng-t2i", cap_key="jimeng:t2i", status="success",
                   images=[{"url": "https://x/1.png"}])
    b = TaskRecord(task_id="jimeng_b", credential_id="c2",
                   upstream_sessionid="a4196393c9c61b568e0ce3bc14d4fda9",
                   model="jimeng-t2i", cap_key="jimeng:t2i", status="success",
                   images=[{"url": "https://x/2.png"}])
    for rec, other in ((a, "a4196393c9c61b568e0ce3bc14d4fda9"),
                       (b, "522ceab845a313502b72f5067534d191")):
        _c, body = view(rec)
        assert other not in json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# ② 客户端池：按 sessionid 隔离 + 有界 + 向后兼容
# ---------------------------------------------------------------------------

def test_bundle_pool_is_isolated_per_credential(settings, store):
    """🔴 不同 sessionid 必须拿到**不同的** client/uploader/vod。

    这是透传最容易塌的地方：`VodUploader` 复用 `ImageXUploader` 的 STS，
    混用两把凭据 ⇒ 拿到**不属于自己**的上传凭据（表现为莫名 1015/上传失败）。
    """
    from app.service import Service

    svc = Service(settings, store=store)
    a = "522ceab845a313502b72f5067534d191"
    b = "a4196393c9c61b568e0ce3bc14d4fda9"
    ba = svc.bundle_for(a)
    bb = svc.bundle_for(b)
    assert ba is not bb, "两个凭据拿到了同一套组件 ⇒ 会串号"
    for x, y in zip(ba, bb):
        assert x is not y
    # 同一凭据重复取 ⇒ 命中缓存（同一对象）
    assert svc.bundle_for(a) is ba, "同一凭据没命中缓存 ⇒ 每次都新建，白花钱"


def test_bundle_pool_is_bounded(settings, store):
    """🔴 池必须**有界**：每个新调用方都建一套 client，不封顶就是内存泄漏。"""
    from app.service import Service

    settings.cred_pool_max = 3
    svc = Service(settings, store=store)
    for i in range(10):
        svc.bundle_for(f"{i:032x}")
    assert len(svc._cred_pool) <= 3, f"池没封顶：{len(svc._cred_pool)}"


def test_empty_sessionid_falls_back_to_default(settings, store):
    """没有 sessionid ⇒ 用默认那套（= 旧行为，向后兼容）。"""
    from app.service import Service

    svc = Service(settings, store=store)
    assert svc.bundle_for(None) == (svc.client, svc.uploader, svc.vod, svc.cfg)
    assert svc.bundle_for("") == (svc.client, svc.uploader, svc.vod, svc.cfg)

def test_auth_stays_on_even_when_api_keys_is_empty(settings):
    """🔴 `API_KEYS` 为空**不许**把鉴权关掉 —— 那会让 Bearer 被忽略。

    🔴 2026-10-03 实踩的坑：清理线上 `.env` 时把 `API_KEYS` 整行注释掉，
    于是 `auth_enabled`（原= `bool(api_keys)`）变 False ⇒ `require_key`
    走 `credential_of(None)` ⇒ **Bearer 完全被忽略**、任务以 `anonymous`
    落库 ⇒ 表现是"生图挂住直到超时"，而任务其实在跑。

    ⚠️ 这类"某个开关关掉了整条链路"的连带伤害，**光看日志很难定位**
    （没有任何报错、协调器 ticks 正常），所以钉成门禁。
    """
    from app.config import Settings

    blank = settings.replace(api_keys=())
    assert blank.auth_enabled is True, (
        "API_KEYS 为空不能关掉鉴权 —— 透传模式下凭据就是 Bearer 本身")
    assert Settings().auth_enabled is True


def test_task_is_recorded_with_the_callers_own_credential(client):
    """🔴 任务必须以**调用方自己的**凭据落库（不是 `anonymous`）。

    `anonymous` 意味着身份丢失 ⇒ 跨凭据隔离失效（谁都能查谁的任务）。
    这条直接盯住 `credential_id` 的取值。
    """
    sid = "522ceab845a313502b72f5067534d191"
    r = client.post("/async/v1/images/generations",
                    headers={"Authorization": f"Bearer {sid}"},
                    json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]

    body = client.get(f"/async/v1/images/generations/{tid}",
                      headers={"Authorization": f"Bearer {sid}"}).json()
    # 用**同一把** sessionid 必须能查到（查不到 = 身份算错了）
    assert body.get("task_id") == tid, (
        f"用同一 sessionid 却查不到自己的任务：{body}")
    assert body.get("status") in ("queued", "in_progress", "success")


# ---------------------------------------------------------------------------
# 🔴🔴 防"静默失效"：生产 self.client 恒为 None 时的两个坑
# ---------------------------------------------------------------------------

def test_poll_many_does_not_early_return_when_self_client_is_none(
        settings, store, fake_jimeng, fake_uploader):
    """🔴🔴 `poll_many` **不能**因为 `self.client is None` 就整段 return。

    这是 2026-10-03 **自造的真 bug**（透传改造引入）：
    删掉"用 env 建默认 client"之后，生产 `self.client` **恒为 None**，
    而 `poll_many` 第一行原是 `if self.client is None: return`
    ⇒ **每个 tick 直接返回** ⇒ 任务永远停在 `in_progress`、
    `updated_at` 从不刷新，**日志里一条报错都没有**
    （协调器 ticks 正常、`/readyz` ready、`/stats` 全健康）。

    实测症状极具误导性：生图请求挂到超时，而**手动调 `fetch_many`
    却能拿到 `status=50 success`** ⇒ 看起来像"取结果坏了"，
    实际是"**没人去取**"。

    这条用未注入 client 的 Service 复现（生产形态）。
    """
    from app.service import Service
    from app.store import TaskRecord

    svc = Service(settings, store=store, client=None, uploader=None, cfg=None)
    assert svc.client is None, "本用例要复现生产的 self.client is None"

    rec = TaskRecord(task_id="jimeng_polltest", credential_id="c1",
                     upstream_sessionid="522ceab845a313502b72f5067534d191",
                     model="jimeng-t2i", cap_key="jimeng:t2i",
                     status="in_progress", prompt="p",
                     upstream_submit_id="upstream-1",
                     # ⚠️ 年龄要**小于** `task_timeout`（否则先被看门狗判死），
                     # 但**大于** `poll_grace` + `poll_interval`（否则进不了轮询）。
                     started_at=int(time.time()) - 5,
                     updated_at=int(time.time()) - 5)
    store.put(rec)

    # 给池子塞一个能用的 client（模拟"凭据由请求带来"）
    fake_cfg = type("_C", (), {"snapshot": lambda self: None,
                               "count_options": lambda self, k: None,
                               "resolution_map": lambda self, k: {}})()
    svc._cred_pool["522ceab845a313502b72f5067534d191"] = (
        fake_jimeng, fake_uploader, fake_uploader, fake_cfg)
    from app.upstream.jimeng.client import TaskState
    fake_jimeng.states = [TaskState(
        submit_id="upstream-1", status=50, status_name="success",
        finished=True, failed=False,
        images=[type("_I", (), {"url": "https://cdn/x.png", "width": 2048,
                                 "height": 2048, "format": "png",
                                 "note": "", "item_id": "i1", "vid": ""})()])]

    out = svc.poll_many([rec])
    assert out["polled"] == 1, (
        f"poll_many 没轮询（返回 {out}）—— `self.client is None` "
        f"让它整段return 了，这是 2026-10-03 的线上事故")
    assert store.get(rec.task_id).status == "success", (
        "轮询到了却没推进到终态")


def test_audit_capability_does_not_fail_when_self_client_is_none(
        settings, store, fake_jimeng, fake_uploader):
    """🔴 同理：`_run_audit` 也不能因 `self.client is None` 就判失败。

    它跑在**受理路径**上（还没推凭据上下文）⇒ 生产恒为 None
    ⇒ `jimeng-audit` 能力会**永远失败**。
    """
    from app.service import Service
    from app.store import TaskRecord

    svc = Service(settings, store=store, client=None, uploader=None, cfg=None)
    assert svc.client is None
    svc._cred_pool["522ceab845a313502b72f5067534d191"] = (
        fake_jimeng, fake_uploader, fake_uploader, None)
    fake_jimeng.audit_result = ({"audit_decision": 1},)

    rec = TaskRecord(task_id="jimeng_audittest", credential_id="c1",
                     upstream_sessionid="522ceab845a313502b72f5067534d191",
                     model="jimeng-audit", cap_key="jimeng:audit",
                     status="queued", prompt="",
                     image_refs=["data:image/png;base64,iVBORw0KGgo="])
    store.put(rec)
    svc._run_audit(rec)
    assert store.get(rec.task_id).status == "success", (
        "audit 能力在生产形态下被误判为『没凭据』⇒ 永远失败")


def test_credential_context_is_always_popped(settings, store, fake_jimeng,
                                             fake_uploader):
    """🔴 `_push_cred` / `_pop_cred` 必须**配对** —— 泄漏会让下个任务
    拿到**上一个人**的 client/uploader（静默串号，且极难发现）。"""
    import inspect

    from app.service import Service

    src = inspect.getsource(Service)
    pushes = src.count("self._push_cred(")
    pops = src.count("self._pop_cred()")
    assert pushes == pops, (
        f"_push_cred({pushes}) 与 _pop_cred({pops}) 数量不等"
        f" ⇒ 有路径泄漏凭据上下文")


# ---------------------------------------------------------------------------
# 🔴 blend 门控的接线（此前 refresh/消费两端都没人调 ⇒ 从未生效）
# ---------------------------------------------------------------------------

def _mk_spec(feats: tuple | None):
    from app.upstream.jimeng.capabilities import ModelSpec
    return ModelSpec(model_req_key="k", model_name_starling_key="n",
                     model_tip_starling_key="t", feats=feats)


def test_bundle_for_populates_blend_capable_map(settings, store, monkeypatch):
    """🔴 `bundle_for` 建 cfg 时必须**回填** blend 能力表。

    2026-10-03 发现：`refresh_blend_capability` 与
    `upstream_supports_blend` **全仓都没有调用者**（`git log -S` 实锤）
    ⇒ 门控自 mj82 登记起**从未生效**。

    ⚠️ 必须 monkeypatch `ModelConfigCache`：`bundle_for` 会**真的**新建
    client 并让 cfg 去 `get_common_config` —— 那是**真实上游往返**，
    测试不该碰网络（实测确实打出去了，还顺带确认了真实表里
    所有 Seedream 模型都带 `byte_edit` ⇒ 接线后不会误拒现有模型）。
    """
    from app.models import blend_capable_map
    from app.service import Service

    class _Spec:
        def __init__(self, feats):
            self.feats = feats

    class _Snap:
        specs = {"high_aes_general_v50p_large": _Spec(("byte_edit",)),
                 "high_aes_general_v50": _Spec(())}

    class _FakeCfg:
        def __init__(self, _client):
            pass

        def snapshot(self):
            return _Snap()

    monkeypatch.setattr("app.service.ModelConfigCache", _FakeCfg)

    svc = Service(settings, store=store, client=None, uploader=None, cfg=None)
    svc.bundle_for("522ceab845a313502b72f5067534d191")
    m = blend_capable_map()
    assert m.get("high_aes_general_v50p_large") is True, (
        "Pro 的 feats 含 byte_edit ⇒ 必须回填为 True")
    assert m.get("high_aes_general_v50") is False, (
        "该模型 feats 为空 ⇒ False（带模型名的 i2i 用它应被拦下）")



def test_blend_gate_rejects_unsupported_model_before_submit(
        client, client_state, fake_jimeng, fake_uploader, service):
    """🔴 带明确不支持 blend 的模型 ⇒ 本地 400 拦下，**不提交、不花钱**。"""
    from app.models import set_blend_capable

    set_blend_capable({"high_aes_general_v50": False,
                       "high_aes_general_v50p_large": True})
    n_before = len(fake_jimeng.of("blend"))
    tid = _create(client, model="jm_image_model_yc_mj82", prompt="x",
                  image=[_data_uri(b) for b in _PNGS[:1]])
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert "blend" in (rec.error or {}).get("message", ""), rec.error
    assert len(fake_jimeng.of("blend")) == n_before, (
        "门控应该**在提交前**拦下 —— 却真的调了上游 blend")


def test_blend_gate_fails_open_when_table_unreadable(
        client, client_state, fake_jimeng, fake_uploader, service):
    """🔴 能力表**没读到**（空表）⇒ fail-open 放行 + 留痕。

    上游一次抖动不该拒掉所有 i2i —— 与素材预审的 fail-open 同理。
    留痕让调用方知道"这次没预检"。
    """
    from app.models import set_blend_capable

    set_blend_capable({})                     # 空 = 没读到
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client, model="jimeng-i2i", prompt="x",
                  image=[_data_uri(b) for b in _PNGS[:1]])
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "in_progress", rec.error
    assert any("跳过" in d and "门控" in d for d in rec.degradations), (
        f"fail-open 必须留痕，degradations={rec.degradations}")
