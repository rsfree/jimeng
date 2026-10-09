# 对外契约（冻结）

> 本文件是**唯一的对外契约真相**。改动 = 破坏调用方，必须同步 `tests/test_api.py`。
> 上游侧的字段与错误码在 `docs/UPSTREAM.md`；推导过程在 `.workbuddy/memory/`。

---

## 0. 端点

| 方法 | 路径 | 鉴权 | 状态码 | 用途 |
|---|---|---|---|---|
| `POST` | `/async/v1/images/generations` | 🔒 | `202` | 受理，**只回一个 `task_id`** |
| `GET` | `/async/v1/images/generations/{task_id}` | 🔒 | `202`/`200` | 非终态回排队态；终态回结果 |
| `POST` | `/v1/images/generations` | 🔒 | `200`/`202`/`503` | **同步**出图：创建+轮询合并（≤300s），见 §0.5 |
| `GET` | `/v1/models` | 🔓 | `200` | 能力清单（OpenAI 形态）；**只此一条路径**、**免鉴权** |
| `DELETE` | `/async/v1/images/generations/{task_id}` | 🔒 | `200`/`400` | 删除**已终态**的任务 |
| `GET` | `/async/v1/images/generations` | 🔒 | `200` | 本 Key 的任务列表 |
| `POST` | `/api/v3/contents/generations/tasks` | 🔒 | `200` | **视频唯一入口**：火山方舟契约（创建）。按方舟模型名分流档位；带素材走全能参考 |
| `GET` | `/api/v3/contents/generations/tasks/{id}` | 🔒 | `200` | 火山方舟契约（查询） |

🔒 = **必须** `Authorization: Bearer <key>`（详见 §0.3）；
🔓 = **免鉴权**（刻意公开的发现性端点，仅 `/v1/models`，见 §0.3）。

> 🔴 **2026-09-24 破坏性变更**：原生 `/async/v1/videos/generations` 三路由
> （受理/查询/删除）已**整体移除**，视频一律走方舟门面（`POST/GET
> /api/v3/contents/generations/tasks`）。方舟 `model` **必填**（如
> `doubao-seedance-2-0-mini-260615`），按名分流到对应即梦档位并在
> `degradations` 留痕；带素材（image_url/video_url/audio_url）⇒ 全能参考。

### 0.3 鉴权：**所有业务端点都要 Bearer**（2026-09-23 收紧）

`Authorization: Bearer <key>`，与 `API_KEYS`（逗号分隔）白名单比对；比对通过后
只保留 **Key 的 HMAC 指纹**（明文永不落库）。`API_KEYS` 为空 ⇒ 鉴权整体关闭
（仅限内网，启动打 WARNING）。

**内部映射：Key 指纹 → 任务归属。** 三类读写的可见范围**完全一致**：

| 动作 | 无 Key | 无效 Key | 合法但非属主 | 属主 |
|---|---|---|---|---|
| 受理 / 列表 / 删除 | `401` | `401` | （列表只回自己的） | ✅ |
| **查询单条（图片/视频/方舟）** | **`401`** | `401` | **`404`** | ✅ |
| `GET /v1/models` | **`200`（公开）** | `200` | ✅（不分属主） | ✅ |
| `GET /stats` | **`401`** | `401` | ✅（不分属主） | ✅ |

🔴 **本次收紧的破坏性变更**：查询单条此前是"**`task_id` 即凭据**"（不带 Key 也能读，
带别人的 Key 也能读）。取消它有两个理由：

1. 可见范围不一致 —— 读能靠 id 分享、写/删不能，调用方很容易误判；
2. `task_id` 一旦泄漏（贴进工单、聊天记录、日志）就等价于泄漏产物，
   而它**没有有效期、也没法撤回**。

⇒ 要分享产物请分享产物的 **`url`**，不要分享任务的 `task_id`。

**"不存在"与"不属于你"刻意合并为同一个 `404`**：区分开就等于告诉别人
"这个 id 存在"，那正是枚举的前置条件。

**不鉴权的例外有两类**（其余端点一律要 Bearer）：
1. **探活**（判活必须无凭据可用）：`GET /healthz`（容器 HEALTHCHECK）、
   `GET /readyz`（编排层判活，会 ping 一次库）。
2. **发现性端点 `GET /v1/models`** —— 2026-09-23 起**免鉴权**（用户指令）：
   客户端在配置 Key **之前**先探"这服务有什么能力"是常规做法，内容只有
   能力的公开描述、不含任何任务/凭据/内部状态。
运维端点 `GET /stats` **要鉴权**（内容是内部的：闸门计数、DSN 掩码、协调器派发计数）。
例外名单由 `tests/test_api.py::test_every_business_route_requires_a_bearer`
钉死（`_PUBLIC_PATHS` + `PROBE_PATHS`；**扫描全部方法** —— 2026-09-23 从
"只看 GET"升级：POST/DELETE 才是会产生费用的写路径，只盯 GET 是盲区）
—— **新加路由忘挂鉴权会当场红**。

