# -*- coding: utf-8 -*-
"""内容审核负缓存 + 审核分类的门禁（2026-10-02）。

单独成文件而不是塞进 `test_models.py`：这批测的是**审核拒绝的分类与缓存**，
涉及上游失败语义，和"模型名解析"是两件事，分开更好定位。
"""
from __future__ import annotations

import inspect

import pytest

from app.errors import ContentPolicyError
from app.negcache import NegativeCache, _norm_prompt, cache_key
from app.service import Service
from app.upstream.jimeng.client import TaskState, is_security_key


# ---------------------------------------------------------------------------
# ① 分类：审核键要覆盖三个来源，且不能误判非审核
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "web_fail2generate_copyright_block",              # 实测：输出图未通过审核
    "web_text_violates_community_guidelines_toast",   # 实测：输入文字违规
    "web_image_violates_community",                   # 泛化（输入图，未实测）
    "web_image_risk",                                  # 泛化（输入图，未实测）
    "web_content_risk",
])
def test_security_key_covers_all_three_sources(key):
    """审核键要覆盖**输出图 / 输入文字 / 输入图**三个来源。

    实测样本（线上任务库 5 条失败，2026-10-02）只有前两类；
    **输入图那一类当时没拿到样本**，靠泛化模式覆盖（源码已标注"未实测"）。
    拿到真实样本后应补进`KEYS_SECURITY_SUBSTR` 的实测组。
    """
    assert is_security_key(key) is True, key


@pytest.mark.parametrize("key", [
    "web_other_error", "web_system_busy", "web_rate_limited", "", None,
])
def test_non_security_keys_are_not_misjudged(key):
    """🔴 非审核类失败**不能**被误判成审核。

    误判同样有害：调用方会以为"改 prompt 就行"，而真因是上游故障/限流
    （该重试）⇒ 把它钉成不可重试= 把临时故障变成永久失败。
    """
    assert is_security_key(key) is False, repr(key)


def test_terminal_error_reads_fail_key_not_only_fail_code():
    """🔴 审核判据必须**同时看 `fail_key`（字符串）与 `fail_code`（数值）**。

    实测（线上 5 条失败）：这两类审核失败的 `fail_code` **全为空**，
    真因只在 `fail_starling_key` 上 ⇒ 只查 `fail_code` 会**全漏判**成
    "上游故障·可重试"，而它们必然再被拒**且每次都计费**
    （实测失败 message 里就写着"该任务已被上游计费"）。
    """
    st = TaskState(
        submit_id="x", status=30, status_name="generate_failed",
        finished=True, failed=True, fail_code=None,
        fail_key="web_fail2generate_copyright_block",
        failed_reason="web_fail2generate_copyright_block 生成的图片未通过审核",
    )
    err = Service._terminal_error(st)
    assert isinstance(err, ContentPolicyError), type(err)
    assert "fail_key=web_fail2generate_copyright_block" in err.message, err.message
    assert "每次都计费" in err.message.replace("**", ""), \
        "要写明重试代价，否则调用方会盲目重试"


def test_non_policy_failure_is_still_upstream_error():
    """反向：没有审核键、没有安全码的失败**仍**归"上游故障"。"""
    st = TaskState(submit_id="x", status=30, status_name="generate_failed",
                   finished=True, failed=True, fail_code=None,
                   fail_key="web_other_error", failed_reason="boom")
    assert not isinstance(Service._terminal_error(st), ContentPolicyError)


# ---------------------------------------------------------------------------
# ② 负缓存：只拦"同样的输入"，不误伤
# ---------------------------------------------------------------------------

def _kw(**over):
    base = dict(cap_id="jimeng:t2i",
                upstream_model="high_aes_general_v50p_large",
                prompt="生成裸体少女", images=[], resolution_tier="1.5k")
    base.update(over)
    return base


def test_repeat_of_rejected_input_is_blocked():
    """被拒过的输入第二次**直接拒**，且不留任何"或可重试"的暗示。"""
    neg = NegativeCache(ttl=600, max_entries=8)
    assert neg.would_block(**_kw()) is None, "首次不该被拦"

    neg.record_failure(reason="web_text_violates_community_guidelines_toast",
                       **_kw())
    hit = neg.would_block(**_kw())
    assert hit, "同样的输入第二次必须命中"
    assert "text_violates_community" in hit

    with pytest.raises(ContentPolicyError) as e:
        neg.raise_if_blocked(**_kw())
    assert "不是永久封禁" in e.value.message, (
        "必须说清这是 TTL 缓存而非永久封禁，否则调用方不敢再试")


