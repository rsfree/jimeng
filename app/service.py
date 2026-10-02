#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务编排：受理 → 入队 → 协调器推进 → 出结果。

## 分工

| 环节 | 谁做 | 为什么 |
|---|---|---|
| 校验 + 落库 | **请求线程**（`create`） | 调用方要立刻拿到 `task_id`，且请求内**零上游往返** |
| 下载输入图 / 上传 / 建任务 / 轮询 | **协调器线程**（`dispatch` / `poll`） | 建任务是**计费**动作，必须单点、受闸门约束、可重试 |

## 🔴 两条贯穿始终的纪律

1. **"被接受" ≠ "能跑通"**：上游 `ret=0` 只说明请求被受理，任务仍可能终态
   `status=30 generate_failed`，而且**照样计费**。⇒ 判成败只看 `task.status`。
2. **降级必须可见**：任何"请求了 A、实际做了 B"（张数吸附、图片归一化、
   能力表读不到退回冻结快照）都必须出现在响应的 `degradations` 里。
   静默降级等于让调用方按 A 的预期为 B 付费。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import models
from .ark import resolve_ark_model
from .config import Settings
from .errors import (
    AdapterError,
    CapabilityNotWiredError,
    CapabilityUnavailableError,
    ContentPolicyError,
    InvalidParameterError,
    RiskControlError,
    TaskNotFoundError,
    UpstreamQuotaError,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from .gate import UpstreamGate, build_gate
from .media import Blob, load_images, load_one, normalize, reuse_image_uri
from .observability import OBS
from .store import TaskRecord, TaskStore
from .upstream.jimeng import (
    DEFAULT_MODEL,
    DEFAULT_SIZE,
    DEFAULT_VIDEO_ASPECT_RATIO,
    DEFAULT_VIDEO_MODEL,
    DEFAULT_VIDEO_RESOLUTION,
    VIDEO_ASPECT_RATIOS,
    VIDEO_RESOLUTIONS,
    JimengAuthError,
    JimengClient,
    JimengContentError,
    JimengError,
    JimengParamError,
    JimengQuotaError,
    JimengRateLimitError,
    JimengRiskError,
    JimengTimeout,
    ImageXUploader,
    VodUploader,
    parse_size,
    resolve_video_commerce,
)
from .upstream.jimeng.capabilities import ModelConfigCache
from .upstream.jimeng.client import CODES_SECURITY

log = logging.getLogger(__name__)

#: `Service._submit` **认得的能力名** —— 唯一真相，门禁拿它对照能力注册表。
#:
#: 为什么要有这张表：能力在 `models.CAPABILITIES` 里注册、受理也通，**但 `_submit`
#: 没有对应分支**时，任务会在派发那一刻炸——而这条路径此前表现为一个
#: "上游不可用·可重试"的假象（见 `CapabilityNotWiredError`）。
#: 现在 `tests/test_video.py::test_every_capability_has_a_submit_route` 直接对照：
#: 注册表里出现新名字而这里没跟上 ⇒ 当场红。
SUBMIT_ROUTES: frozenset[str] = frozenset({
    "t2i", "i2i", "hd", "pro-hd", "outpaint",   # 图片族（后三者走后编辑族分支）
    "detail-fix",
    "t2v", "t2v-fast", "t2v-pro", "t2v-2.5-draft",  # 共用一条路径（T2V_VARIANTS）
    "vfi", "omni-video",
})

#: 受理请求允许的字段。
ACCEPTED_FIELDS = frozenset({
    "model", "prompt", "image", "size", "n", "seed", "negative_prompt",
    "source_task_id",
})
#: **视频接口额外**允许的字段（图片接口见到它们 = 传错了地方，400 说清楚）。
VIDEO_FIELDS = frozenset({"resolution", "duration", "aspect_ratio",
                          "target_fps", "video", "audio"})
#: **认得但本服务做不到**的字段 —— 见到就进 `degradations`（响亮降级），
#: 而不是当"未知字段"报错。它们来自 OpenAI/方舟图片接口的习惯写法。
KNOWN_UNSUPPORTED_FIELDS = frozenset({
    "watermark", "response_format", "quality", "style", "stream", "user",
    "sequential_image_generation", "max_images",
})

#: 建任务失败后最多重投几次（只对**可重试**错误计数）。
#: 3 是刻意小的数：重试一个付费动作的成本是非线性的。
DISPATCH_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# 凭证指纹
# ---------------------------------------------------------------------------


def fingerprint_secret(store: TaskStore) -> str:
    """取（或首次生成）凭证指纹用的密钥，**持久化在任务库里**。

    🔴 为什么不放 `.env`：它必须"首次启动自动生成、此后永不变"。
    若每次重启换一个，**所有历史任务会突然不属于任何人** ——
    调用方会看到自己的任务凭空 404，而任务其实好好躺在库里。

    ⚠️ 这里曾经直接用 `store._connect()` + 裸 SQL 读写 `meta` 表 —— 那是
    存储层还是 SQLite/JSON 时的写法。切到 SQLModel 后 `_connect` 不存在了，
    于是 `Service()` **一构造就 AttributeError**（而 `import app.service` 完全正常）。
    现在一律走存储层的公开接口 `get_meta`/`set_meta`；静态门禁（ruff SLF001）
    就是为了让"跨层摸私有成员"这类耦合当场现形。
    """
    key = "credential_fingerprint_secret"
    secret = store.get_meta(key)
    if secret:
        return secret
    # 首次启动：生成并落库。多进程同时首启时可能各生成一次，属无害竞态
    # （最后写入者胜；本服务单进程运行，见 gunicorn_conf.py 的 WORKERS=1）。
    secret = uuid.uuid4().hex
    store.set_meta(key, secret)
    return secret


def credential_id(api_key: str | None, secret: str) -> str:
    """把调用方的 Key 换成一个**不可逆指纹**。

    🔴 用 HMAC 而不是裸 sha256：API Key 是**低熵可枚举空间**，
    裸哈希等于给了一份可爆破的对照表。
    ⚠️ 明文 Key **永不落库**（任务表里只有这个指纹）。
    """
    if not api_key:
        return "anonymous"
    return hmac.new(secret.encode(), api_key.encode(), hashlib.sha256).hexdigest()


def new_task_id(model: str) -> str:
    """`jimeng_<32 位十六进制>`。

    形态对齐参考接口（`doubao_seedream_<32hex>` = 服务名 + uuid4 hex）。
    这里用固定的 `jimeng` 前缀而不是从 model 推：model 可以带别名/上游 key，
    推出来的前缀会五花八门，让"同一条链路"的任务看起来不像一类。
    """
    _ = model
    return f"jimeng_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------

def to_adapter_error(exc: BaseException) -> AdapterError:
    """上游异常 → 对外错误。**每条都带可执行的下一步**。"""
    if isinstance(exc, AdapterError):
        return exc
    if isinstance(exc, JimengAuthError):
        # 上游凭据失效是**部署问题**，不是调用方的参数错误 ⇒ 503 而非 401
        return CapabilityUnavailableError(
            "上游即梦凭据（sessionid）失效或已过期，本服务当前无法受理任务；"
            "请联系服务方更新 JIMENG_SESSIONID。",
            upstream="jimeng")
    if isinstance(exc, JimengRateLimitError):
        return UpstreamRateLimitError(str(exc), upstream="jimeng")
    if isinstance(exc, JimengQuotaError):
        return UpstreamQuotaError(
            f"{exc}；即梦积分/日额度已耗尽，**重试无效**，需充值或等额度按日重置。",
            upstream="jimeng")
    if isinstance(exc, JimengRiskError):
        return RiskControlError(
            f"{exc}；命中即梦风控，重试会延长标记，本服务已进入冷却期。",
            upstream="jimeng")
    if isinstance(exc, JimengContentError):
        return ContentPolicyError(str(exc), upstream="jimeng")
    if isinstance(exc, JimengParamError):
        return InvalidParameterError(
            str(exc) + "（该错误由上游返回，通常与 prompt / 输入图有关）",
            upstream="jimeng")
    if isinstance(exc, JimengTimeout):
        return UpstreamTimeoutError(str(exc), upstream="jimeng")
    if isinstance(exc, JimengError):
        return UpstreamUnavailableError(str(exc), upstream="jimeng")
    return UpstreamUnavailableError(
        f"未预期的上游错误（{type(exc).__name__}: {exc}）", upstream="jimeng")


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------


def _degradations(rec: TaskRecord) -> dict[str, Any]:
    """非空时才给 `degradations` 键（无值不给键，别给 `[]` 噪音）。"""
    return {"degradations": list(rec.degradations)} if rec.degradations else {}


def view(rec: TaskRecord) -> tuple[int, dict[str, Any]]:
    """任务记录 → (HTTP 状态码, 响应体)。

    四条刻意选择：
      · **非终态回 202**：调用方拿到 202 就该继续轮询，不该把排队态当结果；
      · **失败也回 200**：任务本身完成了（只是结果是失败）——
        请求没有出错，HTTP 层不该报错，否则调用方的重试逻辑会误触发；
      · **成功体只给 `url`**：`data[]` 元素与参考接口逐字一致。宽高/格式等真知识
        别的地方有（trace 里），不塞进这里 —— 多一个键就多一分"契约形状不同"的风险；
      · **五态都带顶层 `status`**（2026-09-22 补成功态）：queued / in_progress /
        success / failure / canceled 统一 —— 调用方**一个字段判全程**，不必按
        "有没有 `data`"推断终态。取值与 `store` 状态同名（小写）**是本服务的契约**；
        对接 New API 任务插件时由**插件侧**归一化到其大写枚举，本服务不做跨系统对齐。
    """
    deg = _degradations(rec)
    if rec.status == "queued":
        return 202, {"task_id": rec.task_id, "status": "queued", **deg}
    if rec.status == "in_progress":
        return 202, {"task_id": rec.task_id, "status": "in_progress", **deg}
    if rec.status == "canceled":
        return 200, {"task_id": rec.task_id, "status": "canceled", **deg}
    if rec.status == "failure":
        return 200, {
            "task_id": rec.task_id,
            "status": "failure",
            "error": rec.error or {"message": "任务失败（原因未记录）"},
            **deg,
        }

    usage: dict[str, Any] = {}
    _cap = models.REGISTRY.get(rec.cap_key)
    # 视频任务对外的量词是 `videos`，不是 `images` —— 契约形状按媒体分流
    usage["videos" if (_cap and _cap.media == "video") else "images"] \
        = len(rec.images)
    if rec.credits is not None:
        # 🔴 **这是上游回执里的 `forecast_generate_cost`，是"预估"，不是实际扣费。**
        # 实测它**严重高估**：i2i 报 55 / 实扣 **12**（t2i / hd 在 Lite 上实测**免费**，
        # 回执照样报 44 / 9）。按 `submit_id` 对账见
        # `POST /commerce/v1/benefits/user_credit_history`。名字里必须带 `forecast`。
        usage["forecast_credits"] = rec.credits
    return 200, {
        "status": "success",
        "data": [{"url": im["url"]} for im in rec.images],
        "created": rec.finished_at or rec.updated_at,
        "usage": usage,
        **deg,
    }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

#: 多张垫图时**同时上传**几张。与 `Capability.max_images`（blend 上限 4）同量级；
#: 再高没有收益 —— 每次上传要走 apply/put/commit 三段，而且并发取 STS 本来就有双检锁
#: （不会退化成每张各签一次），瓶颈在上游侧的往返，不在我们开几个线程。
MAX_UPLOAD_PARALLELISM = 4

#: `image` 数组的**全局**合理性上限。真正的额度由各能力声明
#: （`Capability.max_images`）决定；这里只拦"一次塞几百个 URL"这种明显不合理的请求，
#: 免得在还没解析出能力之前就先去做昂贵的图片校验。
MAX_INPUT_IMAGES = 4


#: 上传**一张**被限流时最多尝试几次（指数退避，封顶 8s）。
#:
#: 为什么要单独给上传加这一层：多张时走 `Executor.map` —— **任何一个异常都会冒泡**，
#: 所以"某一张被限流"会让**整批垫图失败**（而提交/轮询那条路本来就有退避）。
#: 上限 3 是刻意小的：3 次都还在限流，说明不该继续打（与 `DISPATCH_MAX_ATTEMPTS` 同一理由）。
UPLOAD_MAX_ATTEMPTS = 3

#: 一个任务最多**自动续生成**几次（action=2）。硬封顶：续生成是计费动作，
#: 不能让它变成无底洞；到顶就退回「用成功的图补齐」并如实写明。
CONTINUE_MAX = 3

#: 同步接口（`POST /v1/images/generations`）等待终态时的**库轮询间隔**（秒）。
#: 0.5s ⇒ 任务完成后最多再等半秒就被调用方看到；300s 预算下最多 600 次
#: 主键查询（PG 上微秒级，代价可忽略）。刻意不做成配置项：它是纯效率参数，
#: 多一个旋钮就多一份"以为调了会生效"的假配置面。
SYNC_WAIT_POLL_INTERVAL = 0.5


class Service:
    """服务组件集合 + 编排逻辑。请求线程与协调器线程共用同一实例。"""

    def __init__(self, settings: Settings, *, store: TaskStore | None = None,
                 client: JimengClient | None = None,
                 uploader: ImageXUploader | None = None,
                 vod: VodUploader | None = None,
                 gate: UpstreamGate | None = None,
                 cfg: ModelConfigCache | None = None) -> None:
        self.settings = settings
        self.store = store or TaskStore(
            settings.db_target,
            pool_size=settings.task_db_pool_size,
            max_overflow=settings.task_db_max_overflow,
            pool_recycle=settings.task_db_pool_recycle,
            pre_ping=settings.task_db_pool_pre_ping,
            connect_timeout=settings.task_db_connect_timeout,
        )
        self._cred_secret = fingerprint_secret(self.store)
        self.gate = gate or _build_gate(settings)
        self.client = client
        self.uploader = uploader
        self.cfg = cfg
        if settings.upstream_configured and self.client is None:
            self.client = JimengClient(
                sessionid=settings.jimeng_sessionid,
                cookie=settings.jimeng_cookie,
                base=settings.jimeng_base_url,
                workspace_id=settings.jimeng_workspace_id,
                poll_interval=settings.jimeng_poll_interval,
                capture_upstream=settings.otel_capture_upstream,
            )
            self.cfg = ModelConfigCache(self.client)
        if self.client is not None and self.uploader is None:
            self.uploader = ImageXUploader(self.client)
        #: VOD 上传（视频/音频 → vid），与 ImageX **共享同一把 STS**。
        #: 测试可注入假上传器；生产自动构建。
        self.vod = vod
        if self.vod is None and self.client is not None \
                and self.uploader is not None:
            self.vod = VodUploader(self.client, imagex=self.uploader)

    # ------------------------------------------------------------------ 能力表

    def refresh_blend_capability(self) -> int:
        """从**服务端能力表**回填"哪些上游模型支持 blend（图生图）"。

        数据源 = `get_common_config` 的 `feats`（含 `byte_edit`），**只读零成本**。
        🔴 为什么必须读服务端而不能写死：`feats` 是上游的权威声明，
        而"表里没有"有两种可能—— 真不支持，或我们没读到。
        两种都按"不支持"处理（保守拒绝）并让报错说清替代路径：
        **猜一个 blend 能不能用，代价是建任务后才炸**。
        返回回填的条目数（0 = 没读到能力表，此时全部按不支持处理）。
        """
        if self.cfg is None:
            models.set_blend_capable({})
            return 0
        snap = self.cfg.snapshot()
        if snap is None:
            models.set_blend_capable({})
            return 0
        models.set_blend_capable({
            key: bool(spec.feats and "byte_edit" in spec.feats)
            for key, spec in snap.specs.items()
        })
        return len(snap.specs)

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        for obj in (self.vod, self.uploader, self.client):
            closer = getattr(obj, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110
                    pass

    def status(self) -> dict:
        return {
            "upstream_configured": self.settings.upstream_configured,
            "auth_enabled": self.settings.auth_enabled,
            "concurrency": self.settings.jm_concurrency,
            "tasks": {"active": self.store.count_active(),
                      "total": self.store.count()},
            "gate": self.gate.stats(),
            "model_config": self.cfg.stats() if self.cfg else None,
            "observability": OBS.status(),
        }

    # ------------------------------------------------------------------ 受理

    def credential_of(self, api_key: str | None) -> str:
        return credential_id(api_key, self._cred_secret)

    def create(self, body: dict[str, Any], *, credential: str,
               dry_run: bool = False, video: bool = False,
               preset_degradations: list[str] | None = None,
               ark_model: str | None = None) -> TaskRecord:
        """校验 + 落库，返回任务记录。**请求内零上游往返。**

        `video=True` 表示走**视频端点**：候选池切到视频族、额外放行
        `resolution` / `duration` / `aspect_ratio` / `source_task_id` /
        `target_fps`（图片端点见到它们 = 传错了地方，400 说清楚）。

        `preset_degradations` / `ark_model` 给 **Ark 门面**用：
        形态翻译中产生的降级说明要带进任务记录；调用方请求的 Ark 模型名
        （如 `doubao-seedance-2-0-mini-260615`）要原样留存，查询时回显。

        🔴 刻意**不在这里下载输入图**：异步接口的语义就是"受理即返回"，
        把下载塞进请求会让受理时间随上游网络抖动。图片拉取失败会在任务里
        体现为 `failure`（附明确原因），而不是让人在受理时干等。
        """
        if not isinstance(body, dict):
            raise InvalidParameterError("请求体必须是 JSON 对象")

        # 🔴 **显式 `null` 等价于"没给"**，用默认值。
        # 否则调用方写 `"n": null` / `"prompt": null` 会被判成参数错误，
        # 而语义上它只是"这个字段我没设置"。（HTTP 层的 Pydantic schema 会把
        # 未提供的可选字段填成 None，所以这一步是必需的，不是可选优化。）
        body = {k: v for k, v in body.items() if v is not None}

        unknown = set(body) - ACCEPTED_FIELDS - KNOWN_UNSUPPORTED_FIELDS \
            - (VIDEO_FIELDS if video else frozenset())
        if unknown:
            raise InvalidParameterError(
                f"未知字段 {sorted(unknown)}；本接口接受 "
                f"{sorted(ACCEPTED_FIELDS | (VIDEO_FIELDS if video else frozenset()))}"
                f"（其中 "
                f"{sorted(KNOWN_UNSUPPORTED_FIELDS)} 是**认得但本服务做不到**的字段，"
                f"传了会进 `degradations` 而不是报错）",
                param=sorted(unknown)[0])
        if video:
            # 视频端点**不收**图片族字段：size（视频用 resolution 档位）、
            # negative_prompt（视频草稿没有该字段，实抓确认）。
            # `image` 已放行（全能参考吃图片素材）。
            banned = sorted(set(body) & {"size", "negative_prompt"})
            if banned:
                raise InvalidParameterError(
                    f"字段 {banned} 不属于视频接口：视频用 resolution/duration/"
                    f"aspect_ratio 表达画面形态，草稿里没有"
                    f"{'、'.join(banned)} 的对应位置（实抓确认）。",
                    param=banned[0])
        else:
            misplaced = sorted(set(body) & VIDEO_FIELDS)
            if misplaced:
                raise InvalidParameterError(
                    f"字段 {misplaced} 只属于**视频请求**（方舟门面 "
                    f"`/api/v3/contents/generations/tasks`；图片接口没有这些参数）。",
                    param=misplaced[0])

        degradations: list[str] = list(preset_degradations or [])
        for k in sorted(set(body) & KNOWN_UNSUPPORTED_FIELDS):
            if body[k] is not None:
                degradations.append(
                    f"参数 {k}={body[k]!r} 本服务不支持（即梦这条链路没有对应能力），已忽略；"
                    f"不要按它的语义预期结果。")

        if not self.settings.upstream_configured:
            raise CapabilityUnavailableError(
                "本服务未配置上游即梦凭据（JIMENG_SESSIONID），无法受理任务。",
                upstream="jimeng")

        image = self._validate_image(body.get("image"))
        prompt = body.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise InvalidParameterError("prompt 必须是字符串", param="prompt")
        prompt = (prompt or "").strip()

        #: 🔴 视频受理的模型解析（2026-09-24 起对外只有方舟模型名）：
        #: `doubao-seedance-*` 前缀 ⇒ 方舟语义 —— 带 source_task_id/target_fps ⇒
        #: 补帧（视频生视频）；带素材 ⇒ 全能参考；否则按方舟名分流到即梦档位。
        #: `jimeng-*` 内部名仍然直通（内部调用/测试兼容层）。
        has_materials = bool(body.get("video") or body.get("audio")
                             or image)
        eff_model = body.get("model")
        if video and eff_model \
                and str(eff_model).lower().startswith("doubao-seedance"):
            if body.get("source_task_id") or body.get("target_fps") is not None:
                eff_model = "jimeng-vfi"
            elif has_materials:
                eff_model = "jimeng-omni-video"
            else:
                eff_model = resolve_ark_model(str(eff_model))[0]
        if not eff_model:
            if video:
                if has_materials:
                    eff_model = "jimeng-omni-video"
                elif body.get("source_task_id"):
                    eff_model = "jimeng-vfi"
                else:
                    eff_model = "jimeng-t2v"
            elif body.get("source_task_id"):
                eff_model = "jimeng-detail-fix"   # 图片端点：引用既有作品修复
        cap, upstream_model = models.resolve(eff_model, has_image=bool(image),
                                            n_images=len(image), video=video)
        #: 🔴 **占位名兜底的留痕**（2026-10-02）。调用方显式写了一个我们没登记的
        #: 型号（`seedream-9-9-ultra` / `doubao-seedream-5-0-pro-260628` …），
        #: 按占位处理 ⇒ 兜底到默认档出图。**兜底本身是允许的**（免费档，不掏钱），
        #: 但**不能静默** —— 否则调用方拿到的是它没点的模型却毫无察觉。
        #: 现象（本服务 v0.1.9 实测）：`seedream-9-9-ultra` 返回 200 且出图成功
        #: （submit_id 9154eaa2），响应体里 `degradations` 为**空**。
        #: `take_fallback_note()` 取走即清 ⇒ 同一请求里不会重复写。
        if (note := models.take_fallback_note()) is not None:
            degradations.append(note)

        if cap.prompt_required and not prompt:
            raise InvalidParameterError(
                f"model {cap.api_id}（{cap.title}）需要 prompt，但本次没给或为空。",
                param="prompt")

        #: 补帧（vfi）的**源任务校验** —— 只做本地库读（请求内零上游往返）。
        #: 三件引用（vid / item_id / origin_history_id）+ 源草稿，缺一不可；
        #: 源任务必须属于**当前凭证**（跨凭证引用 = 变相枚举别人的任务）。
        src_rec: TaskRecord | None = None
        local_video_ref: str | None = None
        if cap.name == "detail-fix":
            # 细节修复：**只支持引用形态**（item_id+origin_history_id，实测唯一
            # 能过的形态；单组件+origin_image 两次 generate_failed 且计费）。
            src_id = body.get("source_task_id")
            if not isinstance(src_id, str) or not src_id.strip():
                raise InvalidParameterError(
                    f"model {cap.api_id}（细节修复）必须给 source_task_id："
                    f"本服务一个**已成功**的图片任务 —— 引用形态需要它的 "
                    f"item_id + origin_history_id（实测唯一能过的形态；"
                    f"直接贴 image 走 origin_image 形态，实测必失败）。",
                    param="source_task_id")
            src_rec = self.store.get_scoped(src_id.strip(), credential)
            if src_rec is None:
                raise InvalidParameterError(
                    f"source_task_id {src_id!r} 不存在，或不属于当前 API Key。",
                    param="source_task_id")
            img0 = src_rec.images[0] if src_rec.images else {}
            src_cap = models.REGISTRY.get(src_rec.cap_key)
            if (not src_cap or src_cap.media != "image"
                    or src_rec.status != "success" or not img0.get("url")):
                raise InvalidParameterError(
                    f"source_task_id {src_id!r} 不是已成功的图片任务"
                    f"（status={src_rec.status}，model={src_rec.model}）。",
                    param="source_task_id")
            if not img0.get("item_id") or not src_rec.upstream_history_id:
                raise InvalidParameterError(
                    f"源任务 {src_id!r} 缺少引用所需 item_id/history"
                    f"—— 可能是旧版本生成的任务，请重新生成后再修复。",
                    param="source_task_id")
        elif cap.name == "vfi":
            src_id = body.get("source_task_id")
            vfi_videos = self._validate_refs(body.get("video"), "video")
            if src_id and vfi_videos:
                raise InvalidParameterError(
                    "补帧的 source_task_id 与 video 二选一："
                    "前者补本服务生成的视频，后者补本地/外部视频（实测支持）。",
                    param="video")
            if vfi_videos:
                # ✅ 本地/外部视频补帧（2026-09-20 真跑实证：vid-only 形态，
                # 72s 出片）。只接受 1 条。
                if len(vfi_videos) != 1:
                    raise InvalidParameterError(
                        "补帧一次只接受 1 条源视频。", param="video")
                local_video_ref = vfi_videos[0]
                if not prompt:
                    raise InvalidParameterError(
                        "本地视频补帧必须给 prompt（无源任务可沿用）。",
                        param="prompt")
            elif isinstance(src_id, str) and src_id.strip():
                src_rec = self.store.get_scoped(src_id.strip(), credential)
                if src_rec is None:
                    raise InvalidParameterError(
                        f"source_task_id {src_id!r} 不存在，或不属于当前 API Key。",
                        param="source_task_id")
                img0 = src_rec.images[0] if src_rec.images else {}
                if (src_rec.cap_key != "jimeng:t2v" or src_rec.status != "success"
                        or not img0.get("url")):
                    raise InvalidParameterError(
                        f"source_task_id {src_id!r} 不是已成功的视频任务"
                        f"（status={src_rec.status}，model={src_rec.model}）—— "
                        f"补帧只能引用本服务 t2v 任务的产物。",
                        param="source_task_id")
                if (not img0.get("vid") or not img0.get("item_id")
                        or not src_rec.upstream_history_id or not src_rec.draft_json):
                    raise InvalidParameterError(
                        f"源任务 {src_id!r} 缺少补帧所需的引用"
                        f"（vid/item_id/history/draft）—— 可能是旧版本生成的任务，"
                        f"请用当前服务重新生成源视频后再补帧。",
                        param="source_task_id")
                if not prompt:
                    # 产物补帧：省略时沿用源任务的（实抓里就是同一个）
                    prompt = src_rec.prompt
                    degradations.append(
                        "prompt 省略 ⇒ 已沿用源视频任务的提示词"
                        f"「{prompt}」（实抓形态：补帧请求原样带源 prompt）。")
            else:
                raise InvalidParameterError(
                    f"model {cap.api_id}（补帧）需要 source_task_id"
                    f"（本服务已成功的视频任务）或 video"
                    f"（本地/外部视频，实测支持）—— 二选一。",
                    param="source_task_id")
            tfps = body.get("target_fps")
            if tfps is None:
                tfps = 60                       # 实抓：24 → 60
            if isinstance(tfps, bool) or not isinstance(tfps, int) \
                    or not (24 <= tfps <= 120):
                raise InvalidParameterError(
                    f"target_fps 必须是 24..120 的整数，实得 {tfps!r}（实抓 60）。",
                    param="target_fps")
            if body.get("seed") is not None:
                degradations.append(
                    "补帧草稿没有 seed 字段（实抓确认）⇒ seed 已忽略。")

        extra_info: dict[str, Any] | None = None
        if cap.name == "detail-fix":
            assert src_rec is not None
            extra_info = {
                "item_id": str(src_rec.images[0].get("item_id")),
                "history_id": src_rec.upstream_history_id,
            }
        if cap.name == "omni-video":
            # 全能参考素材清单（引用原样落库，派发时才下载/上传 —— 受理零上游往返）。
            omni: list[dict[str, str]] = (
                [{"kind": "image", "ref": r} for r in image]
                + [{"kind": "video", "ref": r}
                   for r in self._validate_refs(body.get("video"), "video")]
                + [{"kind": "audio", "ref": r}
                   for r in self._validate_refs(body.get("audio"), "audio")])
            if not omni:
                raise InvalidParameterError(
                    "全能参考至少要一个参考素材（image/video/audio）；"
                    "纯文字请走 jimeng-t2v。", param="video")
            if len(omni) > 6:
                raise InvalidParameterError(
                    f"参考素材最多 6 个（实抓样本 4 个：2 视频+1 图+1 音频），"
                    f"本次 {len(omni)} 个。", param="video")
            extra_info = {"omni": omni}
        if cap.name == "vfi":
            assert src_rec is not None or local_video_ref
            extra_info = {"target_fps": tfps}
            if local_video_ref:
                extra_info["local_video"] = local_video_ref
            else:
                extra_info.update({
                    "source_task_id": src_rec.task_id,
                    "vid": src_rec.images[0].get("vid"),
                    "item_id": str(src_rec.images[0].get("item_id")),
                    "history_id": src_rec.upstream_history_id,
                    "source_submit_id": src_rec.upstream_submit_id,
                })
        duration_ms: int | None = None
        aspect_ratio: str | None = None
        if cap.media == "video":
            if body.get("aspect_ratio") and cap.name == "vfi":
                degradations.append(
                    "补帧组件没有 video_aspect_ratio 字段（实抓确认）"
                    "⇒ aspect_ratio 已忽略（沿用源视频画面）。")
            # 分辨率/时长的默认值：补帧**沿用源任务**（实抓形态），
            # t2v 用实抓档位（720p × 4s）。
            resolution = body.get("resolution") or (
                (src_rec.size if src_rec else None)
                or ("720p" if cap.name == "omni-video"
                    else DEFAULT_VIDEO_RESOLUTION))
            if not isinstance(resolution, str) or resolution not in VIDEO_RESOLUTIONS:
                raise InvalidParameterError(
                    f"resolution 只接受 {list(VIDEO_RESOLUTIONS)}，实得 {resolution!r}。",
                    param="resolution")
            size = resolution                      # 复用 size 列存分辨率档位
            duration = body.get("duration")
            if duration is None:
                duration = ((src_rec.duration_ms or 4000) // 1000
                            if src_rec
                            else (5 if cap.name == "omni-video" else 4))
                # omni 实抓档位 5s；t2v 实抓档位 4s
            if isinstance(duration, bool) or not isinstance(duration, int) \
                    or duration < 1:
                raise InvalidParameterError(
                    "duration 必须是 >=1 的整数（秒）", param="duration")
            if cap.name == "vfi":
                if local_video_ref:
                    # 本地视频补帧：实测组合仅 720p×4s（源视频 5s 也按 4s 提交
                    # 成功）；改档位没有依据，拒绝。
                    # 🔴 检查必须用 `duration`（秒）而不是 duration_ms —— 后者
                    # 在此处尚未赋值（下方 665 行才算），恒 None，之前这条
                    # 恒 400 的 bug 被 vfi 门面用例首次踩到（2026-09-24）。
                    if resolution != DEFAULT_VIDEO_RESOLUTION \
                            or duration != 4:
                        raise InvalidParameterError(
                            "本地视频补帧的实测档位是 720p×4s（resolution/"
                            "duration 请省略让服务取默认）。",
                            param="duration")
                else:
                    # 产物补帧：实抓形态是"沿用源任务"：显式改档位没有依据。
                    assert src_rec is not None
                    if resolution != (src_rec.size or DEFAULT_VIDEO_RESOLUTION):
                        raise InvalidParameterError(
                            f"补帧的 resolution 必须与源任务一致"
                            f"（源任务 {src_rec.size}，本次 {resolution}）。",
                            param="resolution")
                    if duration_ms is not None and src_rec.duration_ms \
                            and duration_ms != src_rec.duration_ms:
                        raise InvalidParameterError(
                            f"补帧的 duration 必须与源任务一致"
                            f"（源任务 {src_rec.duration_ms // 1000}s，"
                            f"本次 {duration}）。",
                            param="duration")
            else:
                try:
                    resolve_video_commerce(
                        cap.video_model or DEFAULT_VIDEO_MODEL,
                        resolution, duration)
                except JimengError as e:
                    raise InvalidParameterError(str(e), param="duration") from e
            duration_ms = duration * 1000
            aspect = body.get("aspect_ratio") or DEFAULT_VIDEO_ASPECT_RATIO
            if not isinstance(aspect, str) or aspect not in VIDEO_ASPECT_RATIOS:
                raise InvalidParameterError(
                    f"aspect_ratio 只接受 {list(VIDEO_ASPECT_RATIOS)}，"
                    f"实得 {aspect!r}。⚠️ 只有 16:9 有实抓样本，其余比例未验证。",
                    param="aspect_ratio")
            aspect_ratio = aspect
        else:
            size = body.get("size") or _default_size()
            try:
                parse_size(str(size))
            except JimengError as e:
                raise InvalidParameterError(str(e), param="size") from e

        #: ⚠️ **不传 `n` 与传 `n=...` 是两种情况，必须分开处理。**
        #:
        #: 🔴 契约：**不传 `n` ⇒ 取该模型的最小合法值**（通常就是 1），
        #: **绝不采用上游的 `default_generate_count`** —— 实测各家不同
        #: （5.0 Pro 默认 2、5.0 Lite 默认 4），照它的默认走，调用方会按"1 张"的
        #: 预期收到 2~4 张的账单。**默认必须是最省的那个。**
        #: 另外：这种情况**不该**产生"已吸附"告警 —— 调用方什么都没要求，
        #: 我们说"把你的 1 改成了 2"只会让人困惑。
        n_given = body.get("n")
        if n_given is None:
            n_raw: int | None = None
        else:
            if isinstance(n_given, bool) or not isinstance(n_given, int) or n_given < 1:
                raise InvalidParameterError("n 必须是 >=1 的整数", param="n")
            n_raw = n_given

        seed = body.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise InvalidParameterError("seed 必须是整数", param="seed")

        n = n_raw or 1
        if cap.media == "video":
            # 🔴 视频草稿**没有张数字段**（实抓确认无 `gen_option`，
            # `batchNumber=1` 只是埋点）⇒ 一次一条。多要的响亮降级，
            # 绝不假装能给 —— 静默只出 1 条比明确拒绝更害人。
            if n_raw is not None and n_raw > 1:
                degradations.append(
                    f"视频任务请求 n={n_raw}，但视频草稿没有张数字段"
                    f"（实抓确认无 gen_option）⇒ 按 n=1 处理；"
                    f"要多条视频请发多个任务。")
            n = 1
        elif self.cfg is not None:
            # 🔴 **所有能力**的张数都走同一条路：草稿里写的都是
            # `abilities.gen_option.gen_count`（**组件级**字段，与具体 ability 平级）。
            # 早先只放行 t2i（后来才加上 i2i），后编辑族则写着"只接受 1" ——
            # 而那句"扩图固定出 4 张"其实是**我们没传张数、上游用了默认值**。
            # 同一个坑（把"我们没传"误读成"上游不支持"）已经踩过两次，别再犯。
            # 🔴 2026-10-02：`model_key` 的取值范围从"只有 t2i"扩到
            # **"任何解析出了上游模型的能力"**（目前 t2i + i2i）。原先 i2i
            # 恒用 DEFAULT_MODEL，导致换模型只对文生图生效。
            # 张数选项**逐模型**查（不同模型声明不同：Lite 1..8 /mj82 只有 [4]）。
            model_key = upstream_model or DEFAULT_MODEL
            opts = self.cfg.count_options(model_key)
            declared = self.cfg.count_options_declared(model_key)
            note = self.cfg.degradation_note(model_key)
            if note:
                degradations.append(note)
            from .upstream.jimeng.client import resolve_count  # noqa: PLC0415
            if n_raw is None:
                n = min(opts) if opts else 1      # 默认 = **最小合法值**，且不留吸附告警
            else:
                n, warn = resolve_count(model_key, n_raw, opts)
                if warn:
                    degradations.append(warn)
            if declared is None:
                # 🔴 **有些模型就是不声明张数选项**（实测 `..._v30l_art_fangzhou:...`
                # 的 `generate_count_options` 为 null）。这种模型上 `gen_count`
                # 传了也是**白传**（上游忽略、按自己的默认值出图）。
                # `count_options()` 会退回冻结快照、所以**永远非空** ——
                # 用它做判断会把"不可控"伪装成"可控"。必须用不做兜底的
                # `count_options_declared()` 才能发现，并且**留痕**。
                degradations.append(
                    f"模型 {model_key} **未声明张数选项**"
                    f"（服务端 generate_count_options 为空）⇒ 该模型的张数不可控，"
                    f"上游按自己的默认值出图；本服务请求的 n={n} 可能不生效。"
                    f"要控张数请换一个声明了张数选项的模型。")
            elif len(declared) == 1 and n_raw is not None:
                # 🔴 2026-10-02（mj82 触发）：**只声明了一个取值**（如 `[4]`）
                # 与"声明了一串（1..4）"是**完全不同的语义** —— 前者张数**不可控**，
                # 任何 n 都会被吸附成那一个值；后者才是"可自由选"。
                # `declared is None` 那条**抓不到这一种**（它是 `[4]`，非空）。
                # 症状：调用方要 1 张，我们报"已吸附为 4"，他以为拿到了 1 张 ——
                # 实际 4 张，**且按 4 张计费**。这属于"让调用方算错成本"，
                # 必须用一句话说清"这个模型的张数由上游定，你改不了"。
                degradations.append(
                    f"模型 {model_key} **张数不可控**（服务端只声明了一个取值 "
                    f"{list(declared)}）⇒ 无论请求 n 多少，实际都按 {declared[0]} 张"
                    f"出图并计费（本次 n={n_raw} 已按此处理）。"
                    f"要自由控制张数请换一个声明了多个取值的模型。")

        now = int(time.time())
        rec = TaskRecord(
            task_id=new_task_id(cap.api_id),
            credential_id=credential,
            model=cap.api_id,
            cap_key=cap.key,
            upstream_model=upstream_model,
            status="queued",
            prompt=prompt,
            image_refs=image,
            size=str(size),
            n=n,
            seed=seed,
            negative_prompt=str(body.get("negative_prompt") or ""),
            duration_ms=duration_ms,
            aspect_ratio=aspect_ratio,
            extra_json=(json.dumps({**(extra_info or {}),
                                    **({"ark_model": ark_model}
                                       if ark_model else {})},
                                   ensure_ascii=False)
                       if (extra_info or ark_model) else None),
            degradations=degradations,
            created_at=now,
            updated_at=now,
        )
        self.store.put(rec)
        OBS.info("task accepted",
                 task_id=rec.task_id, model=rec.model, capability=rec.cap_key,
                 has_image=bool(image), image_count=len(image),
                 size=rec.size, n=rec.n,
                 degradations=len(degradations), dry_run=dry_run)
        return rec

    @staticmethod
    def _validate_refs(raw: Any, field: str) -> list[str]:
        """校验 video/audio 素材引用数组（形态同 image：非空字符串数组）。"""
        if raw is None:
            return []
        if isinstance(raw, str):
            raise InvalidParameterError(
                f"{field} 必须是**数组**；单条请写 `{field}: [\"https://…\"]`。",
                param=field)
        if not isinstance(raw, list):
            raise InvalidParameterError(f"{field} 必须是数组", param=field)
        out: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise InvalidParameterError(
                    f"{field}[{i}] 必须是非空字符串（http(s) URL / data URI）",
                    param=field)
            out.append(item.strip())
        return out

    @staticmethod
    def _validate_image(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            raise InvalidParameterError(
                "image 必须是**数组**（文生图传 `[]`）；收到的是字符串。"
                "若要传单张图请写 `\"image\": [\"https://…\"]`。",
                param="image")
        if not isinstance(raw, list):
            raise InvalidParameterError("image 必须是数组", param="image")
        out: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise InvalidParameterError(
                    f"image[{i}] 必须是非空字符串（http(s) URL / data URI / base64）",
                    param="image")
            out.append(item.strip())
        if len(out) > MAX_INPUT_IMAGES:
            raise InvalidParameterError(
                f"image 最多 {MAX_INPUT_IMAGES} 张（收到 {len(out)} 张）。"
                f"⚠️ 各能力的上限可能更小：只有 `jimeng-i2i`（图生图）支持多张垫图，"
                f"后编辑三族（hd / pro-hd / outpaint）只接受 1 张 —— "
                f"给多了会在能力校验那一步明确报错。",
                param="image")
        return out

    # ------------------------------------------------------------------ 查询

    def get_for_credential(self, task_id: str, credential: str) -> TaskRecord:
        """按 id 取任务 —— **内部映射：API Key 指纹 → 任务归属**。

        `credential` **必填**（2026-09-23 收紧）：走 `store.get_scoped()`，
        取不到一律 404。此前允许 `None`（"免鉴权读、只按 id 取"），
        由 HTTP 层在"没带 Authorization"时传入；那条放宽口径已取消，
        这里也就不再保留那个分支 —— 留着就是一条**没有 HTTP 入口却仍然存在**的活口子，
        将来有人把某个 GET 的依赖改回可选就会静默复现。

        ⚠️ "不存在"与"不属于当前 Key"**刻意合并成同一个 404**：
        区分开就等于告诉别人"这个 id 是存在的"，那正是枚举的前置条件。

        🔴 **本地拦，不问上游**：放行到上游就是用错的钥匙去查，
        返回的 404/空**无法区分**"任务真没了"与"钥匙不对"，
        而且把跨凭证隔离交给了别人的实现去兜。
        """
        rec = self.store.get_scoped(task_id, credential)
        if rec is None:
            raise TaskNotFoundError(
                f"任务 {task_id} 不存在，或不属于当前 API Key。")
        return rec

    def delete_for_credential(self, task_id: str, credential: str) -> dict:
        """删除/取消任务。

        🔴 **即梦没有取消端点**（实测只有建任务 + 查询两个接口）⇒
        对**非终态**任务的删除必须**响亮失败**，绝不能本地置 canceled 就返回成功：
          ① 上游任务会**继续跑、继续扣积分**，而调用方以为停了；
          ② 本地与上游状态**永久不一致**，且没有任何出口能看出来。
        已终态的任务（删本地记录）不受此限。
        """
        rec = self.get_for_credential(task_id, credential)
        if not rec.terminal:
            raise InvalidParameterError(
                f"任务 {rec.task_id} 仍在 {rec.status}，无法删除："
                f"即梦上游**没有取消端点**（只有建任务与查询两个接口），"
                f"本地删除只会造成「你以为停了、实际上还在跑并继续计费」的假象。"
                f"请轮询到终态后再删除。",
                param="task_id")
        self.store.delete(rec.task_id)
        OBS.info("task deleted", task_id=rec.task_id, status=rec.status)
        # 小写贯穿全部出口（2026-09-22 统一）：此处曾用大写 "DELETED"，
        # 与任务状态机五态（queued/in_progress/success/failure/canceled）风格不一致。
        return {"task_id": rec.task_id, "status": "deleted"}

    def list_for_credential(self, credential: str, *, limit: int = 50) -> dict:
        recs = self.store.list_recent(credential_id=credential, limit=limit)
        return {
            "items": [{"task_id": r.task_id, "status": r.status,
                       "model": r.model, "created_at": r.created_at}
                      for r in recs],
            "total": len(recs),
        }

    # ------------------------------------------------------------------ 同步等待

    def wait_terminal(self, task_id: str, credential: str, *,
                      max_wait: float) -> TaskRecord:
        """在 `max_wait` 预算内等待任务到**终态**；超预算就返回当时的记录。

        🔴 这是同步接口（`POST /v1/images/generations`）的等待实现 ——
        它**只查本地库**，从不直接驱动上游：任务的推进始终由协调器线程做
        （见 `coordinator.py` 的模块 docstring，建任务是计费动作，必须走闸门），
        等待方只是把"调用方原本要做的多次轮询"折叠进一个 HTTP 请求里。

        返回**超时时的记录**而不是抛错：预算耗尽不是错误、是降级信号 ——
        调用方按 `view()` 的 202 语义处理（拿 `task_id` 转异步轮询）；
        任务也**不会**被取消（上游没有取消端点，它本来就在继续跑）。

        前提：有推进者在跑（`Coordinator.running`）—— HTTP 层在进来之前
        已经断言过；没有推进者时等满预算也是白等，那里会快速 503。
        """
        t0 = time.monotonic()
        deadline = t0 + max_wait
        while True:
            rec = self.get_for_credential(task_id, credential)
            remaining = deadline - time.monotonic()
            if rec.terminal or remaining <= 0:
                # 终态与"预算耗尽"合并成同一个返回点：两者对调用方都是
                # "拿去用 `view()` 看"——区别只在 200 还是 202。
                OBS.info("sync wait finished", task_id=task_id, status=rec.status,
                         waited_s=round(time.monotonic() - t0, 3),
                         timed_out=not rec.terminal)
                return rec
            time.sleep(min(SYNC_WAIT_POLL_INTERVAL, remaining))

    # ------------------------------------------------------------------ 推进

    def dispatch(self, rec: TaskRecord) -> None:
        """把 queued 任务推到上游（**计费动作**）。协调器线程调用。"""
        if self.client is None or self.uploader is None:
            self._fail(rec, CapabilityUnavailableError(
                "服务未配置上游客户端（JIMENG_SESSIONID 缺失）", upstream="jimeng"))
            return

        cap = models.REGISTRY[rec.cap_key]
        # 闸门：节奏 + 冷却。**在这之前不发任何请求**
        try:
            self.gate.acquire()
        except AdapterError as e:
            # 冷却中/节奏满：**不消耗 attempts**，下个 tick 再说
            OBS.warning("gate held dispatch", task_id=rec.task_id,
                        error=e.message, retry_after=e.retry_after)
            return

        ctx = {"task_id": rec.task_id, "model": rec.model, "capability": rec.cap_key}
        try:
            image_uris: list[str] = []
            if cap.image_required:
                image_uris = self._prepare_input_images(rec)

            sid = self._submit(rec, cap, image_uris)
        except AdapterError as e:
            self._on_dispatch_error(rec, e)
            return
        except JimengError as e:
            self._on_dispatch_error(rec, to_adapter_error(e))
            return
        except Exception as e:
            log.exception("dispatch 未预期异常 task=%s", rec.task_id)
            self._on_dispatch_error(rec, UpstreamUnavailableError(
                f"未预期的内部错误（{type(e).__name__}: {e}）", upstream="jimeng"))
            return

        now = int(time.time())
        self.store.patch(rec.task_id, status="in_progress",
                         upstream_submit_id=sid, started_at=now,
                         # 续生成（action=2）要求「原样再带一遍草稿」⇒ 必须存下来
                         # ⚠️ 用 getattr 兜住测试替身（假客户端没有这个属性）
                         draft_json=getattr(self.client, "last_draft", None) or None,
                         attempts=rec.attempts + 1)
        OBS.span("task.dispatched", **ctx)
        OBS.info("task submitted", upstream_submit_id=sid,
                 attempts=rec.attempts + 1, **ctx)

    def poll(self, rec: TaskRecord) -> None:
        """推进**单个** in_progress 任务（单条入口，内部走批量实现）。"""
        self.poll_many([rec])

    def poll_many(self, recs: list[TaskRecord]) -> dict[str, int]:
        """一轮推进一批在途任务 —— **上游查询合并成一次**。

        三道门，顺序不能反（都不能省）：

        ① **总超时看门狗**：不处理就永远卡在 in_progress。判超时**不发上游请求**
           （已经太久没结果，再打一次也白打，还多一次风控暴露）。
        ② **起轮宽限**（`POLL_GRACE`）：刚提交时上游可能还没落库。
        ③ 🔴 **轮询间隔**（`JIMENG_POLL_INTERVAL`）：距上次推进不足间隔就**不问**。

        ③ 是这次补上的 —— 在此之前 `JIMENG_POLL_INTERVAL` 只被传给了
        `JimengClient(poll_interval=…)`，而服务从不调 `client.wait()`/`generate()`，
        于是协调器**每个 tick（默认 1s）就打一次上游**，比配置值勤一倍。
        配置项被读了却没有效果，属于"假配置"：静态门禁只能查"有没有人读"，
        查不出"读了有没有用"（这一条只能靠人对着调用链看）。

        **批量**的意义：上游 `get_history_by_ids` 吃的是 `submit_ids`（复数），
        逐个查会让请求量随在途任务数**线性增长**；一次查完则与并发无关。
        默认并发 1 时收益为零，但它是"把并发提上去"的前提 —— 否则提并发就等于
        把上游请求量一起乘 N，而那正是风控最敏感的维度。

        ⚠️ 三道门读的是**传入记录上的字段**（尤其是 `updated_at`）。
        调用方必须传**刚从库里读出来的行**；若持有旧对象反复调用，
        间隔门看到的永远是旧时间 ⇒ 等于门不存在。
        （协调器每 tick 都 `list_by_status` 重读，所以生产路径是对的；
        但这确实是个陷阱，本仓的基准脚本第一版就踩了。）
        """
        if self.client is None or not recs:
            return {"polled": 0, "expired": 0, "skipped": 0}
        now = time.time()
        asked: list[TaskRecord] = []
        expired = 0
        for rec in recs:
            if now - (rec.started_at or rec.created_at) > self.settings.task_timeout:
                # ① 看门狗（本地判死，不发上游请求）
                self._fail(rec, UpstreamTimeoutError(
                    f"任务超过 {self.settings.task_timeout:.0f}s 仍未到终态，本地判超时。"
                    f"上游任务可能仍在跑（本服务不再跟进；如需继续，"
                    f"请保留 submit_id 人工查）。", upstream="jimeng"))
                expired += 1
                continue
            if now - (rec.started_at or 0) < self.settings.poll_grace:
                continue                                    # ② 起轮宽限
            if now - rec.updated_at < self.settings.jimeng_poll_interval:
                continue                                    # ③ 轮询间隔
            asked.append(rec)

        if not asked:
            return {"polled": 0, "expired": expired,
                    "skipped": len(recs) - expired}

        ids = [r.upstream_submit_id for r in asked if r.upstream_submit_id]
        try:
            states = self.client.fetch_many(ids)
        except JimengError as e:
            err = to_adapter_error(e)
            for rec in asked:
                if err.retryable:
                    # 可重试的探测失败**不改状态**（任务还在跑），只记账
                    self.store.patch(rec.task_id, attempts=rec.attempts + 1)
                else:
                    self._fail(rec, err)
            OBS.warning("poll failed", error=err.message, retryable=err.retryable,
                        tasks=len(asked))
            return {"polled": 0, "expired": expired, "skipped": 0}

        for rec in asked:
            st = states.get(rec.upstream_submit_id or "")
            if st is None:                                  # 理论上不会发生
                continue
            self._advance(rec, st)
        return {"polled": len(asked), "expired": expired, "skipped": 0}

    def poll_due(self, rec: TaskRecord, *, now: float | None = None) -> bool:
        """这个任务现在该不该问上游（供测试与运维观测用，**无副作用**）。

        守卫：不是 in_progress、或没有 `upstream_submit_id` ⇒ 一律 False。
        没有 submit_id 就压根无从问起（协调器虽然只拿 in_progress 记录来调它，
        但这个断言是公开的，不该依赖调用方先自己筛过）。
        """
        if rec.status != "in_progress" or not rec.upstream_submit_id:
            return False
        now = time.time() if now is None else now
        if now - (rec.started_at or rec.created_at) > self.settings.task_timeout:
            return False
        if now - (rec.started_at or 0) < self.settings.poll_grace:
            return False
        return now - rec.updated_at >= self.settings.jimeng_poll_interval

    def _continue_partial(self, rec: TaskRecord, st: Any) -> None:
        """上游报 `status=45`（部分成功）时**立刻续生成**，而不是干等。

        🔴 **为什么必须在这个状态调**：实测 `action=2` **只在"待补生成"态被接受**
        （`status=45`/部分成功）。拿已完成（`50`）的任务去续一律 `ret=1002` ——
        我为此做了 6 组对照实验（逐字草稿 / 未续过的 history / 沿用 submit_id /
        补 query 参数）才定位到：**前四个假设全被"拿已完成任务去续"这个错前提误导**。

        ⚠️ **这是计费动作** ⇒ 由调用方用 `CONTINUE_MAX` 封顶，且每次留痕。
        ⚠️ **异步**：只提交 + 换上新 `submit_id` 放回 `in_progress`，让协调器照常轮询；
        **绝不在这里同步等**（单并发下那会堵死协调器）。
        """
        have = [{"url": im.url, "width": im.width, "height": im.height,
                 "format": im.format, "note": im.note,
                 "item_id": im.item_id, "vid": im.vid} for im in st.images]
        if rec.images:
            seen = {im.get("url") for im in rec.images}
            have = list(rec.images) + [im for im in have if im.get("url") not in seen]
        want = rec.n or len(have)
        hist = getattr(st, "history_record_id", None) or rec.upstream_history_id
        if len(have) >= want or not hist or not rec.draft_json:
            # 没有缺口 / 没有续生成原料 ⇒ 保持原行为（继续等），不硬造请求
            self.store.patch(rec.task_id, updated_at=int(time.time()), images=have)
            return
        try:
            sid = self.client.continue_task(hist, rec.draft_json)
        except AdapterError as e:
            self.store.patch(
                rec.task_id, updated_at=int(time.time()), images=have,
                degradations=list(rec.degradations) + [
                    f"⚠️ 上游部分成功（45）后自动续生成失败（{e.err_type}）："
                    f"{e.message}"])
            return
        n_used = (rec.continuations or 0) + 1
        self.store.patch(
            rec.task_id, status="in_progress", upstream_submit_id=sid,
            images=have, continuations=n_used,
            degradations=list(rec.degradations) + [
                f"⚠️ 上游只完成 {len(have)} 张（请求 n={want}）⇒ 已自动续生成"
                f"（第 {n_used}/{CONTINUE_MAX} 次，`action=2`）去取剩余的真图。"])
        OBS.info("task continued", task_id=rec.task_id, model=rec.model,
                 trigger="partial_45", continuations=n_used,
                 have=len(have), want=want)

    def _advance(self, rec: TaskRecord, st: Any) -> None:
        """把一次查询结果落到任务上（终态收敛 / 未完成只刷时间）。"""
        if not st.finished:
            # 🔴 **`status=45`（部分成功）不是"再等等"，而是上游在问"要不要继续"。**
            # 实测 `action=2` **只在这个状态被接受**（拿已完成的任务去续一律 1002）；
            # 所以要**在这里就续**，而不是干等到 `TASK_TIMEOUT`（30 分钟）——
            # 那正是"4 张垫图卡 30 分钟"的成因。
            if (self.settings.continue_enabled      # 🔴 开关，默认关
                    and st.status == 45 and st.images
                    and (rec.continuations or 0) < CONTINUE_MAX):
                self._continue_partial(rec, st)
                return
            # 其余未完成态：只刷 updated_at（让"上次推进时间"反映真实进度，
            # 也让 stale 扫描不会把正常轮询的任务误判成卡死）。
            # ⚠️ 它同时是 ③ 轮询间隔的判据，所以这一步不能省。
            self.store.patch(rec.task_id, updated_at=int(time.time()))
            return

        if st.failed:
            self._fail(rec, self._terminal_error(st))
            return

        # 🔴 **"至少有 1 张输出"是成功的最低线**（用户口径）：
        # 终态 `status=50` 却**零产物**时，报 `success` + 空 `data` 就是
        # "静默按少的交付"的极端情形 —— 调用方会拿到一个看起来成功、
        # 实际什么都没有的响应。这种一律按失败处理。
        if not st.images:
            self._fail(rec, UpstreamUnavailableError(
                f"上游报成功但**零产物**（status={st.status} {st.status_name}）。"
                f"本服务不交付空成功，请重试或联系上游。",
                upstream="jimeng", upstream_status=st.status_name))
            return

        images = [{"url": im.url, "width": im.width, "height": im.height,
                   "format": im.format, "note": im.note,
                   "item_id": im.item_id, "vid": im.vid} for im in st.images]
        notes = [im.note for im in st.images if im.note]
        deg = list(rec.degradations) + [f"产物提示：{n}" for n in notes]

        # 🔴 **上游少出图时，用成功的图补齐**（用户口径 2026-09-20 的"优化方案 1"）。
        #
        # 上游自己维护 `total_image_count` / `finished_image_count`
        # （实测：4 张那条是 `total=4, finished=1, status=45`，排在队列里慢慢出）。
        #
        # 语义（刻意选这个而不是"少给几张算几张"）：
        #   · 调用方**拿到 `n` 个 url** —— 契约上的数量不因上游抖动而变；
        #   · 缺口由**已成功的图按序重复填充**；
        #   · **必须写明"其中 k 张是重复的"** —— 不假装那是新图。
        #     否则调用方会以为拿到了 n 个不同结果，那是另一种静默失真。
        # ⚠️ 零产物已在上面拦成失败，所以走到这里 `images` 必然 ≥1 张。
        # 🔴 **判据是「交付张数 < 请求的 n」，不是「finished < total」。**
        #
        # 实测 `total_image_count` **跟着垫图张数走、不是跟着"要出几张"**：
        #   · 4 垫图 + 未传 n（⇒ n=1） ⇒ total=**4**
        #   · 3 垫图 + n=2              ⇒ total=**3**
        # ⇒ 拿 `finished < total` 判"少给"会在"3 垫图 + n=2"上**误报**
        #   （2 < 3，可是我们要的正好就是 2 张，一张没少）。
        # 所以那两个计数**只当排查上下文**，判据用"交付 vs 请求"。
        # 续生成回来的批次要和已有产物**合并**（同一任务分几次出图）
        if (rec.continuations or 0) > 0 and rec.images:
            seen = {im.get("url") for im in rec.images}
            images = list(rec.images) + [im for im in images if im.get("url") not in seen]

        want = rec.n or len(images)
        if want > len(images):
            # ⚠️ **这里刻意不再试续生成**：`action=2` 只在「待补生成」态
            # （`status=45`）被接受，那一支已经由 `_continue_partial` 接管；
            # 走到"终态成功（50）但少给"这条路时再试，**必然 `ret=1002`**
            # （白花一次请求，还会在降级里塞一条无用的失败说明）。
            # 所以这里**只做退路**：用成功的图补齐。

            uniq = len(images)
            if uniq:
                images = [images[i % uniq] for i in range(want)]
            ctx = (f"（上游计数 finished={st.finished_count}/total={st.total}，"
                   f"**仅供排查**：该 total 跟的是垫图数、不是出图张数）"
                   if st.total is not None and st.finished_count is not None else "")
            why = (f"已续生成 {rec.continuations} 次仍未凑齐，"
                   if (rec.continuations or 0) >= CONTINUE_MAX
                   else "无续生成原料（缺 history_id 或 draft）")
            deg.append(
                f"⚠️ 上游只出了 {uniq} 张、请求 n={want}{ctx} —— {why}，"
                f"按口径**用成功的图补齐**到 {want} 个 url："
                f"**其中 {want - uniq} 张是重复的**（url 与前 {uniq} 个相同，"
                f"别当新图用）。")
        now = int(time.time())
        self.store.patch(
            rec.task_id, status="success", images=images,
            credits=st.cost, finished_at=now, degradations=deg,
            # 续生成的必需字段（回执里本来就有，此前只解析不持久化）
            upstream_history_id=getattr(st, "history_record_id", None))
        OBS.info("task succeeded", task_id=rec.task_id, model=rec.model,
                 image_count=len(images), credits=st.cost,
                 status_name=st.status_name,
                 elapsed_s=round(now - rec.created_at, 1))

        # 🔴 **扣了积分就必须在 Logfire 上看得见** —— 生成是这条链上唯一花钱的动作，
        # 不能等翻账单才发现。落点选**任务翻终态这一刻**，不是读接口 `view()`：
        # 后者有两个毛病 —— 没人轮询就不报警，而同一条被轮询多次会**重复报**。
        #
        # ⚠️ 规则刻意**不是**「forecast > 0 就报」：t2i / hd 在 Seedream 5.0 Lite 上
        # **实测免费**（没有任何消耗记录），但上游回执照样报 44 / 9 —— 若那也告警，
        # 告警会次次都响，真扣费的那次反而没人看了（狼来了）。按**实测价**判定：
        #   · `credits_measured > 0` ⇒ 实测会扣 ⇒ **报**
        #   · `credits_measured == 0` ⇒ 实测免费 ⇒ **不报**
        #   · `credits_measured is None` ⇒ 未实测、无法排除扣费 ⇒ **报**
        _cap = models.REGISTRY.get(rec.cap_key)
        _measured = _cap.credits_measured if _cap else None
        if _measured != 0:
            OBS.warning(
                "credits consumed", task_id=rec.task_id, model=rec.model,
                images=len(images), n=rec.n,
                forecast_credits=st.cost, measured_credits=_measured,
                why=("该能力实测会扣分" if (_measured or 0) > 0
                     else "该能力的实扣未实测，无法排除扣费"))

    # ------------------------------------------------------------------ 内部

    def _load_media_ref(self, ref: str) -> Blob:
        """加载任意参考素材（URL / data URI / base64）—— 不做图片嗅探校验
        （视频/音频不是图片；大小上限复用 MAX_INPUT_BYTES 的语义）。"""
        blob = load_one(ref, self.settings)
        if blob.size > self.settings.max_input_bytes:
            raise InvalidParameterError(
                f"参考素材超过上限 {self.settings.max_input_bytes} 字节",
                param="video")
        return blob

    def _transfer_one(self, blob: Any) -> tuple[str, list[str], int]:
        """归一化 + 上传**一张**，返回 (uri, 降级说明, 字节数)。

        可被多线程并发调用。安全性依据（两处都已在别处钉过）：
          · `JimengUploader.token()` 是**双检锁** —— 并发 N 张只会取一次 STS，
            不会退化成"每张各签一次"（`upload.py` 的模块注释里记着实测）；
          · 上传走 `httpx.Client`，它对并发请求是线程安全的。

        ## 🔴 为什么这里要单独退避重试

        多张时走 `Executor.map` —— **任何一个异常都会冒泡**，
        所以"某一张被限流"会让**整批垫图失败**。而提交/轮询那条路本来就有退避，
        只有上传这一段没有 ⇒ 这是唯一会把瞬时限流放大成整单失败的缺口。

        只重试**明确可重试**的（`retryable=True`，即限流一类）；
        **风控（`retryable=False`）立刻抛出** —— 持续施压只会延长标记，
        重试反而是帮倒忙。`normalize` 也放在循环**外**：同一份字节不该重复算。
        """
        norm = normalize(blob, self.settings)
        delay = 0.5
        for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
            try:
                uri = self.uploader.upload(norm.data)   # type: ignore[union-attr]
                if attempt > 1:
                    OBS.info("upload retried ok", attempt=attempt)
                return uri, list(norm.notes), norm.size
            except Exception as e:
                if not getattr(e, "retryable", False) or attempt == UPLOAD_MAX_ATTEMPTS:
                    raise
                wait = getattr(e, "retry_after", None) or delay
                OBS.warning("upload rate-limited, backing off", attempt=attempt,
                            wait_s=round(float(wait), 2), err=type(e).__name__)
                time.sleep(min(float(wait), 8.0))
                delay *= 2
        raise AssertionError("unreachable")

    def _prepare_input_images(self, rec: TaskRecord) -> list[str]:
        """下载 → 归一化 → 上传，**每张垫图各一次**，返回 `image_uri` 列表。**不计费。**

        🔴 **顺序必须与调用方给的 `image` 数组一致** —— 垫图的先后对生成语义有影响。
        多张时用线程池并发，但用 `Executor.map`（**保序**），
        绝不是"谁先传完谁排前面"。

        张数上限已在受理时校验（`Capability.max_images`），所以这里可以放心地
        "来几张传几张"；不会出现"下载了 N 张只用第 1 张"那种静默浪费。

        ## 能复用就不搬运

        输入若**本身就是上游存储里的资产**（典型：拿上一次的产物当这次输入），
        直接复用它的 `image_uri` —— **下载 + 归一化 + 上传整段跳过**。
        这段实测是 ~0.5s 下载 + ~1.3s 上传（3 张、2.9MB 级），而搬运的是同一份字节。
        守卫（host + hex key）与代价见 `media.reuse_image_uri`；
        可复用的与需要搬运的可以混在一批里，**顺序照旧按 `image_refs` 回填**。
        """
        assert self.uploader is not None
        started = time.monotonic()
        reused = [reuse_image_uri(r) for r in rec.image_refs]
        pending = [r for r, u in zip(rec.image_refs, reused) if u is None]

        parallel = 1
        cached: bool | None = None
        notes: list[str] = []
        if pending:
            blobs = load_images(pending, self.settings)
            if len(blobs) <= 1:
                # 单张：不值得为一次调用付线程池的钱；顺带 `last_cached` 此时是准确的
                results = [self._transfer_one(b) for b in blobs]
                cached = self.uploader.last_cached
            else:
                # 🔴 多张**并发**：用 `Executor.map`（**保序**），
                # 绝不是"谁先传完谁排前面"。
                parallel = min(len(blobs), MAX_UPLOAD_PARALLELISM)
                with ThreadPoolExecutor(max_workers=parallel) as pool:
                    results = list(pool.map(self._transfer_one, blobs))
                # ⚠️ 并发下 `last_cached` 是**共享字段**，取值不可靠 ⇒ 不报它。
                cached = None
            notes = [n for _, ns, _ in results for n in ns]
            fresh = iter(uri for uri, _, _ in results)
            uris: list[str] = [u if u is not None else next(fresh) for u in reused]
        else:
            # 全都可复用 ⇒ 一次下载、一次上传都不需要
            uris = list(reused)          # type: ignore[arg-type]

        if notes:
            # 一次写完：N 张的降级说明合起来只 patch 一次，别每张都写库
            self.store.patch(rec.task_id,
                             degradations=list(rec.degradations) + notes)
        OBS.info("input images ready", task_id=rec.task_id,
                 count=len(uris),
                 reused=sum(1 for u in reused if u is not None),
                 uploaded=len(pending), parallel=parallel,
                 elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                 cached=cached)
        return uris

    def _submit(self, rec: TaskRecord, cap: models.Capability,
                image_uris: list[str]) -> str:
        assert self.client is not None
        size = rec.size or _default_size()
        if cap.name == "t2i":
            model_key = rec.upstream_model or DEFAULT_MODEL
            opts = self.cfg.count_options(model_key) if self.cfg else None
            sid = self.client.submit(
                rec.prompt, model=model_key, size=size, count=rec.n or 1,
                negative_prompt=rec.negative_prompt, seed=rec.seed,
                count_options=opts)
        elif cap.name in models.T2V_VARIANTS:
            # 文生视频族（t2v / t2v-fast / t2v-pro）：三者**提交路径完全相同**，
            # 只差 `video_model`；模型与计费档位由 client.submit_video 按白名单定，
            # 张数恒 1（草稿无 gen_option，受理时已降级留痕）。
            # 🔴 别写成 `cap.name == "t2v"` —— 那样新变体会掉进后编辑族分支
            # 并在 `jimeng_tool` 上炸（见 models.T2V_VARIANTS 的注释）。
            sid = self.client.submit_video(
                rec.prompt,
                model=cap.video_model or DEFAULT_VIDEO_MODEL,
                resolution=rec.size or DEFAULT_VIDEO_RESOLUTION,
                duration_ms=rec.duration_ms or 4000,
                aspect_ratio=rec.aspect_ratio or DEFAULT_VIDEO_ASPECT_RATIO,
                seed=rec.seed)
        elif cap.name == "omni-video":
            # 全能参考：素材**现在**才下载/上传（受理时只落库引用）。
            # 图片走 ImageX（复用图生图链路）；视频/音频走 VOD → vid。
            # 计费：amount = 输出秒数 + Σ输入视频秒数（VOD Duration 可探测）。
            info = json.loads(rec.extra_json or "{}")
            materials: list[dict[str, Any]] = []
            input_video_s = 0.0
            for m in info.get("omni", []):
                blob = self._load_media_ref(m["ref"])
                if m["kind"] == "image":
                    uri, _, _ = self._transfer_one(blob)
                    materials.append({"kind": "image", "uri": uri})
                else:
                    if self.vod is None:
                        raise CapabilityUnavailableError(
                            "服务未配置 VOD 上传器", upstream="jimeng")
                    out = self.vod.upload(blob.data)
                    materials.append({
                        "kind": m["kind"], "uri": out["vid"],
                        "width": out.get("width"), "height": out.get("height"),
                        "duration_ms": out.get("duration_ms") or 0,
                    })
                    if m["kind"] == "video" and out.get("duration_ms"):
                        input_video_s += out["duration_ms"] / 1000.0
            sid = self.client.submit_video_omni(
                rec.prompt, materials=materials,
                resolution=rec.size or DEFAULT_VIDEO_RESOLUTION,
                duration_ms=rec.duration_ms or 5000,
                aspect_ratio=rec.aspect_ratio or DEFAULT_VIDEO_ASPECT_RATIO,
                seed=rec.seed,
                input_video_s=round(input_video_s, 2))
        elif cap.name == "vfi":
            # 补帧：local_video（本地/外部视频，实测形态）或产物三件套。
            info = json.loads(rec.extra_json or "{}")
            if info.get("local_video"):
                blob = self._load_media_ref(info["local_video"])
                if self.vod is None:
                    raise CapabilityUnavailableError(
                        "服务未配置 VOD 上传器", upstream="jimeng")
                out = self.vod.upload(blob.data)
                sid = self.client.submit_video_vfi(
                    None, prompt=rec.prompt, vid=out["vid"],
                    resolution=rec.size or DEFAULT_VIDEO_RESOLUTION,
                    duration_ms=rec.duration_ms or 4000,
                    target_fps=info.get("target_fps") or 60)
            else:
                src = self.store.get(info.get("source_task_id") or "")
                if src is None or not src.draft_json:
                    raise JimengParamError(
                        f"源任务 {info.get('source_task_id')!r} 不存在或缺 draft_json，"
                        f"无法构造补帧草稿。", code=1001)
                sid = self.client.submit_video_vfi(
                    src.draft_json, prompt=rec.prompt,
                    vid=info["vid"], origin_history_id=info["history_id"],
                    item_id=info["item_id"],
                    resolution=rec.size or DEFAULT_VIDEO_RESOLUTION,
                    duration_ms=rec.duration_ms or 4000,
                    target_fps=info.get("target_fps") or 60,
                    source_submit_id=info.get("source_submit_id"),
                    source_item_id=info.get("item_id"))
        elif cap.name == "detail-fix":
            # 细节修复：引用形态（item_id+origin_history_id，探针真跑验证过）。
            info = json.loads(rec.extra_json or "{}")
            opts = self.cfg.count_options(DEFAULT_MODEL) if self.cfg else None
            sid = self.client.edit(
                "detail", item_id=int(info["item_id"]),
                origin_history_id=int(info["history_id"]),
                size=rec.size or "2048x2048", count=rec.n or 1,
                count_options=opts)
        elif cap.name == "i2i":
            # blend 原生吃**列表** ⇒ 多张垫图一次带上；
            # 张数走与文生图**同一套吸附**（`generate_count_options`）——
            # 草稿里真的把 `gen_count` 写进 `abilities.gen_option` 了，所以 `n` 生效。
            # 🔴 2026-10-02（mj82 触发）：**模型必须跟着走**。
            # 原先这里写死 `DEFAULT_MODEL`（Lite），于是 `jm_image_model_yc_mj82`
            # 这类"支持 byte_edit 的模型"在图生图里根本用不上——`blend()` 明明
            # 有 `model` 参数，这里却没传。⇒ 换模型必须**连能力一起换**，
            # 否则"我换了模型"只换了文生图，图生图还是旧模型（静默不一致）。
            model_key = rec.upstream_model or DEFAULT_MODEL
            opts = self.cfg.count_options(model_key) if self.cfg else None
            sid = self.client.blend(rec.prompt, image_uris=image_uris, size=size,
                                    count=rec.n or 1, count_options=opts,
                                    model=model_key)
        elif cap.jimeng_tool:
            # 后编辑族（hd / pro-hd / outpaint）：上游用单个 `origin_image` 承载输入图，
            # 但**张数同样是 `abilities.gen_option.gen_count`**（组件级字段）⇒ 一并传。
            assert len(image_uris) == 1, "后编辑族只接受 1 张输入图（受理时已校验）"
            opts = self.cfg.count_options(DEFAULT_MODEL) if self.cfg else None
            sid = self.client.edit(cap.jimeng_tool, image_uri=image_uris[0],
                                   size=size, count=rec.n or 1,
                                   count_options=opts)
        else:
            # 🔴 接线遗漏：能力**注册了**、`_submit` 却没有对应分支。
            # 绝不 `assert`（见 CapabilityNotWiredError 的注释：AssertionError 会被
            # 当成"上游异常·可重试"，把排障方向带偏，还白涨 attempts）。
            raise CapabilityNotWiredError(
                f"能力 {cap.api_id}（{cap.name}）没有提交实现 —— 本服务的接线遗漏，"
                f"不是上游问题。请补 `Service._submit` 分支，并同步 "
                f"`models.T2V_VARIANTS` 与 `SUBMIT_ROUTES`。", upstream="jimeng")
        # 客户端侧还可能产生吸附告警（如 t2i 的张数），一并留痕
        extra = [w for w in (self.client.last_warnings or []) if w]
        if extra:
            self.store.patch(rec.task_id,
                             degradations=list(rec.degradations) + extra)
        return sid

    def _on_dispatch_error(self, rec: TaskRecord, err: AdapterError) -> None:
        """建任务失败的处理。**分两种**：可重试的回队列，其余判死。"""
        if isinstance(err, RiskControlError):
            self.gate.mark_risk_hit()
        elif isinstance(err, UpstreamQuotaError):
            # 额度耗尽：进静默期。**不做"锁到明天"** —— 上游重置时刻未必是本地零点，
            # 静默期到点自然重试，能自愈。
            self.gate.mark_quota_exhausted(
                min(self.settings.jm_cooldown * 2, 3600.0))

        attempts = rec.attempts + 1
        if err.retryable and attempts < DISPATCH_MAX_ATTEMPTS:
            self.store.patch(rec.task_id, status="queued", attempts=attempts,
                             started_at=None)
            OBS.warning("dispatch failed, will retry", task_id=rec.task_id,
                        attempts=attempts, error=err.message)
            return
        self._fail(rec, err, attempts=attempts)

    def _fail(self, rec: TaskRecord, err: AdapterError,
              *, attempts: int | None = None) -> None:
        now = int(time.time())
        self.store.patch(
            rec.task_id, status="failure", error=err.to_error()["error"],
            finished_at=now, attempts=attempts if attempts is not None
            else rec.attempts)
        OBS.error("task failed", task_id=rec.task_id, model=rec.model,
                  err_type=err.err_type, err_code=err.err_code,
                  message=err.message)

    @staticmethod
    def _terminal_error(st: Any) -> AdapterError:
        """上游任务到终态但**失败** —— 这是"被接受≠能跑通"的落点。

        🔴 **内容审核不能只看 `status`。** 实测（用户抓包）：
        `status=30`（通用的"生成失败"）+ `fail_code=2038`（`InputTextRisk`）才是真因，
        `fail_starling_message` = "你输入的文字不符合平台规则，请修改后重试"。

        只看 `status`（原先只判 `10/40`）会把它归成 `UpstreamUnavailableError`
        ⇒ 调用方以为"上游故障、可以重试"，而它**必然再被拒**（还可能每次都计费）。
        """
        reason = st.failed_reason or st.status_name
        code = st.status
        fc = getattr(st, "fail_code", None)
        if code in (10, 40) or fc in CODES_SECURITY:
            detail = f"，fail_code={fc}" if fc else ""
            return ContentPolicyError(
                f"内容审核未通过（status={code} {st.status_name}{detail}）：{reason}。"
                f"换个 prompt 或换张输入图重试 —— **原样重试没有意义**。",
                upstream="jimeng")
        return UpstreamUnavailableError(
            f"上游生成失败（status={code} {st.status_name}）：{reason}。"
            f"⚠️ 该任务**已被上游计费**（详见积分消耗）。",
            upstream="jimeng", upstream_status=st.status_name)


def _build_gate(settings: Settings) -> UpstreamGate:
    """构造上游闸门 —— **唯一的构造点**，要调闸门参数改这里。"""
    return build_gate(settings)


def _default_size() -> str:
    """默认出图尺寸 —— **唯一的默认值定义点**（抓包实测唯一跑通的档位）。

    ⚠️ 这两个 helper 曾经在函数体里做局部 import（`from .gate import build_gate`），
    而 `DEFAULT_SIZE` 当时**根本没从包 `__init__` 导出** ⇒ 一调用就 `ImportError`。
    局部 import 会把这类"名字不存在"的问题推迟到运行期才炸，且不容易被静态检查
    看见。现在一律走模块级导入。
    """
    return DEFAULT_SIZE


__all__ = [
    "Service", "view", "to_adapter_error", "credential_id", "new_task_id",
    "fingerprint_secret", "ACCEPTED_FIELDS", "KNOWN_UNSUPPORTED_FIELDS",
    "DISPATCH_MAX_ATTEMPTS",
]