### 0.4 前缀即语义：`/v1/*` = 同步，`/async/*` = 异步（2026-09-23 起）

🔴 **`/async/v1/models` 已取消** —— 请求它会得到 `404`。
模型清单只有 `GET /v1/models` 这一条路径（**免鉴权**，2026-09-23 用户指令；
其余端点仍一律要 Bearer）。

为什么模型清单进 `/v1`：它是**同步、无任务语义**的端点 —— 没有 `task_id`、
没有轮询、不产生计费。OpenAI 风格的客户端/插件按惯例探的就是 `/v1/models`。
反过来，**异步族（受理/查询/删除）必须留在 `/async/v1`**：它们是
`202 + task_id` + 轮询的**异步任务**语义，挂到 `/v1` 会被误读成同步的
OpenAI images/videos API —— `/async` 前缀本身就是那道护栏。

⚠️ 这道护栏拦的是"**异步语义**挂错前缀"，**不是**"禁止同步端点进 `/v1`"——
2026-09-23 新增的同步出图接口（§0.5）就是 `/v1` 的正式成员。

两侧口径都由 `tests/test_api.py::test_models_endpoint_lives_only_under_v1` 钉死：
`/v1/models` 必须在、`/async/v1/models` **必须不存在**（防止有人"顺手加回来"
让两种前缀重新分叉），外加"`/v1` 下只允许白名单里的**同步**端点"的反向断言
（白名单当前 = `/v1/models` + `/v1/images/generations`）。

### 0.5 同步出图：`POST /v1/images/generations`（2026-09-23 新增）

**创建 + 轮询合并进一个请求**：调用方发一次 POST，服务端在预算内等任务到终态，
直接返回最终体 —— 不用再拿 `task_id` 去轮询。

```
POST /v1/images/generations          Authorization: Bearer <key>
{"model": "jimeng-t2i", "prompt": "…", "image": [], "n": 1}
```

三种结局（**状态码判定，不需要读额外字段**）：

| 结局 | 状态码 | 响应体 |
|---|---|---|
| 预算内到终态 | `200` | 与异步查询的终态体**逐键一致**（成功/失败/取消，见 §2） |
| 预算耗尽仍在跑 | `202` | `{task_id, status}` + `Location: /async/v1/images/generations/{id}` |
| 没有推进者 | `503` | `error.code = "sync_unavailable"` |

* **预算** = `SYNC_MAX_WAIT`（默认 **300s**，墙钟总时长）：排队 + 建任务 +
  上游生成 + 轮询全含在内。
* 降级后任务**不受影响、不会被取消**（即梦没有取消端点）—— 拿 `202` 里的
  `task_id` 走异步轮询即可，`Location` 头已经指向那里。这是**无缝降级**，
  不是失败：调用方甚至可以把它当"少一步的异步受理"来用。
* `503 sync_unavailable` 出现在协调器线程没在跑的部署形态（如
  `COORDINATOR_ENABLED=0`）—— 没有推进者时同步等待注定白等，故**快速说清**。
* 请求校验/降级/能力解析与异步受理**完全同一套**（同一个 `Service.create`），
  报错逐字一致。
* 内部链路：落库 → 叫醒协调器 → 轮询**本地库**直到终态（等待期间**零上游请求**）。

**部署配套（上线前必看）**：请求会在服务端阻塞至多 300s ⇒

| 层 | 要求 |
|---|---|
| gunicorn | `GUNICORN_TIMEOUT`（默认 **360**）必须 > `SYNC_MAX_WAIT`，否则 worker 会被判卡死杀掉 |
| nginx | `proxy_read_timeout` 必须 ≥ 300 + 余量（默认 60s 会在中途**掐断**同步请求） |
| 调用方 | HTTP 客户端读超时应 ≥ 305s，否则自己先断开 |

⚠️ **什么时候别用它**：长排队/长生成（视频族、高并发下的后位任务）用异步族更合适 ——
同步接口会占住一条 HTTP 连接等到底。它适合"交互式、要一步到位"的场景。

### 0.2 火山方舟（Ark）契约门面（2026-09-20 起）

