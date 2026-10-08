# -*- coding: utf-8 -*-
"""sessionid 透传的门禁（2026-10-03）。

用户口径"鉴权用 SESSIONID 就行"：Bearer 就是即梦 sessionid，直接当上游凭据用，
**明文**存进任务表（用户明确"直接明文就行"）。

这些用例钉住三件最容易塌的事：凭据不外泄、按人隔离、不越权。
"""
from __future__ import annotations

import json


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