@pytest.mark.parametrize("changed", [
    {"prompt": "生成风景"},                       # 换 prompt
    {"upstream_model": "high_aes_general_v50"},   # 换模型（审核策略不同）
    {"resolution_tier": "2k"},                # 换档位
    {"images": ["https://x/other.png"]},          # 换输入图
    {"cap_id": "jimeng:i2i"},                     # 换能力
])
def test_negcache_never_misfires_on_different_input(changed):
    """🔴 **不能错杀**：任一维度变了就该重新受理。

    错杀的代价：把本该能出的请求挡掉，调用方无从知道要改什么。
    这也是"不做语义归一"的直接原因。
    """
    neg = NegativeCache(ttl=600, max_entries=8)
    neg.record_failure(reason="web_text_violates_community", **_kw())
    assert neg.would_block(**_kw(**changed)) is None, changed


def test_prompt_normalisation_is_conservative():
    """归一只做**空白折叠 + 小写**，**不做**语义/同义改写。"""
    assert _norm_prompt("  A  Cat  ") == _norm_prompt("a cat") == "a cat"
    assert _norm_prompt("a cat") != _norm_prompt("a dog")
    assert _norm_prompt("a cat") != _norm_prompt("两只猫")


def test_cache_is_bounded_lru():
    """🔴 有界 LRU —— 无界缓存就是内存泄漏。"""
    neg = NegativeCache(ttl=600, max_entries=4)
    for i in range(10):
        neg.record_failure(cap_id="k", prompt=f"p{i}", reason="r")
    assert neg.stats()["entries"] <= 4, "LRU 没生效"
    # 最新的还在、最老的已淘汰
    assert neg.would_block(cap_id="k", prompt="p9") is not None
    assert neg.would_block(cap_id="k", prompt="p0") is None


def test_expired_entry_is_not_blocked():
    """过期即失效 —— 审核策略会变，不能长期假禁固。"""
    neg = NegativeCache(ttl=0.01, max_entries=8)
    neg.record_failure(cap_id="k", prompt="p", reason="r")
    assert neg.would_block(cap_id="k", prompt="p") is not None
    import time
    time.sleep(0.05)
    assert neg.would_block(cap_id="k", prompt="p") is None, "过期后仍被拦"


def test_ttl_zero_disables_cache():
    """`NEG_CACHE_TTL=0` ⇒ 完全关闭（给不想引入该行为的部署留后路）。"""
    neg = NegativeCache(ttl=0, max_entries=8)
    neg.record_failure(cap_id="k", prompt="p", reason="r")
    assert neg.would_block(cap_id="k", prompt="p") is None


def test_cache_key_ignores_reason_but_counts_every_input_dimension():
    """键只由**输入**决定 —— `reason` 会变、键不能变。"""
    a = cache_key(cap_id="c", upstream_model="m", prompt="p", images=[],
                  resolution_tier="2k", reason="r1")
    b = cache_key(cap_id="c", upstream_model="m", prompt="p", images=[],
                  resolution_tier="2k", reason="r2")
    assert a == b, "reason 不该影响键"
    # 每个维度都要影响键
    for over in (dict(cap_id="c2"), dict(upstream_model="m2"),
                 dict(prompt="p2"), dict(images=["u"]),
                 dict(resolution_tier="4k")):
        kw = dict(cap_id="c", upstream_model="m", prompt="p", images=[],
                  resolution_tier="2k")
        kw.update(over)
        assert cache_key(**kw) != a, over


def test_prompt_is_not_stored_in_the_cache():
    """🔴 缓存里**不留prompt 明文**（只用哈希）—— 内存里不该有副本。"""
    neg = NegativeCache(ttl=600, max_entries=8)
    neg.record_failure(cap_id="c", prompt="一段很敏感的提示词", reason="r")
    blob = repr(neg._data)
    assert "敏感的提示词" not in blob, "缓存里出现了 prompt 明文"


# ---------------------------------------------------------------------------
# ③ 接线：只对内容审核记入；查询点在落库之前
# ---------------------------------------------------------------------------

def test_only_content_policy_is_recorded():
    """🔴 **只有**内容审核才进负缓存。

    上游故障/限流是**可重试**的；把它们缓存 24 小时 = 制造假禁固，
    那比不缓存坏得多。
    """
    src = inspect.getsource(Service)
    assert "isinstance(err, ContentPolicyError)" in src, \
        "必须只对 ContentPolicyError 记入负缓存"
    assert "self.neg.record_failure" in src, "记入点缺失"