对外形态逐字段对齐方舟《创建/查询视频生成任务》（实现收拢在 `app/ark.py`），
底层翻译到即梦 Seedance 链路 —— 方舟 SDK 客户端无需改代码即可切换：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v3/contents/generations/tasks` | 方舟创建形态：`{model, content[], ratio, duration, resolution, seed, …}` → **只回 `{"id": …}`** |
| `GET` | `/api/v3/contents/generations/tasks/{id}` | 方舟查询形态：`{id, model, status, error, content{video_url}, created_at, …}`；`status ∈ queued/running/succeeded/failed` |

翻译与降级规则（全部在 `degradations` 里可见，查询响应原样带回）：

* `model`：任何 `doubao-seedance-*` → 映射到 `jimeng-t2v`（降级留痕）；
* `content[]`：`text` ⇒ 文生视频；带 `image_url`/`video_url`/`audio_url`
  ⇒ 翻译成即梦**全能参考**（omni_reference，混合参考素材），
  方舟的角色语义（first_frame 等）不逐一对应，降级留痕；
* `ratio "adaptive"` → 默认 16:9（降级留痕）；`duration`/`resolution` 过
  即梦计费档位白名单（720p×4s/5s）；
* `watermark` / `generate_audio` / `callback_url` / `return_last_frame` 等
  方舟常规参数 → **降级留痕不挡人**；
* 🔴 `usage`（completion_tokens/total_tokens）**不给** —— 即梦链路没有
  token 口径，伪造数字等于说谎；预估积分以扩展字段 `usage.forecast_credits`
  给出（仅成功任务）。

### 0.3 全能参考 / 补帧（方舟门面形态）

**全能参考视频**（门面带素材 ⇒ 自动路由到 `jimeng-omni-video`，素材语义覆盖模型分流）：

```json
{
  "model": "doubao-seedance-2-0-260128",       // 方舟名必填；带素材被全能参考覆盖并留痕
  "content": [
    {"type": "text", "text": "用参考视频的构图，图片做首帧"},   // 必填（指令）
    {"type": "image_url", "image_url": {"url": "https://…/a.png"}},  // 可选（走 ImageX）
    {"type": "video_url", "video_url": {"url": "https://…/v.mp4"}},  // 可选（走 VOD → vid）
    {"type": "audio_url", "audio_url": {"url": "https://…/a.mp3"}}   // 可选（走 VOD → vid）
  ],
  "resolution": "720p", "duration": 5
}
```

* 素材总数 ≤ 6（实抓样本 4：2 视频+1 图+1 音频）；
* **计费口径（实抓解出）**：amount = 输出秒数 + Σ输入视频秒数（音频不计）；
  ⚠️ 输入视频时长服务端暂探测不到 ⇒ 预扣只按输出时长计并留痕，
  实扣以积分消耗记录为准；
* 提交侧已按实抓逐字段适配，**端到端实跑未验证**（15.35 积分级）。

**视频生视频 / 补帧**（门面显式信号 ⇒ `jimeng-vfi`）：方舟契约没有补帧概念，
本服务用 **`target_fps` 或 `source_task_id` 字段**表达"视频生视频 = 插帧"意图：

```json
{
  "model": "doubao-seedance-2-0-260128",       // 方舟名必填；有 vfi 信号即被覆盖
  "content": [
    {"type": "text", "text": "把这段视频补到 60fps"},
    {"type": "video_url", "video_url": {"url": "https://…/v.mp4"}}   // 本地/外部视频
  ],
  "target_fps": 60                              // 信号①：插帧目标帧率（默认 60）
}
```

* 两种形态：**本地视频**（`content[]` 恰好 1 条 `video_url` + `target_fps`，
  实测档位 720p×4s——resolution/duration 省略让服务取默认）或
  **引用产物**（`source_task_id` 指向本服务已成功的视频任务，prompt/resolution/
  duration 沿用源任务）；
* 补帧**不重画内容**（同一视频插到 60fps），与全能参考（模仿参考生成新片）
  语义不同——想"模仿参考视频的动作"不要加 `target_fps`；
* 素材混用（图/音频/多视频）+ vfi 信号 ⇒ 400；留痕说明走的是补帧链路。

**方舟门面受理体**（视频唯一入口；🔴 2026-09-24 起对外**全部方舟模型名**，
`jimeng-*` 内部名已从清单/文档退场，仅作路由别名兼容）：

```json
{
  "model": "doubao-seedance-2-0-mini-260615",  // **必填**，按名分流（下表）
  "content": [{"type": "text", "text": "一只猫在跳舞"}],   // 必填，text 合成 prompt
  "resolution": "720p",           // 可省略，默认 720p（已实抓档位）
  "duration": 4,                  // 可省略，单位秒，默认 4（mini 档）
  "ratio": "16:9",                // 方舟字段名；"adaptive" → 16:9 降级留痕
  "seed": 123                     // 可选整数；-1 等价未传
}
```

成功响应（方舟形态）：`{"id": "jimeng_…", "status": "succeeded",
"content": {"video_url": "…mp4"}, "usage": {"forecast_credits": …},
"degradations": [映射留痕]}`。

**方舟模型名 → 即梦档位分流**（每条映射在 `degradations` 响亮留痕；
依据 = 即梦侧 key 实读，见 `docs/UPSTREAM.md` §16）：

| 方舟 model | 本服务能力 | 即梦侧实际档位 |
|---|---|---|
| `doubao-seedance-2-0-mini-*` | `jimeng-t2v` | Seedance 2.0 mini（720p×4s/5s） |
| `doubao-seedance-2-0-fast-*` | `jimeng-t2v-fast` | Seedance 2.0 Fast（720p×5s） |
| `doubao-seedance-2-0-*`（其余） | `jimeng-t2v-pro` | Seedance 2.0 VIP（720p×5s） |
| `doubao-seedance-2-5-*` | `jimeng-t2v-2.5-draft` | 2.5 样片（480p×5s；正式版未接入） |
| 其它 `doubao-seedance-*` | `jimeng-t2v`（兜底留痕） | 同 mini 档 |
| （任意方舟名）+ `target_fps`/`source_task_id` | `jimeng-vfi` | 补帧（视频生视频，插帧 60fps） |
| （任意方舟名）+ 素材素材 | `jimeng-omni-video` | 全能参考（模仿参考生成新片） |

视频的**硬边界**（与"不猜"纪律一致）：

* `(能力, resolution, duration)` 必须命中**实抓档位白名单**，否则受理时 400
  —— `benefit_type`/`amount` 是计费字段，没有抓包依据的档位拒绝构造。
  已实抓：`t2v`=`720p×{4,5}s`、`t2v-fast`=`720p×5s`、`t2v-pro`=`720p×5s`、
  `t2v-2.5-draft`=`480p×5s`；`aspect_ratio` 不在白名单里（不参与计费）；
* 视频草稿**没有张数字段**（实抓确认无 `gen_option`）⇒ 一次一条；
* ✅ **四个视频能力中三个已在 2026-09-23/24 端到端实跑过**（实扣见下表）。

### 0.3 视频实测单价（2026-09-23/24 真跑对账）

三证吻合 = 余额差分 + 消耗记录 + `submit_id`。

| 能力 | 上游模型 | 出片 | 实扣 | 回执 forecast | 耗时 |
|---|---|---|---|---|---|
| `jimeng-t2v-fast` | `dreamina_seedance_40_vision` | 1280×720 | **30** | 156（高估 5.2×） | 93s |
| `jimeng-t2v-pro` | `dreamina_seedance_40_pro_vision` | — | **70** | 453（高估 6.5×） | 167s |
| `jimeng-t2v`（mini） | `dreamina_seedance_40_mini` | 1280×720 | **24**（2026-09-20，4s 档） | 166（高估 ~7×） | 114s |
| `jimeng-t2v-2.5-draft` | `dreamina_seedance_45_pro_draft` | 480p 样片 | **45**（2026-09-24，两次） | 316（高估 7.0×） | 156s |

⚠️ 两条口径（都由实测推翻过）：

1. 我们构造 `benefit_type` 时算的 `amount = 输出秒 + Σ输入视频秒`（≈1/秒）
   **不是实扣**：5s 档实扣 30/70，而 `amount` 传的是 5。它是**预扣字段**，
   真扣由上游定价 —— 要对账只能 `submit_id` + `user_credit_history`。
2. `t2v-fast` 的 UI 标了 `useSeedanceFast5sFreeTrial: true`（5s 免费试用）
   —— **没有生效**，5s 照样扣 30。

**产物元数据**：视频产物带 `width` / `height` / `vid` / `item_id`
（2026-09-23 实读：`1280×720` 与提交的 `16:9` 一致 ⇒ 比例**确实生效**）。

---

## 1. 受理

```http
POST /async/v1/images/generations
Authorization: Bearer <key>
Content-Type: application/json

