# -*- coding: utf-8 -*-
"""prompt 前置决策预审（`prompt_guard.py`，Jev 决策模型）的门禁。

🔴 测试**禁真网络**：`_remote_noul` 的 HTTP 层用 fake 注入（`guard._http`），
详见 `conftest` 与项目纪律（2026-10-03 实测：没打桩真打了一次上游）。
"""
from __future__ import annotations

import inspect

import pytest

from app.errors import ContentPolicyError
from app.prompt_guard import PromptGuard, _QUESTION
from tests.conftest import AUTH


# ---------------------------------------------------------------------------
# fake HTTP 层
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """duck-type httpx.Client：记录调用，按配置返回 noul / 抛异常 / 缺字段。"""

    def __init__(self, *, noul: float | None = 0.01,
                 exc: Exception | None = None,
                 omit_noul: bool = False) -> None:
        self.noul = noul
        self.exc = exc
        self.omit_noul = omit_noul
        self.calls = 0
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.payloads: list[dict] = []

    def post(self, url: str, *, headers: dict | None = None,
             json: dict | None = None, **_kw: object):
        self.calls += 1
        self.urls.append(url)
        self.headers.append(headers or {})
        self.payloads.append(json or {})
        if self.exc is not None:
            raise self.exc
        answers = {"text_policy": {"type": "noul"}}
        if not self.omit_noul:
            answers["text_policy"]["noul"] = self.noul
        return _FakeResp({"model": "bocha-jev-v1", "answers": answers,
                          "usage": {"input_tokens": 1, "output_tokens": 0}})


def _guard(http: FakeHttp, *, threshold: float = 0.5,
          enabled: bool = True, api_key: str = "sk-test") -> PromptGuard:
    g = PromptGuard(base_url="https://jev.test/v1", api_key=api_key,
                    model="bocha-jev-v1", timeout_s=1.0, threshold=threshold,
                    cache_ttl=600.0, cache_max=8, enabled=enabled)
    g._http = http
    return g


# ---------------------------------------------------------------------------
# ① 三条用户口径：拦截 / 缓存 / 失败放行
# ---------------------------------------------------------------------------

def test_blocks_high_noul_and_second_ask_hits_cache():
    """口径①②：违规 ⇒ 本地拦（不建任务不花钱）；**同 prompt 第二次不再调远端**。"""
    http = FakeHttp(noul=0.99)
    g = _guard(http)
    for _ in range(2):                      # 同一 prompt 问两次
        with pytest.raises(ContentPolicyError) as e:
            g.check("违规 prompt 文本")
        assert "不提交上游" in e.value.message.replace("**", "")
    assert http.calls == 1, "第二次必须命中缓存，不再打决策服务"
    assert g.stats()["cache_hits"] == 1
    assert g.stats()["blocks"] == 2


def test_allows_low_noul():
    http = FakeHttp(noul=0.03)
    g = _guard(http)
    assert g.check("一只橘猫在窗台上晒太阳") is None
    assert g.check("穿比基尼泳装的少女在海滩打排球") is None  # 归一后不同键 → 真调用
    assert http.calls == 2
    assert g.stats()["blocks"] == 0


def test_remote_failure_fails_open_with_note_and_cooldown():
    """口径③：**远端失败默认进入下一步** —— 放行 + 留痕。

    且带 60s 故障冷却：冷却期内直接跳过，**不再**每次受理都白等一次超时
    （否则 Jev 挂掉 = 每个受理都慢 8s）。
    """
    http = FakeHttp(exc=RuntimeError("connection refused"))
    g = _guard(http)
    note1 = g.check("某个 prompt")
    assert note1 is not None and "跳过预审" in note1
    note2 = g.check("另一个 prompt")        # 冷却期内：不再打远端
    assert note2 is not None and "故障冷却" in note2
    assert http.calls == 1, "冷却期内不应再次调用远端"
    st = g.stats()
    assert st["errors"] == 1 and st["error_skips"] == 1


def test_missing_noul_fails_open():
    """响应里解析不出判定 ⇒ 视同预审失败：放行 + 留痕，**绝不误拦**。"""
    http = FakeHttp(omit_noul=True)
    g = _guard(http)
    note = g.check("某个 prompt")
    assert note is not None and "无判定" in note
    assert g.stats()["blocks"] == 0


# ---------------------------------------------------------------------------
# ② 开关与边界
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [{"enabled": False}, {"api_key": ""}])
def test_disabled_or_unkeyed_never_calls_remote(kw):
    """未启用 / 没配 Key ⇒ 直接放行，**零网络调用**（部署没配 Key 也不炸）。"""
    http = FakeHttp(noul=0.99)
    g = _guard(http, **kw)
    assert g.check("任何 prompt") is None
    assert http.calls == 0
    assert g.stats()["disabled_skips"] == 1