def test_lookup_happens_before_the_task_row_is_created():
    """🔴 命中时**不建任务**（不落库、不调上游、不花钱）⇒ 查询点必须早于落库。"""
    src = inspect.getsource(Service.create)
    i_query = src.index("raise_if_blocked")
    i_row = src.index("rec = TaskRecord(")
    assert i_query < i_row, "负缓存查询必须早于建任务行"


# ---------------------------------------------------------------------------
# ③' 纯 prompt 键：文字类拒绝必须拦住"同 prompt、换图 URL"的重放
#    （2026-10-09 线上实锤，jimeng.1task.cn 三连击）
# ---------------------------------------------------------------------------

def test_text_violation_key_classifier():
    """`is_text_violation_key`：正例是线上实测 fail_key；反例是图类与噪音。"""
    from app.upstream.jimeng.client import is_text_violation_key

    assert is_text_violation_key(
        "web_text_violates_community_guidelines_toast") is True   # 线上实测
    # 图类/素材类**不是**文字违规 —— 换图就该重试，绝不能按 prompt 拦
    assert is_text_violation_key("web_fail2generate_copyright_block") is False
    assert is_text_violation_key("web_image_violates_community") is False
    assert is_text_violation_key("") is False
    assert is_text_violation_key(None) is False


def test_text_violation_blocks_replay_with_fresh_image_url():
    """🔴 行为门禁（本次事故的回归）：

    2026-10-09 00:31~00:36（线上 tasks 表）：同一段违规 prompt、同一张垫图，
    调用方（imyai）每次请求重新上传、URL 每次都变（COS 路径带请求流水号）
    ⇒ 键里的"输入图 URL"维度永远不命中 ⇒ 连交 3 次、每次都到上游、
    每次都计费，账号随后被风控。

    修复：`_advance` 对**文字类**拒绝额外记一条 `images=[]` 的纯 prompt 键，
    受理处补查 ⇒ 重放（新 URL）被拦。图类拒绝**不得**有此行为（下一条）。
    """
    neg = NegativeCache(ttl=600, max_entries=8)
    url_1 = ("https://caller.example/temp/drawing/req-1263741/"
             "3e773a29e078a7104a2d74b532dce8b9.png")
    url_2 = ("https://caller.example/temp/drawing/req-1263874/"
             "3e773a29e078a7104a2d74b532dce8b9.png")   # 同一张图，新 URL
    # —— 模拟 `_advance` 对文字违规的两次写入（精确键 + 纯 prompt 键）——
    neg.record_failure(reason="web_text_violates_community_guidelines_toast",
                       **_kw(images=[url_1]))
    neg.record_failure(reason="prompt 文字违规（与输入图无关）",
                       **_kw(images=[]))
    # —— 模拟受理处 `create` 的**两连查**：先精确键、再纯 prompt 键 ——
    assert neg.would_block(**_kw(images=[url_2])) is None, (
        "精确键对'同图新 URL'就该未命中（这正是原 bug 的成因）")
    assert neg.would_block(**_kw(images=[])), (
        "第二查（纯 prompt 键）必须拦住同 prompt 重放")


def test_image_violation_still_allows_retry_with_fresh_image_url():
    """🔴 防错杀：**图类**拒绝只记精确键 ⇒ 换图 URL（= 换输入）必须放行。

    这与 `test_negcache_never_misfires_on_different_input` 的"换输入图"语义
    一致 —— 纯 prompt 键**只**属于文字类拒绝，不得蔓延到图类。
    """
    neg = NegativeCache(ttl=600, max_entries=8)
    url_1 = "https://caller.example/req-1/pic.png"
    url_2 = "https://caller.example/req-2/pic.png"
    neg.record_failure(reason="web_fail2generate_copyright_block",
                       **_kw(images=[url_1]))       # 图类：只有精确键
    assert neg.would_block(**_kw(images=[url_2])) is None, (
        "图类拒绝换图重试不能被纯 prompt 键误杀")