{
  "model": "jimeng-t2i",
  "prompt": "飞上天",
  "image": []
}
```

**`image` 是数组，张数按能力分**（2026-09-20 起）：

| 能力 | 接受几张 | 说明 |
|---|---|---|
| `jimeng-t2i`（文生图） | 0 | 必须传 `[]`；传了会 400 |
| `jimeng-hd` / `jimeng-pro-hd` / `jimeng-outpaint` | **1** | 上游用单个 `origin_image` 承载；**多传会明确 400** |
| `jimeng-i2i`（图生图） | **1–4**（多张垫图） | 上游草稿的 `image_uri_list` / `image_list` 本来就是列表；**顺序即请求顺序** |

- 全局另有一道 4 张的合理性上限（挡住"一次塞几百个 URL"）。
- 🔴 **三种行为只有两种**：要么**支持**（全部上传、全部进草稿），要么**明确拒绝**。
  **绝不存在第三种（收下 N 张却只用第 1 张）** —— 那会让调用方以为用了 N 张。
- 张数超限一律 `400 invalid_parameter`（`param="image"`），信息里会写明上限与应有做法。

**`n`（出图张数）的默认值**：

- 🔴 **不传 `n` ⇒ 取该模型的最小合法值**（通常就是 **1**），
  **不采用上游自己的 `default_generate_count`**。
  实测上游各家默认不同（5.0 Pro 默认 **2**、5.0 Lite 默认 **4**）——
  照它的默认走，调用方会按"1 张"的预期收到 2~4 张的账单。
  **默认必须是最省的那个。**
- 不传 `n` 时**不会**留下"已吸附"的降级说明（没要求过，就没什么可解释的）。
- **显式传** `n` 时按模型的合法取值**吸附**（取不超过请求值的最大合法值；
  低于下界则抬到下界），并**在 `degradations` 里留痕** ——
  吸附会直接改变花费，不允许静默。
- 示例：`n=3` 而该模型选项是 `(1,2,4)` ⇒ 生效 `2`，且
  `degradations` 里出现"请求 n=3 已吸附为 2"。


**响应 `202`**：

```json
{ "task_id": "jimeng_b8d9f0b8247f4eeda60f84c908e192cb" }
```

`Location: /async/v1/images/generations/{task_id}`

### 请求字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `model` | 否* | 见 §3。留空时按 `image` 是否为空推导 —— 但**只在无歧义时**给默认 |
| `prompt` | 看能力 | `t2i` / `i2i` 必需；后编辑族不需要 |
| `image` | 否 | **数组**。文生图传 `[]`；传字符串会被明确拒绝并给出正确写法 |
| `size` | 否 | `"2048x2048"`。默认 `2048x2048`（抓包实测唯一跑通的档位） |
| `n` | 否 | 出图张数。会**吸附**到该模型服务端声明的合法取值（见 §4） |
| `seed` | 否 | 整数 |
| `negative_prompt` | 否 | 仅文生图生效 |

\* `image` 为空时 `model` 可省；带 `image` 时**必须显式指定** —— 四个能力都能接，
而它们的**实扣并不相同**（hd 0 / i2i 0 / pro-hd 1 积分；outpaint 未对账），
替你挑等于替你做决定。

### 关于「认得但做不到」的字段

出现 `watermark` / `response_format` / `quality` / `style` / `stream` / `user` /
`sequential_image_generation` / `max_images` 时**不报错**，而是进 `degradations`
（见 §5）。其它未知字段 → `400`。

这条区分的判据是：**这是"上游没有"还是"你写错了"？** 两者的修复动作完全不同。
把两者都塞进"不支持"，会让人去查上游能力表 —— 而真正的问题在请求体。

---

## 2. 查询

```http
GET /async/v1/images/generations/{task_id}
Authorization: Bearer <key>
```

### 2.1 非终态 → `202`

```json
{ "task_id": "jimeng_...", "status": "queued" }
```

`status ∈ {queued, in_progress}`。**202 的意思是"还没好，继续轮询"** ——
不要把排队态当结果。

### 2.2 成功 → `200`

```json
{
  "status": "success",
  "data": [ { "url": "https://...jpeg?X-Tos-Expires=86400&..." } ],
  "created": 1789490763,
  "usage": { "images": 1, "forecast_credits": 44 }
}
```

四条刻意的取舍：

1. **五态都带顶层 `status`**（`queued` / `in_progress` / `success` / `failure` /
   `canceled`，2026-09-22 补齐成功态）—— 调用方**只凭这一个字段判终态**，
   不必按"有没有 `data`"推断。取值与自身 `store` 状态同名（小写）。
   **跨系统对齐不在这里做**：对接 New API 任务插件时，由**插件侧**把五种取值
   归一化到其大写枚举（`QUEUED` / `IN_PROGRESS` / `SUCCESS` / `FAILURE`）——
   其中 `canceled` 归 `FAILURE`（New API 无独立取消态）。
2. **`data[]` 里只有 `url`。** 宽高/格式我们确实知道，但**不塞进来** ——
   与冻结契约逐字一致，多一个键就多一分"形状不同"的风险。那些真知识在 trace 里。
3. **结果 URL 原样透传，不做转存。** 参考实现的产物也是上游直链（预签名）。
   ⚠️ 即梦产物链接的有效期**未取证** —— 需要长期可用链接时得另做转存，本服务没做。
4. **`created` 是 epoch 秒**（任务完成时刻）。

⚠️ `usage.forecast_credits` 是**上游回执的预估**（`forecast_generate_cost`），
**不是实际扣费** —— 实测高估数倍，对账口径见 `UPSTREAM.md` 积分章节。

### 2.3 失败 → `200`

```json
{
  "task_id": "jimeng_...",
  "status": "failure",
  "error": { "message": "...", "type": "...", "code": "..." }
}
```

**失败也回 200**：任务本身完成了（只是结果是失败），请求没出错。
回 4xx 会让调用方的重试逻辑误触发。

⚠️ 任务失败**不代表没花钱**：即梦的建任务是计费动作，`status=30 generate_failed`
照样扣积分 —— 错误文案里会明确写出来。

### 2.4 不存在 / 不属于本 Key → `404`

```json
{ "error": { "message": "任务 ... 不存在，或不属于当前 API Key。",
             "type": "invalid_request_error", "code": "task_not_found" } }
