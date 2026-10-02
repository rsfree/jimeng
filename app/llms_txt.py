"""`/llms.txt` —— 给 LLM / Agent 读的服务说明书（llmstxt.org 约定）。

内容**从注册表派生**（`models.catalog()` + 设置项），只有小节骨架是静态文案 ——
目的是**不让说明书漂移成假信息**。本文件是 fleet 约定的第二次落地（第一次见 `baidu/app/llms_txt.py`）。

⚠️ 本服务与图片类服务的**两处关键差异**必须写清：
  1. **计费**：建任务即计费（异步形态更是"受理即扣"）—— 单价来自 catalog 的 `credits_measured`；
  2. **两套形态**：同步（等出结果）与**异步任务**（202 + 轮询），另有字节风格的
     `/api/v3/contents/generations/tasks` 兼容面。
"""

from __future__ import annotations

import inspect

from app import __version__
from app import errors as E
from app.config import Settings
from app.models import catalog

def _error_rows() -> list[tuple[str, int, str, str]]:
    """**从异常类派生**错误表（`(code, http, type, 可重试)`）。

    比手写码表强：上游/本服务改了错误模型，说明书**自动跟**（还有测试兜门禁）。
    """
    rows: list[tuple[str, int, str, str]] = []
    for name, cls in vars(E).items():
        if inspect.isclass(cls) and issubclass(cls, E.AdapterError) and cls is not E.AdapterError:
            rows.append((cls.err_code or name, int(cls.status_code),
                         cls.err_type, "可安全重试" if cls.retryable else "不要盲目重试"))
    return sorted(rows, key=lambda r: (r[1], r[0]))


#: 给几条常见错误补「人话」（键 = `err_code`）；没写到的也能自动出现在表里。
_ERROR_NOTES: dict[str, str] = {
    "invalid_parameter": "参数不合法（缺字段 / 类型错 / 该能力不接受的字段）",
    "unknown_model": "模型名不在能力表里",
    "auth_required": "受保护端点缺 `Authorization: Bearer <key>`",
    "task_not_found": "任务号不存在（异步查询）",
    "capability_unavailable": "该能力当前不可用（凭据/上游态）",
    "capability_not_wired": "该能力已宣告但未接线（不应出现，出现即 bug）",
    "upstream_unavailable": "上游未出片 / 结构不符（`detail` 给上游原话）",
    "quota_exhausted": "积分不足（建任务即计费的后果）",
    "risk_control": "上游风控（本服务进冷却窗，带 `retry_after`）",
    "content_policy": "上游内容策略拒绝（改提示词，别重试）",
}


def _billing_of(item: dict) -> str:
    """单价文案：`credits_measured` 是**实测值**，0 表示实测免费（不是没测过）。"""
    c = item.get("credits_measured")
    if c is None:
        return "未知"
    return "**0（实测免费）**" if c == 0 else f"{c} 积分/次（实测）"