def test_empty_prompt_never_calls_remote():
    http = FakeHttp(noul=0.99)
    g = _guard(http)
    assert g.check("") is None
    assert g.check("   ") is None
    assert http.calls == 0


def test_threshold_boundary_is_inclusive():
    """`noul >= 阈值` 即拦（0.5 算违规）—— 边界写明，别让 `>`/`>=` 漂移。"""
    http = FakeHttp(noul=0.5)
    g = _guard(http, threshold=0.5)
    with pytest.raises(ContentPolicyError):
        g.check("边界样本")


def test_http_error_status_fails_open():
    """非 2xx（如 401 被包装/5xx）同样 fail-open —— 预审是优化不是闸门。"""
    class _Resp:
        def raise_for_status(self):
            raise RuntimeError("HTTP 500")

        def json(self):  # pragma: no cover —— raise_for_status 先炸
            return {}

    class _Http:
        calls = 0

        def post(self, url, **_kw):
            self.calls += 1
            return _Resp()

    http = _Http()
    g = _guard(http)  # type: ignore[arg-type]
    assert g.check("某个 prompt") is not None
    assert http.calls == 1


# ---------------------------------------------------------------------------
# ③ 请求形态与题面（题面是实测校准过的，改坏 = 分数塌回去）
# ---------------------------------------------------------------------------

def test_request_shape_and_auth():
    http = FakeHttp(noul=0.01)
    g = _guard(http, api_key="sk-live")
    g.check("某个 prompt")
    assert http.urls == ["https://jev.test/v1/systemone"]
    assert http.headers[0]["Authorization"] == "Bearer sk-live"
    body = http.payloads[0]
    assert body["model"] == "bocha-jev-v1"
    assert body["state"] == "某个 prompt"
    q = body["questions"]["text_policy"]
    assert q["type"] == "noul"
    # 🔴 题面是校准版：泛化问法对擦边样本只给 0.62，必须钉住关键判据
    for keyword in ("捆绑束缚", "泳装", "恋物", "未成年人性化"):
        assert keyword in q["instructions"], f"审核口径题面缺关键词：{keyword}"


def test_module_question_matches_calibration():
    """模块级 `_QUESTION` 必须还是实测校准那版（0.99 vs 0.02 的那版）。"""
    assert _QUESTION["type"] == "noul"
    assert "泳装" in _QUESTION["instructions"]
    assert "捆绑束缚" in _QUESTION["instructions"]


# ---------------------------------------------------------------------------
# ④ Service 接线：受理链里、落库前；/stats 可见
# ---------------------------------------------------------------------------

def test_guard_check_is_wired_before_task_row():
    src = inspect.getsource(__import__("app.service", fromlist=["Service"]).Service.create)
    i_guard = src.index("self.guard.check(prompt)")
    i_row = src.index("rec = TaskRecord(")
    assert i_guard < i_row, "决策预审必须早于建任务行（命中不落库不花钱）"


def test_stats_exposes_prompt_guard():
    src = inspect.getsource(__import__("app.service", fromlist=["Service"]).Service.status)
    assert "prompt_guard" in src


def test_service_create_blocked_by_guard(service, store):
    """端到端：guard 判违规 ⇒ create 抛 ContentPolicyError，**任务不落库**。"""
    http = FakeHttp(noul=0.99)
    guard = _guard(http)
    service.guard = guard            # 换闸
    n_before = store.count()
    with pytest.raises(ContentPolicyError):
        service.create({"model": "jimeng-t2i", "prompt": "违规 prompt"},
                       credential="cred-test")
    assert store.count() == n_before, "被 guard 拦下的任务不得落库"


def test_service_create_guard_error_lands_in_degradations(service):
    """端到端：guard 挂了 ⇒ 放行建任务，degradations 留痕让调用方看见。"""
    http = FakeHttp(exc=RuntimeError("jev down"))
    service.guard = _guard(http)
    rec = service.create({"model": "jimeng-t2i", "prompt": "普通 prompt"},
                         credential="cred-test")
    assert rec.status == "queued"
    assert any("跳过预审" in d for d in (rec.degradations or [])), rec.degradations


def test_http_content_policy_returns_451(app_and_client):
    """🔴 HTTP 状态码 = **451**（2026-10-09 用户口径）。

    451 Unavailable For Legal Reasons —— "内容因合规原因不可用"正是其语义；
    与 400（参数写错）在状态码层面就区分开，调用方不用读 body 就知道该改
    prompt 而不是改参数。
    """
    app, client, _ = app_and_client
    app.state.service.guard = _guard(FakeHttp(noul=0.99))
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"model": "jimeng-t2i", "prompt": "违规 prompt"})
    assert r.status_code == 451, r.text
    err = r.json()["error"]
    assert err["type"] == "content_policy_violation"
    assert "不提交上游" in err["message"].replace("**", "")