```

刻意**不区分**这两种情况（区分开等于告诉攻击者"这个 id 是存在的"），
且**本地拦死、不发上游请求**。

---

## 3. 能力与 `model` 取值

| `model` | 能力 | 输入图 | prompt | 实测单价 | 实测产物 |
|---|---|---|---|---|---|
| `jimeng-t2i` | 文生图 | 不需要 | **必需** | **0（实测免费）** | 2048×2048 |
| `jimeng-i2i` | 图生图（blend，**多垫图上限 4**） | 必需 | **必需** | **0（实测免费）** | 2048×2048 |
| `jimeng-hd` | 超清 | 必需 | 不需要 | **0（实测免费）** | 4096×4096 |
| `jimeng-pro-hd` | 智能超清 | 必需 | 不需要 | **未测** | 2160×2160 |
| `jimeng-outpaint` | 扩图 | 必需 | 不需要 | **未测** | **4 张** 4000×4000 |

> ⚠️ 单价口径（2026-09-20 校准）：这些值**只放实测值**。原先那批（44/40/9/91/28）
> 全部取自上游回执的 `forecast_generate_cost` —— 那是**预估**，实测**高估 4~9 倍**
> （见 `docs/UPSTREAM.md` §12）。**`0` = 实测不扣分**，不是占位；未实测的写「未测」。

**别名**（同样接受）：裸能力名 `t2i` / `i2i` / `hd` / `pro-hd` / `outpaint`；
中文 `即梦` / `文生图` / `图生图` / `超清` / `智能超清` / `扩图`；
英文 `jimeng` / `text2image` / `image2image` / `upscale`；
**别名大小写不敏感**。

### 3.1 web 面板名（2026-09-23 起）

**即梦网页上看到的名字可以直接当 `model` 传**，等价于 `jimeng-t2i` + 对应上游模型：

| 面板名 | 等价于 |
|---|---|
| `Seedream 5.0 Flash` / `5.0 Flash` | `high_aes_general_v50_flash` |
| `Seedream 5.0 Pro` / `5.0 Pro` / `图片 5.0 Pro` | `high_aes_general_v50p_large` |
| `Seedream 5.0 Lite` / `5.0 Lite` | `high_aes_general_v50`（**默认**） |
| `Seedream 4.7` / `4.7` / `图片 4.7` | `high_aes_general_v43` |
| `Seedream 4.6` / `4.5` / `4.1` / `4.0`（及短名 `4.6`…） | 对应的 `v42` / `v40l` / `v41` / `v40` |

**归一规则**：小写化后，把**空格 / `_` / `.` / `·`(U+00B7) / `・`(U+30FB)** 一律看作 `-`，
并折叠连续 `-`。⇒ `Seedream 5.0 Flash` / `seedream 5.0 flash` /
`SEEDREAM_5.0_FLASH` / `seedream-5-0-flash` 是同一个东西。

⚠️ **四条边界**（都由门禁钉住）：

1. **面板名只换"上游模型"，不会跨能力** —— 别指望 `Seedream 5.0 Flash` 帮你做图生图。
2. **上游 key 仍是逐字精确匹配**（`HIGH_AES_GENERAL_V50P_LARGE` 会被判未知模型）；
   规范化**只**用于面板名查表。
3. **`Seedream 3.0` / `3.1` 明确拒绝**（400，报错里给出上游 key 与理由：
   该系列实测 `ret=1006` 权益不足）—— 面板上点得到、本服务没登记时**不许静默退化**
   成默认模型。
4. **已登记的方舟图片模型名可原样传**（2026-10-02，共 3 条）——
   `doubao-seedream-5-0-flash-260915` → Flash（3）、
   `doubao-seedream-5-0-pro-260628` → Pro（8）、
   `doubao-seedream-5-0-260128` → Lite（0）。
   它们自带 `doubao-seedream` 前缀（在 `PLACEHOLDER_PREFIXES` 里），
   靠"别名判在占位判之前"这条从占位里救回来。
   ⚠️ **未登记**的方舟名（如 `doubao-seedream-4-5-251128`）仍落回默认 Lite ——
   **刻意不映射**：认一个没对账过的名字 = 替调用方悄悄切到另一条计费链路
   （Pro 是 8 积分/张），"降级只给错图、升级要扣钱"。
5. 🔴 **方舟名必须一模型一名，不许通配**。`v4*` 族有 **5 个** key
   （4.0/4.1/4.5/4.6/4.7），若把同一个方舟名指给整族，别名表反向派生时
   **后者覆盖前者、静默丢掉 4 个模型** —— 而 `/v1/models` 里两条还都显示着它，
   看着完全正常。门禁 `test_ark_name_is_one_to_one_and_never_collapses`。

7. 🔴 **鉴权 = sessionid 透传**（2026-10-08 用户口径"鉴权用 SESSIONID 就行"）：
   `Authorization: Bearer <即梦 sessionid>` —— **不再查 `API_KEYS` 白名单**，
   该 sessionid 直接作为上游凭据用于**这个任务**。
   · 换 sessionid / 换号**不用改服务配置**，调用方自带凭据。
   · 🔴 **代价（知情接受）**：任何持有即梦登录态的人都能调本服务；
     计费与风控归调用方本人，服务不代付。
   ·「不带 Authorization」仍 401；带别人的凭据查别人的任务 → **404**
     （不泄露该id 是否存在）。
   · ⚠️ 旧配置项 `API_KEYS` **不再参与校验**（保留仅为不破坏现有 .env）。

6. 🔴 **`model` 可用后缀指定分辨率档**（2026-10-02 方案 B）：
   `Seedream 5.0 Lite 4k` / `mj-v8.2-2k` / `high_aes_general_v50-4k`
   （后缀取 `-1k` / `-1.5k` / `-2k` / `-4k`，也接受空格写法）。
   **只认精确后缀** ⇒ 方舟名（`...-260628`）与 `4-7` 这类面板名不会被误判。
   **两处冲突当场 400，绝不静默取一个**：
     · 与 `size` 表达的档位不一致 ⇒ 400（两者是同一件事）；
     · 该模型**服务端没声明**这一档（如 Flash 无 4k、mj82 无 4k）⇒ 400，
       **绝不静默退回** 2k（那会"以为买了 4k、实际按 2k 计费"）。

`GET /v1/models` 的 **`jimeng-t2i`** 条目里带 `upstream_models`，
逐项给出 `{key, web_name, ark_name, credits_measured}` —— 调用方不必去别处对照名字。
（`credits_measured` 是**按模型**的实测值，不是能力级单价；`ark_name` 是可原样传的
**火山方舟模型名**，该档位没有方舟对应名时为 `null`。）

**上游模型 key —— 已登记的 8 个**（可直接当 `model` 传，等价于 `jimeng-t2i` + 该模型）：

| `model` 传这个 key | 上游名字 | 方舟对应名 | 张数选项 | 实测单价 |
|---|---|---|---|---|
| `high_aes_general_v50_flash` | **Seedream 5.0 Flash** | `doubao-seedream-5-0-flash-260915` | **1..4** | **3/张** |
| `high_aes_general_v50`（**默认**） | Seedream 5.0 **Lite** | — | 1..8 | **0（免费）** |
| `high_aes_general_v50p_large` | **Seedream 5.0 Pro** | — | **1..4** | **8/张** |
| `high_aes_general_v43` | Seedream 4.7 | — | 1..8 | 未测 |
| `high_aes_general_v42` | Seedream 4.6 | — | 1..8 | 未测 |
| `high_aes_general_v40l` | Seedream 4.5 | 1..8 | 未测 |
| `high_aes_general_v41` | Seedream 4.1 | 1..8 | 未测 |
| `high_aes_general_v40` | Seedream 4.0 | 1..8 | 未测 |

⚠️ **传上游 key 时的三条纪律**：

1. **精确匹配、必须全小写** —— 该分支**不做小写化**（与别名不同）：
   `HIGH_AES_GENERAL_V50P_LARGE` 会被判「未知 model」。
2. **只会落成 `jimeng-t2i`（文生图）** ⇒ **上游 key 不能用来选 i2i 的模型**（不能垫图）。
3. **`v50p_large`（5.0 Pro）是收费档**：实测 **8 积分/张**（2026-09-20，
   1:1 / 3:4 / 16:9 三种尺寸同价；账单按 `submit_id` 对上、余额差分吻合）。
   Lite 下 t2i/i2i/hd 实测实扣 0。

上游实际有 **10 个**模型：上表 8 个之外还有 **Seedream 3.0 / 3.1**
（key 带冒号：`high_aes_general_v30l:general_v3.0_18b` 等）—— **本服务未登记**，
传它们会被拒为未知模型。

🆕 `high_aes_general_v50_flash`（**Seedream 5.0 Flash**）是 **2026-09-23** 服务端能力表
实读到的**新模型**（`is_new_model: true`）—— 登记依据是**上游自己宣告**
（"能读的就不许猜"），**不是端到端实跑**，故**不报单价**。
它与 Lite 的分工：官方 tip 是"轻量化版 Seedream 5.0 Pro，更快更便宜"，
默认 2k，benefit_type `image_basic_v50_flash_2k` / `_15k`。
复现方式：`python scripts/dump_video_models.py --new`（只读、零成本）。

**占位名**（`auto` / `dall-e-3` / `gpt-image-1` / `seedream-*` …）等价于"没写 `model`"，
走默认推导 —— 第三方 SDK 常硬编码这些值，它们不代表调用意图。

🔴 **占位名兜底会留痕（2026-10-02 新增）**：`seedream-*` 前缀在
`PLACEHOLDER_PREFIXES` 里，所以**任何 `seedream-<我们没登记的型号>`**
（`seedream-9-9-ultra` / `seedream-4-9` / `seedream-5-0` …）都会被判成占位名
⇒ 兜底到**免费**默认档（`high_aes_general_v50`）出图。
**兜底本身是设计意图**（不存在的型号不该硬报 400），但**必须是可见的**：
这类请求的 `degradations` 会带一条

> `model='…' 不是本服务已登记的型号（也不在面板名里），已按占位名兜底到 'high_aes_general_v50'（免费档）。要指定具体型号请用 GET /v1/models 里的名字。`

调用方据此知道拿到的是兜底图。**已登记的型号不产生这条留痕**，
「不写 `model`」的正常默认推导也不算降级。
（实测记录：v0.1.9 线上这两条留痕是**空**的 —— 调用方拿到的是它没点的模型
却毫无察觉。门禁见 `tests/test_models.py::test_unregistered_seedream_falls_back_to_free_default`。）

🔴 **别按名字选工具**：`pro-hd`（"智能超清"）只出 2160²，
而 `hd`（"超清"）出 4096² —— 名字里的"更高级"是错觉。

### 刻意缺席的能力

（历史记录：`jimeng-detail-fix` 曾因"直接贴 `origin_image` 两次 `generate_failed`
且照样计费"被摘除。2026-09-20 查明死因是 `origin_image` 这个**形态**不被支持，
改用**引用形态**（`source_task_id` 指向本服务已成功的图片任务）后一次真跑成功
⇒ 已重新注册，`credits_measured=0`。2026-10-02 复跑再次通过（22.4s）。
**教训**：能力"失败"时先分清是**形态不对**还是**能力不存在** ——
前者可修，后者才摘除。）

---

## 4. `n` 的吸附（会改变花费）

`n` 会被吸附到**服务端**为该模型声明的合法取值上（运行期零成本读取
`get_common_config`，不是代码里的经验值）：

- 实测 `high_aes_general_v50` = **1..8**（默认 4）；`…v50p_large` = 1..4；
- 吸附取**不超过请求值**的最大合法值；低于下界则抬到下界；
- **每次吸附都会写进 `degradations`** —— 张数直接乘积分，静默降级等于让人
  按 A 的预期为 B 付费；
- 后编辑族不支持指定张数（扩图固定出 4 张，由上游决定）：传了就进 `degradations`。

---

## 5. `degradations`（本服务的加性扩展）

任何"请求了 A、实际做了 B"都会出现在这里（**仅非空时出现该键**）：

```json
{ "degradations": ["模型 high_aes_general_v50 的张数选项为 [1..8]，请求 n=12 已吸附为 8",
                   "参数 watermark=True 本服务不支持…，已忽略；不要按它的语义预期结果。"] }
