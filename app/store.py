#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务持久化 —— **SQLModel**（Pydantic + SQLAlchemy）落 SQLite。

## 为什么用 SQL 而不是 JSON 文件

| | JSON 文件 | **SQLite（本仓选择）** |
|---|---|---|
| 按状态查询 | 全量扫描目录 | 索引命中 |
| 并发写 | 要自己上文件锁 | 事务 + WAL |
| 崩溃一致性 | 要自己保证原子写 | 事务严格持久 |
| 依赖 | 零 | 零（stdlib 内置 sqlite3） |

关键差别是**查询**：协调器每个 tick 都要问"有几个 queued / 几个 in_progress"，
JSON 方案下这是每次全量扫描目录 —— 任务攒到几千条时开销就不可忽视了。
SQLite 同样是零依赖（Python 自带），却把索引、事务、原子性都白送了。

## 为什么是 SQLite 而不是 Redis

| 场景 | 选型 | 理由 |
|---|---|---|
| 单机 / 单进程、任务每小时几条 | **SQLite** ← 本项目 | 单次 I/O ~1ms vs Redis ~0.2ms，而瓶颈是**上游出图几分钟** —— 这点差异端到端测不出来；SQLite 零依赖且事务**严格持久** |
| 多实例共享状态 / 高并发写 / 需要 TTL | Redis | 这才是它真正赢的地方 |

⚠️ 三个容易忽略的点：
  ① Redis 默认 RDB **会丢最后几秒**，要严格得上 AOF `always`（性能优势也就没了）——
     而"任务不丢"恰恰是这里的硬需求；
  ② **写错后端配置必须直接报错**，不能静默退回内存，否则持久化会在没人注意时悄悄失效；
  ③ 存储层接口化（put / patch / get / list_recent / count / prune），换后端时调用方零改动。

## 上一版踩过的坑（记在这里防止回退）

曾经有用**进程内 dict** 的做法：为了加载一处代码修正重启服务，**正在轮询的任务立刻不见**。
这类缺陷**只在重启时现形**，平时完全看不出来。
⇒ 必须有"**跨实例可读**"的用例来钉住它：把 SQLite 悄悄换成内存实现时，
别的用例全绿，**只有那条会红**（见 `tests/test_store.py::test_cross_instance_visibility`）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Literal, Optional

from loguru import logger
from sqlalchemy import JSON, Column, Engine, Index, func, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Field, Session, SQLModel, create_engine, select

#: JSON 列：PostgreSQL 上 **JSONB**（可索引、可按键查询）。
#: 这段方言差异**收敛在这一个常量里** —— 表定义本身不出现方言字样。
JSON_COL = JSON().with_variant(JSONB(), "postgresql")

_DSN_PASSWORD_RE = re.compile(r"(?P<head>://[^:/@]+:)(?P<pw>[^@]*)@")


def _scalar_default(col: Any) -> str | None:
    """取列的**标量**默认值并渲染成 SQL 字面量；没有（或不是标量）返回 None。

    只认 Python 侧声明过的标量默认（`Field(default=0)` / `default=""` …）。
    `default_factory`（`list` / `dict`）**刻意不认** —— 给 JSON 列编一个
    `DEFAULT '[]'` 看似方便，实际会掩盖"这列本该有值却没有"的数据问题。
    """
    d = getattr(col, "default", None)
    if d is None or not getattr(d, "is_scalar", False):
        return None
    arg = d.arg
    if isinstance(arg, bool):
        return "TRUE" if arg else "FALSE"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    return None


def mask_dsn(dsn: str) -> str:
    """把 DSN 里的密码换成 `***`。

    ⚠️ 这是本服务**唯一**一处掩码，且它作用于 `/stats` 这个 **HTTP 响应体**
    （不是 logfire 观测面 —— 那边按口径全量不脱敏）。
    理由：PG 密码进响应体没有任何排障收益，而 `/stats` 是随时可能被浏览器/网关
    抓到的地方。要全裸也说一声，一行就能去掉。
    """
    return _DSN_PASSWORD_RE.sub(r"\g<head>***@", dsn or "")

