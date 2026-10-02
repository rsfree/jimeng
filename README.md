# jimeng-service

即梦（`jimeng.jianying.com`）图片/视频生成的出口。参考 `doubao-stream` 的
`/v1/images` 接口形态：默认两段式（`POST` 受理 → `GET` 轮询），
图片族另有**同步一步式**（创建+轮询合并，≤300s，见下表第二行）。

```
POST /async/v1/images/generations        → 202 {"task_id": "jimeng_…"}     只回一个 id
GET  /async/v1/images/generations/{id}   → 202 排队态 / 200 {data,created,usage}
POST /v1/images/generations              → 200 终态体 / 202 降级 / 503     同步：创建+轮询合并（≤300s）
POST /api/v3/contents/generations/tasks  → 200 {"id": …}  **视频唯一入口**：火山方舟原生契约
GET  /api/v3/contents/generations/tasks/{id} → 200 方舟查询形态（queued/running/succeeded/failed）
POST /async/v1/videos/generations        → （已移除，2026-09-24：视频一律走方舟门面）
GET  /v1/models                          → 能力清单（只此一条路径；/async 那份已取消；🔓 免鉴权）
```

契约全文见 **[`docs/INTERFACE.md`](docs/INTERFACE.md)**（冻结）；上游契约见
**[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**。

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务 | `docs/INTERFACE.md` |
| 改「谁能做什么 / 单价 / 别名」 | `app/models.py`（唯一的能力注册表） |
| 改即梦的请求构造 / 错误码分类 | `app/upstream/jimeng/client.py` |
| 改签名算法 | `app/upstream/jimeng/sign.py`（11 条抓包向量自证） |
| 改输入图上传 | `app/upstream/jimeng/upload.py` |
| 改"什么时候轮到谁跑" | `app/coordinator.py` + `app/gate.py` |
| 改任务存取 | `app/store.py`（SQLModel + PostgreSQL） |
| 改响应形状 | `app/service.py::view` —— **唯一出口** |
| 改埋点 | `app/observability.py` —— **唯一收拢点** |

---

## 1. 跑起来

```bash
cp .env.example .env      # 填 JIMENG_SESSIONID 与 POSTGRES_PASSWORD
docker compose up -d --build
curl -s localhost:8200/healthz          # {"status":"ok"}
```

⚠️ **数据库刻意不发布宿主端口**（`db` 只有容器内网的 `5432/tcp`）：
应用走 compose 内网 `db:5432`，宿主端口毫无必要，而发布它只会制造冲突源
（本机 5432 常年被别的项目占着 ⇒ `compose up` 报 "port is already allocated"，
看着像本服务的问题，其实不是）。要在宿主上做管理：

```bash
docker compose exec db psql -U jimeng -d jimeng
```

本机直接跑（需自备 PostgreSQL）：

```bash
export TASK_DB='postgresql+psycopg2://jimeng:<密码>@127.0.0.1:5432/jimeng'
export JIMENG_SESSIONID='<浏览器 cookie 里的 sessionid>'
export API_KEYS='sk-xxxxxxxx'           # 留空 = 关闭鉴权（仅内网）
gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

🔴 末尾那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'` /
`App failed to load.` —— **单测全绿也照样炸**，因为它们都直接调 `create_app()`。
`tests/test_wiring.py::test_dockerfile_cmd_target_resolves` 钉的就是这条。

⚠️ 直接用 gunicorn 跑时**记得设 `COORDINATOR_ENABLED=0`**，
否则它会真的去建任务（**计费**动作）。

### 本机没有 PG？起一个独立的

```bash
scripts/local-pg.sh init      # 初始化（幂等）
scripts/local-pg.sh start     # 启动 + 建库（幂等）
scripts/local-pg.sh dsn       # 打印 TASK_DB / TEST_DATABASE_URL，直接 export
scripts/local-pg.sh stop      # 停止（destroy 才删数据，不可逆）
```

它复用本机已装的 PostgreSQL 二进制（默认 `/Library/PostgreSQL/15/bin`），
在 **5433** 起一个**独立实例**（数据目录 `$HOME/pgdata/jimeng`），
**不碰你机器上已有的 5432 集群** —— 不猜它的密码、也不和别人的数据混在一起。

### 直接跑测试

```bash
eval "$(scripts/local-pg.sh dsn)"    # 一次性导出 TASK_DB 与 TEST_DATABASE_URL
python -m pytest -q
```

### 凭据只要一个

即梦**唯一的硬前提**是 cookie 里的 `sessionid`（实测负对照：其余 20 多个 cookie、
`sign` / `a_bogus` / `msToken` 全部不需要）。取法：登录站点 → DevTools →
Application → Cookies → `sessionid`。

---

## 2. 这套设计的六条主线

### 2.1 受理不碰上游，协调器才是唯一执行者

`POST` **只落库就返回**（请求内零上游往返）。真正建任务由后台协调器做，
因为它要受节奏闸门约束、要能重试、要防止重复提交 —— 而**建任务是计费动作**。

即梦没有回调，只能自己轮询。⇒ 协调器**不是可选增强，是链路的一环**：
没有它，任务永远停在 `queued`（连建任务都不会发生），超时看门狗也永不触发。

⚠️ **同步接口（`POST /v1/images/generations`）不改变这条主线**：它同样只落库，
等待期间**只查本地库** —— "建任务"与"轮询上游"仍然全部由协调器做。
它把"调用方原本要做的多次轮询"折叠进一个请求里，而不是抢协调器的活；
正因如此，没有协调器在跑的部署里它会**快速 503**（`sync_unavailable`），
而不是白等一整个预算。

### 2.2 并发上限放在库里数，不放在信号量里

`JM_CONCURRENCY` 的判据是 `count(status='in_progress')`。

用 `Semaphore` 的话，**进程重启后信号量归零**，而库里那些任务其实还在上游跑
⇒ 重启后会超发。按库计数天然重启安全，也天然跨 worker。

### 2.3 降级必须可见

任何"请求了 A、实际做了 B"都进响应的 `degradations`：张数吸附、图片归一化、
服务端能力表读不到而退回冻结快照。静默降级 = 让人按 A 的预期为 B 付钱。

### 2.4 鉴权：静态 Bearer Key 白名单（两层，都不是 JWT/OAuth）

**门（调用方 → 本服务）**

`Authorization: Bearer <key>`，与 `API_KEYS`（逗号分隔的环境变量）做**白名单比对**。
没有用户体系、没有令牌签发、没有过期时间、没有 scope —— 就是一份静态名单。

- `API_KEYS` 为空 ⇒ **鉴权整体关闭**，请求算作 `anonymous`；启动会打 WARNING。
- 通过后 Key 被换成**不可逆指纹** `credential_id = HMAC-SHA256(secret, key)`，
  `secret` 首次启动随机生成并存在任务库 `meta` 表 ⇒ **明文 Key 永不落库**。
- 任务与该指纹绑定：换一把 Key 读/删别人的任务 ⇒ **404，且不发上游请求**（本地拦死）。
- 🔴 **2026-09-23 收紧：所有业务端点（任何方法）都要 Bearer**，含**全部 GET**
  （任务查询、`/stats`）。此前"查询单条可不带 Key（`task_id` 即凭据）"
  的口径**已取消** —— 可见范围与写/删不一致，且 `task_id` 泄漏即等价于产物泄漏
  （无有效期、无法撤回）。分享产物请用产物 `url`。
  **例外有两类**：探活端点 `/healthz` `/readyz`（判活必须无凭据可用）+
  **发现性端点 `/v1/models`**（2026-09-23 起免鉴权：客户端在配置 Key 之前
  先探"这服务有什么能力"是常规做法，内容不含任务/凭据）。
  门禁：`tests/test_api.py::test_every_business_route_requires_a_bearer`
  （`_PUBLIC_PATHS` + 探活名单；扫描**全部方法** —— POST/DELETE 才是会产生费用的
  写路径，只盯 GET 是盲区）。

**上游（本服务 → 即梦）**

**cookie 复用**，唯一硬前提是 `sessionid`。没有 aksk / OAuth / API Key；
`sign` 头照带但**不参与鉴权**（实测负对照证明），所以不需要每次现取 token。

**已知弱点（不粉饰）**

| # | 问题 | 影响 |
|---|---|---|
| 1 | `key not in api_keys` 是**明文元组的 `in`**，逐元素 `==`，**不是恒定时间比较** | 理论上可按响应时间差逐字节猜 Key。API Key 是高熵随机串，实际利用难度大，但正确写法是 `hmac.compare_digest` |
| 2 | **没有按 Key 的配额/限流** | 闸门是**全局**的；任何一把合法 Key 都能持续消耗即梦积分 |
| 3 | **轮换 Key 会孤儿化历史任务** | 任务绑定旧指纹，换 Key 后旧任务 404（刻意设计，但运维要知道） |
| 4 | **鉴权关闭是默认态** | 只有一条启动 WARNING 兜底；`/readyz` 不看它，所以编排层发现不了"没开鉴权" |
| 5 | 无 IP 白名单 / 无 mTLS / 无审计日志（只有一行请求摘要） | — |

⇒ 生产部署的建议：**必须设 `API_KEYS`**，并在网关层再叠一层鉴权与限流；
需要按 Key 计量的话，本服务当前做不到。

### 2.5 不制造假能力

- 细节修复（`super_resolution`）实测两次 `generate_failed` ⇒ **不注册**，
  也不出现在 `/v1/models`（见 `app/models.py::DELIBERATE_ABSENCES`）；
- 未配凭据时 `POST` 回 **503** 而不是假装受理；
- 上游凭据失效也是 **503**（部署问题），不是 401（调用方的问题）。

### 2.6 观测面**不脱敏**，但凭据不进属性

logfire 侧**全量上报**上下游明细：原始请求/响应、`upstream_submit_id`、重试过程、
降级告警。属性名与值都**不做任何事后改写** —— 事后"顺手脱敏"会改掉上游的实际字段名，
让人对着面板排查一个**不存在的**字段。

`scrubbing=False` 必须保持：SDK 自带 scrubber 按**值子串**命中
`credential`/`token`/`auth`，而即梦产物是 TOS 预签名 URL（必含 `X-Tos-Credential=`）
⇒ 打开它会把**每一条结果 URL** 打成 `[Scrubbed due to 'Credential']`，面板直接不可读。

**「密钥不上报」是另一条纪律，靠实现约束而不是过滤器**：

1. cookie / `sessionid` 只在 `JimengClient.jar` 里，从不作为属性传入；
2. 上游埋点的 `http_path` **只给 pathname、丢掉整个 query**；
3. `capture_headers=False` —— 请求头整个不采集。

⚠️ 代价：**新增埋点时不得把凭据塞进属性**。这条由
`tests/test_observability.py::test_upstream_event_carries_no_credentials_and_drops_the_query`
守着。想改回脱敏设 `OTEL_SCRUBBING=1`（只影响 SDK 自带 scrubber）。

---

## 3. 三条容易踩的坑（都吃过亏）

1. 🔴 **"被接受" ≠ "能跑通"。** 上游返回 `ret=0` 只说明请求被受理，
   任务仍可能终态 `status=30 generate_failed`，**而且照样计费**。
   ⇒ 判成败**只看 `task.status`**，不看 `ret`。（细节修复就是这么白花 2×16 积分的。）
2. 🔴 **别按名字选工具。** `hd`（"超清"）**实测免费**、出 **4096²**；
   而 `pro-hd`（"智能超清"）只出 2160² 且**未测单价**（旧 forecast 报 91，
   实测高估 4~12 倍，别信）。名字里的"更高级"是错觉。
   ⚠️ 同族还有个**按模型换价**的坑：`jimeng-t2i` 的能力级单价是 **0（Lite 口径）**，
   但换上游模型就换价 —— Flash **实测 3/张**、Pro **实测 8/张**。
   按模型的实测价看 `/v1/models` 里 `jimeng-t2i` 的 `upstream_models`
   （含 `ark_name` = 可原样传的**火山方舟模型名**，如
   `doubao-seedream-5-0-flash-260915` → Flash）。
   ⚠️ **方舟名只登记了已实测过的档位**：`doubao-seedream-5-0-pro-260628` 这类
   未登记名会被当**占位名**落回默认 Lite（0 积分）—— 是"给错模型"，
   **不是**"静默换到 8 积分的 Pro"。
3. 🔴 **别用经验值替代可读的服务端数据。** 张数上界曾按"用户经验 1–4"写死成 4，
   而服务端声明默认模型是 **1..8**。现在运行期零成本读 `get_common_config`。

---

## 4. 扩容前必读

**默认 `WORKERS=1` 是架构约束，不是保守参数：**

- 节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是**进程内**状态，
  副本数 N 等于把限速整体乘 N —— 恰好踩在上游风控最敏感的维度；
- 协调器靠数据库租约选主，多 worker 虽安全但非持锁进程只会空转。

**提吞吐的正确顺序**：先把 `JM_CONCURRENCY` 从 1 提到实测上限（**≥4**：
并发 2/4 实测 2/2、4/4 成功，无 `1010`/`1057`/`2020`），再考虑多副本。
放宽并发会成倍放大积分消耗速率与风控暴露面，是**策略选择**不是技术限制。

---

### 已做的三项优化（都有实测，`scripts/bench_poll.py`）

| # | 问题 | 改前 | 改后 |
|---|---|---|---|
| 1 | 一轮 tick 的上游查询次数（N=4 在途） | **4 次**（每次带 1 个 id） | **1 次**（带全部 4 个 id） |
| 2 | 同一段墙钟内（3s）的上游查询次数 | **12 次** | **1 次**（间隔 2s） |
| 3 | 每 tick 的计数查询 | `SELECT task_id` 再 `len()`（搬回全部行） | `SELECT COUNT(*)` |

实测输出：

```
① 逐任务调用（改前）：4 次，每次带 [1, 1, 1, 1] 个 id
   合并调用（改后）  ：1 次，每次带 [4] 个 id
③ JIMENG_POLL_INTERVAL=0.0 ⇒ 12 次查询 ／ =2.0 ⇒ 1 次查询
```

**优化 2 的根因值得单说**：`JIMENG_POLL_INTERVAL` 此前只被传给了
`JimengClient(poll_interval=…)`，而服务从不调 `client.wait()`/`generate()` ——
于是协调器**每个 tick（默认 1s）就打一次上游**，配置项**读了却没效果**。
静态门禁只能查"有没有人读"，查不出"读了有没有用"；这类"假配置"只能靠
对着调用链人眼过一遍。⇒ 现在三道门（总超时 / 起轮宽限 / **轮询间隔**）都在
`Service.poll_many` 里，且有用例守着。

### 真正的吞吐闸门仍然是 `JM_CONCURRENCY`

上面三项是**降低单位产出的上游请求量与数据库开销**，不会提高出图吞吐。
吞吐的天花板是 `JM_CONCURRENCY`（默认 **1**，即串行），而它是**策略选择**：
放宽会成倍放大积分消耗速率与风控暴露面。

优化 1（批量轮询）的意义正在于此：**它是"把并发提上去"的前提** ——
否则提并发等于把上游请求量一起乘 N，而那正是风控最敏感的维度。
实测上游至少容忍 4 并发（无 `1010`/`1057`/`2020`）。

## 5. 测试


```bash
export TEST_DATABASE_URL='postgresql+psycopg2://jimeng:<密码>@127.0.0.1:5432/jimeng_test'
python -m pytest -q
```

- **零真实上游调用**：所有用例注入假上游，一个字节都不发出去（建任务会真花钱）。
- **缺库就响亮失败，不静默跳过** —— 跳过会让人把"没跑"当成"跑过了"。
- 每个用例一个独立 PostgreSQL schema，用完 CASCADE 删掉。
- 纯离线用例（签名向量 / 能力解析 / 可观测性 / 接线门禁）不需要数据库。

### 🔴 启动冒烟必须关协调器

```bash
export COORDINATOR_ENABLED=0     # ← 否则协调器会真的去上游「建任务」
```

协调器默认开启，而**建任务是计费动作**。做"服务能不能起来"的冒烟时如果不关它，
它会代替你向上游提交任务 —— 本仓实测踩过一次（用的假 `sessionid`，
上游在鉴权层回了 `1015`，**没有生成、没有计费**，但这是运气不是设计）。

### 端到端探针：零成本优先，花钱要显式开闸

```bash
# 四个零成本阶段：契约自检 + 真实上传 + 草稿预演
python scripts/e2e.py --cookie-file /path/to/cookie_jimeng.txt

# 真实出图（会扣积分；不加 --allow-real-submit 只打印"将要发生什么"）
python scripts/e2e.py --cookie-file … --phases generate \
    --model jimeng-hd --image /path/to/local.png --allow-real-submit
```

| 阶段 | 花积分 | 做什么 |
|---|---|---|
| `models` | ❌ | `/v1/models` 契约自检 |
| `accept` | ❌ | `POST` 受理并断言**只回一个 task_id**（协调器没启 ⇒ 零上游往返） |
| `upload` | ❌ | **真打上游**跑火山 ImageX 四段式 + `get_image_by_uri` 免费验真 + 缓存命中 |
| `dry` | ❌ | `submit(dry_run=True)` 走完草稿构造与张数吸附，**不发请求** |
| `generate` | 🔴 | 真建任务 + 轮询到终态，**必须 `--allow-real-submit`** |

`generate` 单独隔出来，是因为建任务是**计费动作**，且即梦**失败了也照样扣积分**。
单价**不写死在这里** —— 由 `generate` 阶段现读 `/v1/models` 的
`credits_measured`（`null` = 未实测，**不等于免费**）。
（原先这里抄的是上游 `forecast_generate_cost`：`hd` 9 · `outpaint` 28 · `i2i` 40 ·
`t2i` 44 · `pro-hd` 91 —— 实测**高估 4~12 倍**，已作废。）

### 接线门禁（`tests/test_wiring.py` + `ruff.toml`）

**它抓到过两个能过 `compileall`、也能正常 `import` 的线上级缺陷**：

| 缺陷 | 症状 | 抓它的规则 |
|---|---|---|
| `client.py::_report()` 引用未 import 的 `OBS` | 被 `except Exception: pass` 吞掉 ⇒ **上游埋点从来没生效，日志里一个字都没有** | `F821`（未定义名） |
| `service.py::fingerprint_secret()` 还在调 SQLite 时代的 `store._connect()` | **`Service()` 一构造就 `AttributeError`**，整个服务起不来 | `SLF001`（跨层摸私有成员） |

门禁分两层，缺一层都会漏：

- **静态**：`ruff check`（`F` / `E9` / `BLE` / `S110` / `PLC0415` / `SLF001`），
  **刻意不收风格规则** —— 一次报上百条的门禁等于没有门禁；
- **动态**：`test_wiring.py` 里的用例**真的把链路跑一遍**（用 `httpx.MockTransport`
  打 `JimengClient._post`，断言**确实产生了一条上报**）—— "接线"这件事只有跑才证明得了。

另有两条配置门禁：**每个被读的配置项都必须存在**（否则 `AttributeError`）、
**每个配置项都必须有人读**（没人读的旋钮 = 假配置，会让人以为已调优）。

⚠️ 已知坑：**别在只 `--select RUF100` 的情况下跑 `--fix`**。RUF100 判断"noqa 是否多余"
依赖当时启用了哪些规则 —— `BLE` 没启用时它会把 `# noqa: BLE001` 判成多余并删掉，
等 `BLE` 再启用时那些异常就变成未标注状态（本仓踩过一次，后用 `--add-noqa` 重新标注）。

---

## 6. 诚实边界（未取证的事）

| 事项 | 状态 |
|---|---|
| 即梦产物 URL 的有效期 | **未取证**。故原样透传、不做转存；需要长期链接时得另做 |
| 多图输入（>1 张） | 未取证。四个图类能力的草稿结构实测都只带一张 ⇒ **明确拒绝而不是静默丢图** |
| `input_image_limit` 的量纲 | 服务端有该字段，含义待坐实；故只做保守压缩，不猜阈值 |
| 上游是否回收未引用的上传素材 | 未知。上传缓存取 6h 保守值 |
| `sign` 是否会被风控动态启用 | 码表里有 `1014 ErrSign`，但实测**不参与鉴权**。照带、不依赖 |
| 本地 `a_bogus` / secsdk | 不需要（实测）。全链路纯 HTTP，零浏览器 |

---

## 7. 目录

```
app/
  main.py             FastAPI 装配：路由 / 错误信封 / lifespan / 埋点接线
  config.py           Settings（每个旋钮都必须有人读）
  errors.py           错误分类体系（对外错误信封）
  models.py           能力注册表（唯一真相）
  media.py            输入图嗅探 / 下载 / 归一化
  service.py          编排：受理 / 建任务 / 轮询 / 响应构造
  coordinator.py      后台协调器（推进任务的唯一执行者）
  gate.py             节奏闸门（间隔 / 每分钟上限 / 风控冷却）
  store.py            SQLModel + PostgreSQL 任务存储
  observability.py    logfire + loguru 单一收拢点
  upstream/jimeng/    sign / client / upload / capabilities
tests/                见 §5
docs/                 INTERFACE.md（对外契约）/ UPSTREAM.md（上游契约）
```