```

来源有三类：参数吸附、输入图归一化、服务端能力表读取失败退回冻结快照。

---

## 6. 错误信封

```json
{ "error": { "message": "...", "type": "...", "code": "...",
             "param": "可选", "retry_after": 可选, "detail": "可选" } }
```

| `code` | HTTP | 含义与**下一步** |
|---|---|---|
| `invalid_parameter` | 400 | 请求写错了。`param` 指出是哪个字段 |
| `content_policy_violation` | 451 | 内容审核拦截（上游送审/版权/前置决策预审）⇒ 换 prompt 或换图。451 = Unavailable For Legal Reasons，"内容因合规原因不可用"（2026-10-09 起，原 400） |
| `invalid_api_key` | 401 | 调用方的 Key 不对 |
| `task_not_found` | 404 | 不存在或不属于本 Key |
| `upstream_rate_limited` | 429 | 上游限流，**可退避重试**（带 `Retry-After`） |
| `upstream_quota_exhausted` | 429 | 积分/日额度耗尽，**重试无效** |
| `risk_control_challenge` | 429 | 命中风控，**重试会延长标记**，服务已进入冷却 |
| `upstream_error` | 502 | 上游 5xx / 非 JSON / WAF 页 |
| `upstream_not_configured` | 503 | 服务未配 `JIMENG_SESSIONID`（**部署问题**，不是你的错） |
| `upstream_timeout` | 504 | 上游超时 |

`Retry-After` **只在是真的才知道**的时候给 —— 编一个数字等于伪造事实。

---

## 7. 删除

| 任务状态 | `DELETE` 行为 |
|---|---|
| 非终态（`queued` / `in_progress`） | **`400`** —— 即梦**没有取消端点** |
| 终态 | `200 {"task_id": "...", "status": "deleted"}`，删掉本地记录 |

🔴 未终态任务的删除**必须响亮失败**。本地置"已取消"就返回成功有三个后果：
① 上游任务继续跑、继续扣积分，而调用方以为停了；② 本地与上游状态永久不一致；
③ 没有任何出口能看出来。

---

## 8. 测试与运行

```bash
export TEST_DATABASE_URL='postgresql+psycopg2://jimeng:<密码>@127.0.0.1:5432/jimeng_test'
python -m pytest -q
```

⚠️ 缺 `TEST_DATABASE_URL` 时 store / API / 协调器类用例会**失败**（不是跳过）——
静默跳过会让人把"没跑"当成"跑过了"。签名向量 / 能力解析 / 可观测性等
纯离线用例不依赖数据库，随时可跑。