#: 任务状态。**全小写**（`queued` / `in_progress` / `success` / `failure` / `canceled`）。
#: 内部、存储与对外契约是**同一套写法** —— 刻意不做"库里大写、接口小写"那种映射：
#: 两套写法迟早会有人比较错（读到一个值去和另一种写法比，于是静默不相等）。
#: 非终态只有 `queued` / `in_progress` —— 不再造同义词：
#: "还在排队"是唯一一种等待，多造一个就有第二个真相。
Status = Literal["queued", "in_progress", "success", "failure", "canceled"]

ACTIVE_STATES: tuple[str, ...] = ("queued", "in_progress")
TERMINAL_STATES: tuple[str, ...] = ("success", "failure", "canceled")


# ---------------------------------------------------------------------------
# 表
# ---------------------------------------------------------------------------


class TaskRecord(SQLModel, table=True):
    """一条任务。

    请求参数与结果**并存**：任务可能几分钟后才被查询，甚至跨重启 ——
    只存结果的话，崩溃后连"该重放什么请求"都不知道。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_cred", "credential_id", "created_at"),
    )

    task_id: str = Field(primary_key=True)
    #: 调用方 Key 的 HMAC 指纹。**明文 Key 永不落库。**
    credential_id: str = Field(index=True)
    model: str                        # 对外 model（如 jimeng-t2i）
    cap_key: str                      # 内部能力 key（如 jimeng:t2i）
    upstream_model: Optional[str] = None
    status: str = Field(default="queued", index=True)

    # ---- 请求参数（原样留着：崩溃恢复与审计都要靠它）----
    prompt: str = ""
    image_refs: list = Field(default_factory=list,
                            sa_column=Column(JSON_COL, nullable=False))
    size: Optional[str] = None
    #: 🔴 2026-10-02（方案 B）：`model` 后缀指定的分辨率档（如 "4k"）。
    #: 为 None 表示"没指定、按 size 就近吸附"。
    #: 必须**落库** —— 受理与`_submit` 是两个阶段（可能跨进程/重启），
    #: 不落库就会在派发时丢掉后缀，提交包又回到"按 size 吸附"的老路。
    resolution_tier: Optional[str] = None
    n: Optional[int] = None
    seed: Optional[int] = None
    negative_prompt: str = ""
    #: 视频任务专用：时长（毫秒）。图片任务恒 None。
    #: 🔴 历史库靠启动期幂等迁移补列（见 `_ensure_video_columns`），
    #: 不再依赖运维手工 ALTER —— 那条路忘跑的症状是 `store.patch()` 报
    #: column does not exist，且只在视频任务上炸。
    duration_ms: Optional[int] = None
    #: 视频任务专用：画面比例（如 "16:9"）。图片任务恒 None。
    aspect_ratio: Optional[str] = None
    #: 任务附加信息（JSON 串）：补帧的源引用（source_task_id / vid / item_id /
    #: history_id / target_fps）、Ark 门面收到的原始 model 名等。
    #: 🔴 老库靠启动期幂等迁移补列（`_ensure_video_columns`）。
    extra_json: Optional[str] = None

    # ---- 上游与结果 ----
    #: 即梦的 `submit_id`。**绝不对外暴露**（对外只有本地 task_id）。
    upstream_submit_id: Optional[str] = None
    #: 🔴 续生成（action=2）的**必需字段** —— 上游回执里本来就有（history_record_id），
    #: 我们原先**只解析不持久化** ⇒ 异步/重启之后就没法续。
    upstream_history_id: Optional[str] = None
    #: 已自动续生成（action=2）的次数 —— 用来**封顶**，避免无限续（那会变成无底洞）。
    continuations: int = 0
    #: 首次提交的那份 draft_content（JSON 串）。用户抓包确认：续生成要**重新带一遍草稿**；
    #: 不存就只能重建，而重建容易与首次提交不一致（那会续错对象）。
    draft_json: Optional[str] = None
    images: list = Field(default_factory=list,
                         sa_column=Column(JSON_COL, nullable=False))
    #: 上游 `forecast_generate_cost` 给出的积分（真值，不编造）
    credits: Optional[int] = None
    error: Optional[dict] = Field(default=None,
                                  sa_column=Column(JSON_COL, nullable=True))
    #: 降级留痕（"请求了 A、实际做了 B"必须让调用方看见）
    degradations: list = Field(default_factory=list,
                               sa_column=Column(JSON_COL, nullable=False))

    created_at: int = 0
    updated_at: int = 0
    started_at: Optional[int] = None
    finished_at: Optional[int] = None
    #: 建任务尝试次数（只对可重试错误累加）
    attempts: int = 0

    # ---- 选主（多 worker 下防止重复提交，而重复提交 = 重复计费）----
    lease_owner: Optional[str] = None
    lease_until: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATES


class Lease(SQLModel, table=True):
    """协调器租约。`name` 是租约名（当前只有 `coordinator`）。"""

    __tablename__ = "lease"
    name: str = Field(primary_key=True)
    owner: str
    until: float = 0.0


class Meta(SQLModel, table=True):
    """小 kv。

    存「凭证指纹用的密钥」这类需要**跨重启稳定**的东西。
    为什么不放 `.env`：它必须首次启动自动生成、此后永不变 ——
    否则重启会让所有历史任务变得"不属于任何人"（调用方看到自己的任务突然 404）。
    """

    __tablename__ = "meta"
    k: str = Field(primary_key=True)
    v: str


_PATCHABLE = {
    "credential_id", "model", "cap_key", "upstream_model", "status", "prompt",
    "image_refs", "size", "n", "seed", "negative_prompt", "duration_ms",
    "aspect_ratio", "extra_json", "upstream_submit_id",
    "upstream_history_id", "draft_json", "continuations",
    "images", "credits", "error", "degradations", "created_at", "updated_at",
    "started_at", "finished_at", "attempts", "lease_owner", "lease_until",
}


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def build_engine(target: str | Engine, *, pool_size: int | None = None,
                 max_overflow: int | None = None, pool_recycle: int | None = None,
                 pre_ping: bool = True,
                 connect_timeout: int | None = None) -> Engine:
    """建**PostgreSQL** 引擎。**写错配置直接报错，绝不静默退回别的东西。**

    本服务只支持 PostgreSQL 一种任务库。刻意不留 SQLite/内存兜底：
    多一个后端就多一套"行为略有差异"的路径，而任务库是**事实源** ——
    事实源出现两种行为，是排查成本最高的那类缺陷。

    `postgresql://` 会被补成 `postgresql+psycopg2://`（本仓装的是 psycopg2）。
    """
    if isinstance(target, Engine):
        return target
    raw = str(target).strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if not raw.startswith("postgresql"):
        # 静默退回别的后端会让"任务不丢"在没人注意的时候失效 —— 事故级
        raise ValueError(
            f"TASK_DB 必须是 PostgreSQL DSN，实得 {raw!r}。"
            f"例：postgresql+psycopg2://user:pass@127.0.0.1:5432/jimeng")
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg2://" + raw[len("postgresql://"):]

    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": pre_ping}
    if pool_size:
        kwargs["pool_size"] = pool_size
    if max_overflow is not None:
        kwargs["max_overflow"] = max_overflow
    if pool_recycle:
        kwargs["pool_recycle"] = pool_recycle
    if connect_timeout:
        kwargs["connect_args"] = {"connect_timeout": connect_timeout}
    return create_engine(raw, **kwargs)


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


class TaskStore:
    """任务存储。**线程安全**（每次操作用独立 Session）。"""

    def __init__(self, target: str | Engine, *, pool_size: int | None = None,
                 max_overflow: int | None = None, pool_recycle: int | None = None,
                 pre_ping: bool = True,
                 connect_timeout: int | None = None) -> None:
        self.engine = build_engine(
            target, pool_size=pool_size, max_overflow=max_overflow,
            pool_recycle=pool_recycle, pre_ping=pre_ping,
            connect_timeout=connect_timeout)
        SQLModel.metadata.create_all(self.engine)
        self._ensure_video_columns()
        renamed = self.normalize_status_case()
        if renamed:
            # 响亮：这是"数据被就地改写"，运维必须能在日志里看到
            logger.warning("把 {} 条历史任务的大写状态归一化成小写", renamed)
        self.dsn = mask_dsn(target if isinstance(target, str) else str(target.url))

    # -------------------------------------------------------------- 迁移/维护

    def _ensure_video_columns(self) -> None:
        """启动期**幂等补齐任务表缺的列** —— 从模型元数据机械派生，不再手工枚举。

        🔴 为什么放这里而不是留给运维手工 ALTER：`SQLModel.metadata.create_all()`
        **只建表、不加列**。已有库上给模型加字段，症状是 `store.patch()` 报
        `column does not exist`，而且**只在新字段被写到时才炸**（图片链路全绿），
        极难第一时间定位。

        🔴 为什么改成**机械派生**：原先这张清单是手写的，只列了视频三列，
        于是后加的 `upstream_history_id` / `draft_json` / `continuations`
        漏在外面 —— 本地开发库 `jimeng` 一跑受理就炸（2026-09-23 实测）。
        "记得手工 ALTER"这条纪律**再一次**被证明会忘，所以现在让代码自己保证：
        遍历 `TaskRecord.__table__.columns`，模型加字段 ⇒ 下次启动自动补。

        · `ADD COLUMN IF NOT EXISTS` 幂等，已存在时 PG 直接跳过（零代价）；
        · 非空列带**模型声明的标量默认值**一起给出（`NOT NULL DEFAULT 0`
          对已有行安全；不带 DEFAULT 的 `NOT NULL` 在有数据的表上会直接失败）；
        · 非空但默认值是 `default_factory` 的列（`image_refs` / `images` /
          `degradations` 这类 JSON 列）**不敢瞎给默认值** ⇒ 只在真缺列时
          **响亮报警**，让人来做 —— 静默补一个空值会掩盖数据问题。
        与 `normalize_status_case` 同一取向：**启动时无条件跑一遍**。
        """
        table = TaskRecord.__tablename__
        dialect = postgresql.dialect()
        #: ⚠️ **必须先查实际存在的列**：只按"模型里非空却没有标量默认值"来判定，
        #: 会把 `task_id` / `image_refs` / `images` 这些**早就存在**的列也报成"缺列"
        #: （2026-09-23 实测：启动日志刷了一条完全虚假的 ERROR）。
        existing = {col["name"] for col in inspect(self.engine).get_columns(table)}
        missing_notnull: list[str] = []
        with self.engine.begin() as c:
            for col in TaskRecord.__table__.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col.name} " \
                      f"{col.type.compile(dialect)}"
                default = _scalar_default(col)
                if col.nullable is False:
                    if default is None:
                        missing_notnull.append(col.name)
                        continue
                    ddl += f" NOT NULL DEFAULT {default}"
                c.execute(text(ddl))
        for name in missing_notnull:
            logger.error(
                "任务表缺列且无法自动补：{}（非空、且没有标量默认值）。"
                "请手工迁移后重启 —— 该链路会一直报 column does not exist。", name)

    def normalize_status_case(self) -> int:
        """把历史行的大写状态归一化成小写。返回改动行数。

        🔴 **为什么这一步是必需的**：状态字面量从大写改成小写时，代码里的比较
        （`rec.status == "queued"`）与 `list_by_status("queued")` 都跟着变了，
        而**库里的历史行还是大写**。不归一化的话那些行匹配不上任何查询：

          · `list_by_status("queued")` 查不到它们 ⇒ **任务静默冻结**（不报错、不前进）；
          · `count_by_status("in_progress")` 数不到它们 ⇒ 并发上限看起来是空的。

        这正是"看着一切正常、实际全停住"的那一类故障 —— 所以它必须**在启动时
        无条件跑一遍**，而不是留给运维记得手动执行。

        幂等且便宜：`WHERE status <> lower(status)` 只在第一次真的改行，
        之后每次都是"匹配 0 行"的空转。没有 `lower()` 之外的语义，重复跑安全。
        """
        with Session(self.engine) as s:
            res = s.execute(text(
                "UPDATE tasks SET status = lower(status) "
                "WHERE status <> lower(status)"))
            s.commit()
            return int(res.rowcount or 0)

    # ------------------------------------------------------------------ 健康

    def ping(self) -> bool:
        """探活（就绪探针用）。**不抛** —— 探针失败要返回 false，不是把探针打挂。"""
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # ------------------------------------------------------------------ 写

    def put(self, rec: TaskRecord) -> TaskRecord:
        """整条写入 / 覆盖（按主键 upsert）。返回**会话内持久化后**的那份记录。

        🔴 必须用 `merge()` 的**返回值**：它返回的是会话内托管的新实例，
        传入的 `rec` 仍是游离态 —— 对游离实例调 `refresh()` 会
        `InvalidRequestError: Instance ... is not persistent within this Session`。
        这个 bug 在"没有真库、从没执行过写路径"时完全看不出来。
        """
        rec.updated_at = rec.updated_at or int(time.time())
        with Session(self.engine) as s:
            merged = s.merge(rec)
            s.commit()
            s.refresh(merged)
            s.expunge(merged)   # 交出会话前先脱离，避免后续惰性加载拿到已关闭的会话
            return merged

    def patch(self, task_id: str, **fields: Any) -> TaskRecord | None:
        """局部更新。`updated_at` 自动刷新。返回更新后的记录（不存在则 None）。"""
        if not fields:
            return self.get(task_id)
        bad = set(fields) - _PATCHABLE
        if bad:
            raise ValueError(f"未知字段 {sorted(bad)}")
        with Session(self.engine) as s:
            rec = s.get(TaskRecord, task_id)
            if rec is None:
                return None
            for k, v in fields.items():
                setattr(rec, k, v)
            if "updated_at" not in fields:
                rec.updated_at = int(time.time())
            s.add(rec)
            s.commit()
            s.refresh(rec)
            s.expunge(rec)
            return rec

    def delete(self, task_id: str) -> bool:
        with Session(self.engine) as s:
            rec = s.get(TaskRecord, task_id)
            if rec is None:
                return False
            s.delete(rec)
            s.commit()
            return True

    # ------------------------------------------------------------------ 读

    def get(self, task_id: str) -> TaskRecord | None:
        with Session(self.engine) as s:
            rec = s.get(TaskRecord, task_id)
            if rec is not None:
                s.expunge(rec)
            return rec

    def get_scoped(self, task_id: str, credential_id_: str) -> TaskRecord | None:
        """**按凭证取任务** —— 跨凭证必须取不到。

        这是"别人的任务读不到"的唯一实现点：调用方拿到 None 就回 404，
        **根本不去问上游**（放行到上游会用错的钥匙去查，返回的 404/空
        无法区分"任务真没了"与"钥匙不对"）。
        """
        rec = self.get(task_id)
        if rec is None or rec.credential_id != credential_id_:
            return None
        return rec

    def list_by_status(self, status: str, *, limit: int = 100,
                       order: str = "oldest") -> list[TaskRecord]:
        """按状态取任务。`oldest` 让最早排队的最先被处理（避免饿死）。"""
        stmt = select(TaskRecord).where(TaskRecord.status == status)
        stmt = stmt.order_by(
            TaskRecord.created_at.asc() if order == "oldest"
            else TaskRecord.created_at.desc())
        with Session(self.engine) as s:
            rows = list(s.exec(stmt.limit(limit)).all())
            for r in rows:
                s.expunge(r)
            return rows

    def count_by_status(self, status: str) -> int:
        """🔴 用 `COUNT(*)`，**不是** `SELECT task_id` 再 `len()`。

        后者会把所有匹配行取回 Python 只为数个数 —— 而这两个计数**每个协调器
        tick 都要各查一次**（默认 1s 一次），任务攒到几千条时纯属白搬数据。
        这种"能跑但白搬"的写法不会报错，只会在负载上来后变成看不见的开销。
        """
        stmt = select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status == status)
        with Session(self.engine) as s:
            return int(s.exec(stmt).one())

    def count_active(self) -> int:
        """在途任务数（queued + in_progress）。"""
        stmt = select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status.in_(ACTIVE_STATES))  # type: ignore[attr-defined]
        with Session(self.engine) as s:
            return int(s.exec(stmt).one())

    def list_recent(self, *, credential_id: str | None = None,
                    limit: int = 50) -> list[TaskRecord]:
        stmt = select(TaskRecord)
        if credential_id is not None:
            stmt = stmt.where(TaskRecord.credential_id == credential_id)
        stmt = stmt.order_by(TaskRecord.created_at.desc()).limit(limit)
        with Session(self.engine) as s:
            rows = list(s.exec(stmt).all())
            for r in rows:
                s.expunge(r)
            return rows

    def count(self) -> int:
        with Session(self.engine) as s:
            return int(s.exec(
                select(func.count()).select_from(TaskRecord)).one())

    def stale_active(self, *, older_than_s: float, limit: int = 100) -> list[TaskRecord]:
        """非终态且久未更新的任务（协调器兜底扫）。"""
        cutoff = int(time.time() - older_than_s)
        stmt = (select(TaskRecord)
                .where(TaskRecord.status.in_(ACTIVE_STATES))  # type: ignore[attr-defined]
                .where(TaskRecord.updated_at < cutoff)
                .order_by(TaskRecord.updated_at.asc())
                .limit(limit))
        with Session(self.engine) as s:
            rows = list(s.exec(stmt).all())
            for r in rows:
                s.expunge(r)
            return rows

    # ------------------------------------------------------------------ 维护

    def prune(self, *, retention_days: int, now: int | None = None) -> int:
        """清理超过保留期的**终态**任务。非终态任务一律不删。

        ⚠️ 只删终态：删掉还在跑的任务等于让调用方"任务凭空消失"，
        那正是本服务要避免的那类缺陷。
        """
        cutoff = (now or int(time.time())) - retention_days * 86400
        stmt = (select(TaskRecord)
                .where(TaskRecord.created_at < cutoff)
                .where(TaskRecord.status.in_(TERMINAL_STATES)))  # type: ignore[attr-defined]
        with Session(self.engine) as s:
            rows = list(s.exec(stmt).all())
            for r in rows:
                s.delete(r)
            s.commit()
            return len(rows)

    # ------------------------------------------------------------------ 选主

    def acquire_lease(self, name: str, owner: str, seconds: float) -> bool:
        """抢协调器租约。**多 worker 下保证只有一个在推进任务**。

        没有它时两个进程会同时提交同一个任务 —— 而建任务**是计费动作**，
        等于重复扣积分。

        实现用**纯 ORM**（不写裸 SQL）：先读再写，靠主键冲突兜住并发
        （两个 worker 同时读到"没有租约"时，后到的 INSERT 会撞主键，
        被 `IntegrityError` 接住并返回 False）。这样既不用手写 `ON CONFLICT`，
        也不依赖任何数据库方言 —— 换 PostgreSQL 时这段代码一行不改。
        """
        now = time.time()
        try:
            with Session(self.engine) as s:
                obj = s.get(Lease, name)
                if obj is None:
                    s.add(Lease(name=name, owner=owner, until=now + seconds))
                elif obj.until < now or obj.owner == owner:
                    obj.owner = owner
                    obj.until = now + seconds
                    s.add(obj)
                else:
                    return False
                s.commit()
                return True
        except IntegrityError:
            # 抢输了（另一个 worker 刚插进去）—— 这是**正常结果**，不是错误
            return False

    def release_lease(self, name: str, owner: str) -> None:
        with Session(self.engine) as s:
            obj = s.get(Lease, name)
            if obj is not None and obj.owner == owner:
                s.delete(obj)
                s.commit()

    def lease_owner(self, name: str) -> str | None:
        with Session(self.engine) as s:
            obj = s.get(Lease, name)
            if obj is None or obj.until < time.time():
                return None
            return obj.owner

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        with Session(self.engine) as s:
            obj = s.get(Meta, key)
            return obj.v if obj is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with Session(self.engine) as s:
            s.merge(Meta(k=key, v=value))
            s.commit()

    # ------------------------------------------------------------------ 统计

    def stats(self) -> dict:
        return {"backend": "postgresql", "dialect": self.engine.dialect.name,
                "dsn": self.dsn,          # 已掩码，见 mask_dsn
                "tasks": self.count(),
                "healthy": self.ping()}


__all__ = [
    "TaskStore", "TaskRecord", "Lease", "Meta", "Status",
    "ACTIVE_STATES", "TERMINAL_STATES", "build_engine", "mask_dsn", "JSON_COL",
]