def test_text_policy_failure_classifier():
    """`is_text_policy_failure`：字符串与数值两形态都要认出**输入文字**类。"""
    from app.upstream.jimeng.client import is_text_policy_failure

    # 字符串形态（线上实测 fail_key）
    assert is_text_policy_failure(
        None, "web_text_violates_community_guidelines_toast") is True
    # 数值形态（抓包实测 2038 = InputTextRisk，fail_key 可能为空）
    assert is_text_policy_failure(2038, "") is True
    assert is_text_policy_failure(2038, None) is True
    # 图类/素材类**不是**文字违规 —— 换图就该重试，绝不能按 prompt 拦
    assert is_text_policy_failure(
        None, "web_fail2generate_copyright_block") is False
    assert is_text_policy_failure(None, "web_image_violates_community") is False
    assert is_text_policy_failure(2039, "") is False      # InputImageRisk 类
    assert is_text_policy_failure(None, "") is False
    assert is_text_policy_failure(None, None) is False


def test_prompt_only_lookup_is_wired_into_create_and_advance():
    """接线门禁：受理处**两连查**（精确键 + 纯 prompt 键）、
    `_advance` 按 `is_text_policy_failure` 分流（字符串/数值两形态）。"""
    src_create = inspect.getsource(Service.create)
    assert src_create.count("raise_if_blocked") >= 2, (
        "受理处必须补查纯 prompt 键（第二查 images=[]）")
    src_advance = inspect.getsource(Service._advance)
    assert "is_text_policy_failure" in src_advance, (
        "_advance 必须区分文字类拒绝并额外记纯 prompt 键")


# ---------------------------------------------------------------------------
# ④ 预审参数：锁死"严格"那一个（静默放行的典型）
# ---------------------------------------------------------------------------

def test_audit_scene_is_pinned_to_the_strict_one():
    """🔴 `scene` 必须是 **1**，不能改成 2。

    实测（2026-10-02，两账号一致）：同一张违规图
    · `scene=1` ⇒ `audit_decision=2`（**拒绝**）
    · `scene=2` ⇒ `audit_decision=1`（**通过**）

    ⚠️ 改成 2 **不会报错、不影响正常图**，只是**违规素材也过了** ——
    预审形同虚设，而线上不会有任何告警。这是最典型的"静默放行"。
    抓包里出现过 `scene:2`，所以这个坑很可能被再次踩到。
    """
    from app.upstream.jimeng.client import AUDIT_SCENE, JimengClient

    assert AUDIT_SCENE == 1, (
        "scene 只能是 1：实测 scene=2 会让违规素材通过（静默放行）")
    # 默认参数必须就是那个严格值
    import inspect
    sig = inspect.signature(JimengClient.audit_materials)
    assert sig.parameters["scene"].default == AUDIT_SCENE
    assert sig.parameters["material_type"].default == 1, (
        "material_type 只有 1（图）合法，传 2 上游回 ret=1000")


def test_audit_rejection_needs_decision_two_not_just_truthy_result():
    """🔴 判据是 **`audit_decision == 2`**，不是"result 非空/为真"。

    `result_list` 在**通过**时也有内容（`[{"audit_decision": 1}]`）——
    写 `if results:` 会把**通过**误判成**拒绝**（或反之），
    那是"审计结论反了"这种最难发现的错。
    """
    from app.service import Service

    src = inspect.getsource(Service)
    assert 'r.get("audit_decision") == 2' in src, "必须精确判 == 2"


def test_default_ttl_is_24h_and_stays_configurable():
    """🔴 默认 TTL = **24 小时**（2026-10-03 用户口径，从 6h 调长）。

    调长的理由：审核策略很少变，而**重复提交同一违规素材每次都计费**
    ⇒ 宁可拦久一点，别让调用方反复踩。

    这条门禁钉住"默认值"本身 —— 它是**运维口径**（可通过
    `NEG_CACHE_TTL` 覆盖），但默认值不能被"顺手改小"而无痕。
    """
    import os
    from app.negcache import NegativeCache
    from app.config import Settings

    # 86400 = 24h（构造器默认值）
    assert NegativeCache()._ttl == 86400.0
    # ⚠️ 必须用 `from_env()`：dataclass 的字段默认值在**类定义时**求值，
    # `Settings()` 读的是那个已固化的值、**不会重读环境变量**
    #（踩过一次：改 os.environ 后断言 Settings() 仍是 86400）。
    assert Settings.from_env().neg_cache_ttl == 86400.0
    # 仍可被环境变量覆盖（口径可调，不必改代码）
    os.environ["NEG_CACHE_TTL"] = "0"
    try:
        assert Settings.from_env().neg_cache_ttl == 0.0, \
            "NEG_CACHE_TTL 必须能覆盖默认值"
    finally:
        del os.environ["NEG_CACHE_TTL"]