def render(settings: Settings) -> str:
    """生成正文（纯函数：同一份 settings 永远得到同一份文本）。"""
    items = catalog()
    auth_on = bool(getattr(settings, "auth_enabled", False))

    out: list[str] = []
    add = out.append
    add("# jimeng-service · 即梦生成出口")
    add("")
    add("> 把即梦 web 端（`jimeng.jianying.com`）的生成能力包成 **OpenAI 风格**接口：")
    add("> **图片**（文生图 / 图生图 / 高清 / 扩图 / 超清）+ **视频**，同时提供**同步**与**异步任务**两套形态。")
    add(f"> 本服务对外宣告 **{len(items)} 项能力**（完整清单见下表，由注册表派生）。")
    add("")
    add("## 🔴 先读：计费")
    add("")
    add("- 本上游**按积分计费**，且**建任务即计费**（异步形态受理即扣，失败是否退还以实测为准）。")
    add("- 单价用「能力表」里的**实测值**列；`0（实测免费）` 是**跑过且没扣**，不是没测。")
    add("- 想零成本预演：图片同步端点支持 `\"dry_run\": true`（只回将发出的上游请求计划）。")
    add("")
    add("## 怎么调（两套形态）")
    add("")
    add("**① 同步（等结果，适合脚本）**")
    add("")
    add("```bash")
    add("curl -sS http://<host>/v1/images/generations \\")
    add("  -H \"Authorization: Bearer $JIMENG_API_KEY\" -H 'Content-Type: application/json' \\")
    add("  -d '{\"model\":\"jimeng:t2i\",\"prompt\":\"一只橘猫在窗台上\"}'")
    add("```")
    add("")
    add("**② 视频（唯一入口 = 火山方舟契约，2026-09-24 起）**")
    add("")
    add("```bash")
    add("TASK=$(curl -sS -X POST http://<host>/api/v3/contents/generations/tasks \\")
    add("  -H \"Authorization: Bearer $JIMENG_API_KEY\" -H 'Content-Type: application/json' \\")
    add("  -d '{\"model\":\"doubao-seedance-2-0-mini-260615\",\"content\":[{\"type\":\"text\",\"text\":\"海浪拍岸\"}]}' | jq -r .id)")
    add("curl -sS http://<host>/api/v3/contents/generations/tasks/$TASK -H \"Authorization: Bearer $JIMENG_API_KEY\"")
    add("```")
    add("")
    add("| 形态 | 端点 |")
    add("|---|---|")
    add("| 图片（同步） | `POST /v1/images/generations` |")
    add("| 图片（异步） | `POST /async/v1/images/generations` → `GET /async/v1/images/generations/{task_id}` |")
    add("| 视频（方舟契约） | `POST /api/v3/contents/generations/tasks` → `GET /api/v3/contents/generations/tasks/{task_id}` |")
    add("")
    add("## 鉴权")
    add("")
    add(f"- 当前鉴权：**{'开启（fail-closed）' if auth_on else '关闭（未配 API_KEYS ⇒ 仅适合本机/内网）'}**。")
    add("- 受保护端点需 `Authorization: Bearer <key>`；`/healthz`、`/readyz`、`/v1/models`、`/llms.txt`、`/` 免鉴权。")
    add("")
    add("## 能力表")
    add("")
    add("| model | 名称 | 媒介 | 需输入图 | 需 prompt | 单价 | 说明（节选） |")
    add("|---|---|---|---|---|---|---|")
    for it in items:
        note = (it.get("notes") or "").split("。")[0][:60]
        add(f"| `{it['id']}` | {it.get('title', '')} | {it.get('media', '')} | "
            f"{'✅' if it.get('requires_image') else '—'} | {'✅' if it.get('requires_prompt') else '—'} | "
            f"{_billing_of(it)} | {note} |")
    add("")
    add("## 上游模型选项（可当 `model` 直接传）")
    add("")
    add("文生图族接受**上游模型 key / web 面板名 / 火山方舟模型名**"
        "（大小写、`-`/`_`/空格差异会被归一）：")
    add("")
    add("| 传给本服务 | 上游模型 | 面板名 | 方舟模型名 | 单价 |")
    add("|---|---|---|---|---|")
    seen = False
    for it in items:
        for m in it.get("upstream_models") or []:
            seen = True
            ark = m.get("ark_name")
            add(f"| `{m['key']}` | `{m['key']}` | {m.get('web_name') or '—'} | "
                f"{('`' + ark + '`') if ark else '—'} | "
                f"{_billing_of({'credits_measured': m.get('credits_measured')})} |")
    if not seen:
        add("| — | — | — | — | — |")
    add("")
    add("⚠️ **方舟模型名只登记了已实测过的档位**（目前 Flash）。写成 "
        "`doubao-seedream-5-0-pro-260628` 这类**未登记**的方舟名，会被当成"
        "第三方 SDK 的**占位名**走默认模型（Seedream 5.0 Lite，0 积分）—— "
        "**不是**静默换成收费档。完整清单见 `GET /v1/models` 的 `ark_name` 字段。")
    add("")
    add("## 错误表（由 `app/errors.py` 的异常类派生）")
    add("")
    add("| code | HTTP | type | 重试 | 何时出现 |")
    add("|---|---|---|---|---|")
    for code, http, etype, retry in _error_rows():
        add(f"| `{code}` | {http} | `{etype}` | {retry} | {_ERROR_NOTES.get(code, '')} |")
    add("")
    add("## 其他端点")
    add("")
    add("| 端点 | 鉴权 | 说明 |")
    add("|---|---|---|")
    add("| `GET /` | 免 | 给人看的落地页 |")
    add("| `GET /healthz` | 免 | 存活 + 版本 |")
    add("| `GET /readyz` | 免 | 就绪（凭据 / 闸门 / 冷却） |")
    add("| `GET /v1/models` | 免 | 本服务宣告的能力（OpenAI 四键形态） |")
    add("| `GET /stats` | 需 | 进程内窗口 / 闸门 / 最近 span |")
    add("")
    add("## 更多")
    add("")
    add("- 仓库（公开）：<https://github.com/rsfree/jimeng>")
    add("- 契约与上游实测：仓库 `docs/` 下的接口与上游文档")
    add(f"- 版本：`{__version__}`；本文件由服务从注册表派生（`app/llms_txt.py`），改能力无需手改文档。")
    return "\n".join(out) + "\n"
