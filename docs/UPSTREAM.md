# 即梦上游契约（精简）

> 只记录**服务依赖的事实**及其**出处**。逐字段的取证过程在上游研究仓的
> `docs/upstream/jimeng-image-api.md`；这里是够用的那一份。
> 标 ❓ 的是**未取证**项 —— 不要把它们当结论用。

站点：`https://jimeng.jianying.com`（mweb）。产物名 `即梦 / Dreamina`，
服务端自报模型 `Seedream 5.0 Lite`。

---

## 1. 鉴权

🔴 **最小凭据 = cookie 里的一个键 `sessionid`。**

负对照（读写端点一致的判据）：

| 格 | cookie | `sign` | 读接口 | 写接口 |
|---|---|---|---|---|
| W1 / A | ✅ | ✅ | `ret=0` | `ret=1002`（业务错误，鉴权已过） |
| W3 | ✅ | ❌ | `ret=0` | `ret=1002`（**与 W1 同码**） |
| C / E / W2 | ❌ | — | `ret=1015 login error` | `ret=1015 login error` |

⇒ W1 == W3 ⇒ **`sign` 不参与鉴权**。其余 20 多个 cookie、`a_bogus`、`msToken`
全部不需要。**全链路纯 HTTP，零浏览器。**

⚠️ 码表里有 `1014 ErrSign` ⇒ 签名校验**实现上是有的**，可能按风控力度动态开启。
故本实现**照带但不依赖**（纯 MD5、零成本）。

## 2. 签名

```
sign = md5("9e2c|" + pathname[-7:] + "|" + pf + "|" + appvr + "|" + unix秒 + "|" + tdid + "|11ac")
```

`pf=7`、`appvr=8.4.0`、`sign-ver=1`。与 cookie / 请求体 / query **都无关**
⇒ 完全离线可复现。`device-time` 必须等于参与签名的那一秒。

11 条真实抓包向量在 `app/upstream/jimeng/sign.py::SELF_TEST_VECTORS`，
`verify_vectors()` 直接断言。

## 3. 端点

| 用途 | 路径 | 计费 |
|---|---|---|
| 建任务 | `POST /mweb/v1/aigc_draft/generate` | 🔴 **扣积分** |
| 取任务 | `POST /mweb/v1/get_history_by_ids`（body `{"submit_ids":[…]}`） | 免费 |
| 历史列表 | `POST /mweb/v1/get_history`（body **必须** `{"count":N}`，空 body → `2008`） | 免费 |
| 换签名 URL | `POST /mweb/v1/get_image_by_uri`（body **`{"uris":[…]}` 复数**，单数 → `1000`） | 免费 |
| 模型能力表 | `POST /mweb/v1/get_common_config`（body `{}`） | 免费 |
| 上传 STS | `POST /mweb/v1/get_upload_token`（body `{"scene":2}`） | 免费 |

列表 / 取消：**无取消端点**（见 §7）。

## 4. 建任务的两个硬形态

1. 🔴 **`draft_content` 是「JSON 字符串」而不是 JSON 对象**（双重编码）。
   写成对象 → `1002 common error`。
2. **`submit_id` 由客户端自生成**（uuid4）⇒ 不依赖回执格式，回执解析失败也能轮询。

`metrics_extra` 也是 JSON 字符串，且**三种能力形态不同**（文生图 / blend / 后编辑），
照抄抓包，不拼凑。

## 5. 任务状态

原文出处：`7960.13db92ac44.js` / `5794.fa66eab7e8.js`（两处一致）。

| 值 | 名 | 终态 | 本服务映射 |
|---|---|---|---|
| 0 | init | 否 | `queued` |
| 10 | pre_check_reject | 是 | `failure` |
| 20 | submitted | 否 | `in_progress` |
| 30 | generate_failed | 是 | `failure` |
| 40 | post_check_reject | 是 | `failure` |
| 42 | thinking | 否 | `in_progress` |
| 45 | partial_success | 是 | `success`（带产物提示） |
| 50 | success | 是 | `success` |
| 100 | deleted | 是 | `canceled` |

🔴 **成败只看 `task.status`**：`item.common_attr.status=144` **不是**生成状态；
`ret=0` 只说明"请求被受理"。`status=30` **照样计费**。

实测：4 张 2048×2048 约 19s；并发 2/4 实测无 `1010`（至少容忍 4 并发）。

## 6. 错误码（89 条全表已取证，分类决定重试语义）

分类入口 `client.classify_reject` / `client.raise_for_ret`，代码在
`app/upstream/jimeng/client.py`。

| 类 | 码 | 语义 |
|---|---|---|
| 限流（可退避） | `1` `1010` `1057` `2014` `2020` `10020` | 并发/频率上限 |
| 额度（**重试无效**） | `1006` `4001` `121101` | 积分不足 / 日额度用尽 |
| 风控（**重试会加剧**） | `1018` `1019` `1021` `2035` `2038` `2039` `2041` `2042` `2043` | 必须退避 |
| 内容审核 / 版权 | `1063` `1159` `2003` `2004` `2005` `2048` `2050` | 换 prompt / 换图 |
| 参数 / 业务体 | `1001` `1002` `1161` `1162` `3021` `4003` `4010` … | 只有 `1001/1002` 对"草稿形态"有判别力 |
| 凭据 | `1015` | cookie 里没有有效 `sessionid` |

🔴 **只有 `1001/1002` 说明"服务端读不懂这个草稿"。** 把 `1006`（积分/权益）
当成形态问题，曾产出一条假结论。

## 7. 取消：**没有**

上游只有"建任务 + 查询"两个接口 ⇒ 未终态任务的取消**做不到**。
本服务对未终态 `DELETE` **响亮失败**（400），绝不本地置"已取消" —— 那会让
"其实还在跑并继续计费"变成看不见的事。

## 8. 上传（本地图片 → `image_uri`）

火山引擎 ImageX 四段式，**纯 stdlib AWS4 签名，零浏览器零积分**：

```
① POST /mweb/v1/get_upload_token {"scene":2}    → STS + space_name + upload_domain
② GET  https://<upload_domain>/?Action=ApplyImageUpload&Version=2018-08-01
        &ServiceId=<space>&FileSize=<n>          → UploadAddress{StoreInfos, UploadHost, SessionKey}
③ POST https://<UploadHost>/upload/v1/<StoreUri>  (裸字节 + Authorization: <Auth>)
④ POST ?Action=CommitImageUpload  body {"SessionKey":"<b64(UploadAddress)>"}
```

产出 `tos-cn-i-<space>/<hash>` = 草稿要的 `image_uri`。

🔴 **两个坑**：两条请求的 `SignedHeaders` **不一样**
（GET `x-amz-date;x-amz-security-token` **不含 host**；
POST `content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token`）；
响应字段是 `UploadHost`（**单数**）而非 `UploadHosts`。

🔴 **`image_uri` 不是内容寻址**：同一份字节传两次得到两个**不同**的 uri
⇒ 不缓存既重复 4 次 HTTP、又在账号里堆重复素材。故本服务加了三层：
STS 双检锁 + 内容哈希→uri 缓存 + in-flight 去重（并发同内容 8 个 uri → 1 个）。

## 9. 后编辑工具族 = 同一端点追加组件

不是四个端点：往 `component_list` 追加一个带 `postedit_param` 的
`image_base_component`，`generate_type` 字符串选工具、参数放 `abilities.<同名 key>`。

| 工具 | `generate_type` | `postedit_param.generate_type` | 实测 |
|---|---|---|---|
| 超清 | `normal_hd` | 13 | ✅ 2048² → **4096²**；forecast 报 9，**实扣 0（免费）**（余额差分多次未见变化；旧的"实扣见过 1"系 pro-hd 记录误归，2026-09-23 已核） |
| 智能超清 | `pro_hd` | 35 | ✅ → 2160²；**实扣 1**（2026-09-22 消耗记录 `智能超清2.0-2k amount=1` + submit_id 归属核实）；forecast 报 91，**高估 91 倍** |
| 扩图 | `painting` | 8 | ✅ → **4 张** 4000²（张数由上游定）；forecast 报 28，**实扣未测** |
| 细节修复 | `super_resolution` | 2 | ✅ **item 引用形态 + 免费**（2026-09-20 路 A）；upload 形态 ❌ 两次 failed |

### 9.1 细节修复的输入图：不支持公网直链，且仅上传也未必够（2026-09-20 抓包）

补抓一次真实 UI 的细节修复请求（Pro 链上第 2 跳），三个结论：

1. **输入图不是 URL、也不是 tos uri**：`postedit_param` 全部字段只有
   `{type, id, generate_type: 2, item_id, origin_history_id}` —— 引用的是
   **账号里已有作品**。整个 draft 里没有 `origin_image` / `image_url` /
   `image_uri` 任何一个字段。
2. **链式形态**：`super_resolution` 组件带 `parent_id` 指向前面的
   `generate`（Pro）父组件。我们两次失败的提交是**单组件草稿**
   （`origin_image` 带 tos uri + 补了 `core_param`）—— 形态差异明确存在，
   但"缺父链"与"缺已有作品"哪个是死因，未经真跑不能断言。
3. **`source_from: "link"`（外链）无证据支持**：前端 JS 枚举里确实有这个取值，
   但所有已收集的抓包里**从未出现过**。结论：主输入图不支持公网直链。

⇒ 对外部图的可行路径：上传换 tos uri 后**先成功生成一张**（拿到
`item_id`/`origin_history_id`），再对那张作品做细节修复 —— 三步，不是一步。
（仅上传不生成、直接拿 tos uri 去细节修复 = 正是失败的形态。）

### 9.2 路 A 验证成功（2026-09-20 17:53）：死因就是 origin_image，不需要父链

`scripts/detail_fix_probe.py`（`--go`）单组件 + `item_id`/`origin_history_id`
（**无 `origin_image`**、无父链）真跑一次：

- `submit_id=47ae9aaa-0782-474f-8268-45d4c9b5c3a3`，~10s 到 `status=50 success`，
  出图 **1 张 2560x1440**（与源图同尺寸）。
- **结论**：细节修复的前置条件 = 引用账号已有作品（`item_id` +
  `origin_history_id`）；带上 `origin_image`（tos uri）反而失败。
  9-19 两次失败的死因即此，链式父组件**不是**必要条件。
- **实扣 = 免费（定论）**：UI 标价"细节修复 免费"；实测提交后 ~20 分钟
  `user_credit_history` 零出账、余额 6140 分文未动 —— 标价与实测两证吻合
  （2026-09-20 18:12 关账）。
- `build_post_edit_draft` 已支持该形态（不给 uri/url 时要求
  `item_id`+`origin_history_id`，此时不带 `origin_image`），
  用例 `test_post_edit_item_reference_form_has_no_origin_image` 钉死。
- **仍未注册对外能力**：对外契约怎么"引用一张已有作品"（传本地 task_id？
  直接传 item_id？）是产品决定，定了再接线（见 models.DELIBERATE_ABSENCES）。

## 10. 图生图（blend）

不是另一个端点，而是同一个 `component_list` 里的另一种组件（`gen_type=12`、
`generate_type="blend"`）。结构逐字段照抄账号历史里的 30 条真实样本，关键点：

- 组件**没有 `metadata`**（与后编辑组件不同）；
- `abilities.blend.ability_list[0].name = "byte_edit"`；
- **需要 prompt**（描述要怎么改）——这是它与后编辑族的区别；
- 实测 ✅ 2048²；forecast 报 40，**实扣 0（免费）**。

## 11. 服务端模型能力表（免费、可读、**别猜**）

`POST /mweb/v1/get_common_config`（body `{}`）按模型下发：
`feats` / `generate_count_options` / `default_generate_count` /
`resolution_map`（比例 → **精确像素**，1k/2k/4k）/ `input_image_limit`。

实测：`high_aes_general_v50`（默认）张数 **1..8**（默认 4）；
`high_aes_general_v50p_large` = **1..4**。

### 模型枚举全表 —— **key ⇄ 面板名字**（2026-09-20 实时拉取，权威）

面板上看到的名字与请求里的 `model_req_key` **不是一回事**，对照如下
（`generate_count_options` 同时列上，因为张数直接乘积分）：

| `model_req_key`（请求里传的） | 面板名字 | 张数选项 | 本服务是否登记 |
|---|---|---|---|
| `high_aes_general_v50p_large` | **Seedream 5.0 Pro** | **1..4** | ✅ |
| `high_aes_general_v50` | **Seedream 5.0 Lite** | 1..8 | ✅（**默认**） |
| `high_aes_general_v43` | Seedream 4.7 | 1..8 | ✅ |
| `high_aes_general_v42` | Seedream 4.6 | 1..8 | ✅ |
| `high_aes_general_v40l` | Seedream 4.5 | 1..8 | ✅ |
| `high_aes_general_v41` | Seedream 4.1 | 1..8 | ✅ |
| `high_aes_general_v40` | Seedream 4.0 | 1..8 | ✅ |
| `high_aes_general_v30l_art_fangzhou:general_v3.0_18b` | Seedream 3.1 | —（未声明） | ❌ **未登记** |
| `high_aes_general_v30l:general_v3.0_18b` | Seedream 3.0 | —（未声明） | ❌ **未登记** |

⚠️ 三条（都实测过）：

1. **key 大小写敏感**：`resolve()` 那条分支**不做小写化**，`HIGH_AES_...` 会被判未知 model。
2. **key 只会落成文生图（t2i）** ⇒ **不能用它给 i2i 选模型**。
3. 后两个 key **带冒号**（`:general_v3.0_18b`）且**未登记** ⇒ 传了会被拒；
   要支持得先登记（并确认它们也能出图）。

> 结论：**"5.0 Pro 怎么传" = `"model": "high_aes_general_v50p_large"`**（全小写、一字不差）。
> ⚠️ Lite 是免费档（实测 t2i/i2i/hd 实扣 0）；**Pro 实测 8 积分/张**（2026-09-20 账单对账）。


### 枚举值逐项复核（2026-09-20，与网页端面板对齐）

**比例** —— `resolution_map[bucket].image_ratio_sizes` 是**数组**，
`ratio_type` 取 `1..8`，**顺序与面板逐项一致**：

| ratio_type | 比例 | 2K 像素 | 4K 像素 |
|---|---|---|---|
| 1 | 1:1 | 2048×2048 | 4096×4096 |
| 2 | 3:4 | 1728×2304 | 3520×4693 |
| 3 | 16:9 | 2560×1440 | 5404×3040 |
| 4 | 4:3 | 2304×1728 | 4693×3520 |
| 5 | 9:16 | 1440×2560 | 3040×5404 |
| 6 | 2:3 | 1664×2496 | 3328×4992 |
| 7 | 3:2 | 2496×1664 | 4992×3328 |
| 8 | 21:9 | 3024×1296 | 6197×2656 |

- 面板上排在最前的**"智能"不在这里** —— 它对应草稿里的
  `intelligent_ratio: true`（文生图草稿默认写 `false`）。
- ⚠️ **4K 的像素不是整数倍**（3:4 → 3520×4693），看来是按面积换算的 ——
  **别自己按比例乘**，要用就直接读这张表。

**分辨率** —— 桶名 `1.5k` / `2k` / `4k`，`resolution_name` 分别是
标清 1.5K / 高清 2K / 超清 4K。**不是每个模型都有三档**：
**5.0 Pro 有 `1.5k`**，5.0 Lite 及往下只有 `2k` / `4k`。
`default_resolution_type` 实测 `2k`。

**采样步数** —— `sample_steps`：5.0 系 `steps=16`（`min 10` / `max 41`）。

**生成数量** —— `generate_count_options` + `default_generate_count`，
⚠️ **默认值各家不同**：5.0 Pro 默认 **2**、5.0 Lite 默认 **4** ——
所以"不传张数"在两者上拿到的结果不一样。

**输入图上限** —— `input_image_limit` 原生是**数组、按 ability 分**：
`[{'max_image_num': 10, 'ability_name': 'byte_edit'}]`
（`byte_edit` 就是 blend / 图生图那个 ability；5.0 Lite 未声明该字段）。
🔴 我们这边 `capabilities.py` 用 `_as_int()` 读它 ⇒ **对数组恒得 `None`**
（且目前没有消费方，所以是"声明了却常为 None 的死旋钮"）。
⚠️ 也就是说 `Capability.max_images`（i2i 现在写死 4）**比上游声明的 10 更保守** ——
要放开得先按数组解析这个字段。

⇒ 运行期读取并缓存（`app/upstream/jimeng/capabilities.py`），
**代码里那份快照只作探测失败时的兜底，且会如实标注降级**。
教训：曾按"用户经验 1–4"写死上界 4，对默认模型直接是错的。

## 11.4 凭据：**只有 `sessionid` 是硬前提**（2026-09-20 实测）

拿一份**完整的浏览器 cookie jar（27 个键、2281 字符）**做过对照实测：

| 传入 | 结果（`POST /mweb/v1/get_user_info`） |
|---|---|
| 完整 cookie 串 | `ret=0`，`name='BettermeTry'` |
| **只给 `sessionid`** | `ret=0`，`name='BettermeTry'` |
| **只给 `sessionid`、完全不传 cookie** | `ret=0`，`name='BettermeTry'` |
| 什么都不给 | ✗ `JimengAuthError: 缺少 sessionid —— 即梦唯一的硬前提凭据` |

⇒ **27 个键里只有 `sessionid` 起作用**，其余（`sid_guard` / `ttwid` / `odin_tt` /
`passport_csrf_token` / `uifid` / …）在我们用的 `/mweb/v1/*` 路径上**都不是必需**。
**不需要保存整串 jar** —— `JIMENG_SESSIONID` 一个值就够（本仓现在就是这么做的）。

### 从 `sid_guard` 能读出**到期时间**（运营要看）

```
sid_guard = <sessionid>|<签发时间戳>|<有效秒数>|<到期 GMT 字符串>
示例      = a419…fda9|1789021926|31536000|Fri, 10-Sep-2027 06:32:06 GMT
```
⇒ 这一份凭据**有效期一年、到期 2027-09-10**。到期时整个服务会开始 401，
**运营应提前轮换**（这是个纯字符串解析，零成本可做巡检）。

⚠️ `passport_csrf_token` 存在说明**写操作**可能有 CSRF 校验；我们走 `/mweb/v1/*`
（生成/上传）实测不受影响。

## 11.5 积分余额 / 消耗记录（**只读、不计费**，2026-09-20 实测）

```
POST /commerce/v1/benefits/user_credit_history
body: {"count": 20, "cursor": "0", "history_type": 2}     # 2 = 消耗
```

**鉴权：两种形式等价**（实测同一次调用，三种变体都返回 `200` / `ret=0` / 同一余额）：

| 变体 | 结果 |
|---|---|
| `Cookie: sessionid=<…>`（本仓客户端现有做法） | ✅ `ret=0` |
| `Authorization: Bearer <sessionid>` | ✅ `ret=0` |
| 两者都带 | ✅ `ret=0` |

⇒ 走 Cookie 即可，**不必为它改鉴权代码**。（签名头那套
`sign` / `x-secsdk-web-signature` / `device-time` / `uifid` 由
`app/upstream/jimeng/sign.py` 现成算好，`_post` 自动带。）

**响应**：`data.total_credit`（**账户可用积分**）/ `data.records[]` /
`data.new_cursor` / `data.has_more`。
`records[]`：`amount` / `create_time`（**秒**）/ `title` / `submit_id` / `status`。
`title` 例："图片生成"、"智能超清2.0-2k"。
**`submit_id` 与我们任务的 `upstream_submit_id` 对得上 ⇒ 可按任务对账。**

### 🔴 实测：回执里的 `forecast_generate_cost` **严重高估**

| 任务 | 回执 `forecast_*`（我们报成 `usage.credits`） | **实际扣（本接口）** |
|---|---|---|
| i2i 双垫图 | **55** | **0（用户确认也免费）** |
| hd（"智能超清2.0-2k"） | **9** | **1** |
| Pro t2i（1:1 / 3:4 / 16:9 各 1 张） | **93 / 94 / 106** | **8**（三尺寸同价；余额 6188→6164 差分吻合） |

⇒ **别把 `forecast_generate_cost` 当实际扣费报给调用方**（高估 4~12 倍）。
`Capability.credits_measured` 目前全部取自 forecast，**都偏高**，应改用本接口校准。

⚠️ 两个陷阱：
- **消耗记录会延迟结算**：跑完当场查可能还没有那条记录 ——
  **不能因为"没看到记录"就断定免费**；要等结算，或用 `total_credit` **跑前/跑后差分**。
- 积分**会过期清零**（记录里出现过 "积分到期清零"）。

## 12. 模型单价（2026-09-20 账单对账；forecast 只当参考）

| 档位 | 回执 forecast | **实扣（账单）** |
|---|---|---|
| Lite t2i（`high_aes_general_v50`，默认） | 44 | **0（免费）** |
| Lite i2i（blend 双垫图） | 55 | **0（免费，账号主确认）** |
| `jimeng-hd`（normal_hd） | 9 | 见过 **1**（账单"智能超清2.0-2k"） |
| **Pro t2i（`high_aes_general_v50p_large`）** | 93~106 | **8/张**（1:1 / 3:4 / 16:9 同价；余额差分吻合） |
| `pro_hd`（智能超清）/ `painting`（扩图） | 91 / 28 | 未测 |

⇒ forecast **普遍高估 4~12 倍**，只能当"要花钱"的预警，**别当实扣报**。
旧结论"v43=35 < v50=44 < v40=51、选旧模型省积分不成立"基于 forecast，**作废** ——
实测口径下 Lite 免费、Pro 8/张，**选对档位才是省积分**。
最老的 `v30l:general_v3.0_18b` 直接 `ret=1006`（权益不足，不可用）。

## 13. 文生视频（Seedance t2v，2026-09-20 实抓）

**端点与图片族同一个**：`POST /mweb/v1/aigc_draft/generate`。差异全在草稿与 `extend`：

| 维度 | 图片族 | **视频族（t2v）** |
|---|---|---|
| 组件类型 | `image_base_component` | **`video_base_component`**（`min_version "1.0.0"`） |
| `generate_type` | `generate` / `blend` / 工具名 | **`gen_video`** |
| 参数位置 | `abilities.generate.core_param` 等 | **`abilities.gen_video.text_to_video_params`** |
| 张数 | `abilities.gen_option.gen_count` | **没有**（实抓确认无 `gen_option`；`batchNumber=1` 只是埋点） |
| 组件附加 | — | **`process_type: 1`** |
| `extend` | `root_model` | `root_model` + **`m_video_commerce_info`（计费字段）** + `m_video_commerce_info_list` |

### 已实抓样本（唯一依据）

* 模型：`dreamina_seedance_40_mini`（网页端 Seedance 4.0 Mini，babi_param 场景
  `makesame-text_to_video`，`generate_type: t2v`）；
* 参数：`video_mode 2` · `fps 24` · `duration_ms 4000` · `resolution "720p"` ·
  `video_aspect_ratio "16:9"` · `idip_meta_list []` · `priority 0`；
* 计费：`benefit_type "seedance_20_mini_720p_output_5s"` · `amount 4`
  （⚠️ 档位名叫 output_5s 但时长是 4s —— **照抄抓包，别"纠正"**）；
* `metrics_extra` 是视频专用形态（`enterFrom "ai_feature"` /
  `functionMode "omni_reference"` / `aiFeatureName "21228507900428"` /
  `sceneOptions` 字符串），且**原样复本**挂进草稿的 `video_task_extra`。

### 🔴 三个未取证边界（适配层据此设了硬闸）

1. **计费档位白名单**：`(resolution, duration) → (benefit_type, amount)` 只有上表一档
   （`client.VIDEO_COMMERCE`）。1080p / 5s / 10s 等没有抓包 ⇒ **拒绝构造**（受理时 400），
   绝不猜 —— 计费字段写错 = 按错档位扣积分。
2. **结果回包结构未实抓**：`get_history_by_ids` 查询侧已实抓（submit_id 可查），
   但视频 item 的回包（`item_list[].video.video_url`?）没抓到 ⇒ 解析为尽力而为
   并在产物上标注；解析不到按零产物 = 失败处理。
3. **视频模型清单服务端不下发**：`get_common_config` 的 `model_list` 只有 9 个
   `high_aes_general_*`（图片）。视频模型按场景（babi_param `tool_video`）单独下发，
   只读接口拿不到 ⇒ **新模型要靠补抓提交包**登记（`dump_video_models.py` 探针可复跑验证）。

### 13.1 视频补帧（insert_frame / VideoFrameInterpolation，2026-09-20 实抓）

同一端点、同一根模型（`dreamina_seedance_40_mini`），但形态与 t2v 有四处硬差异：

| 维度 | t2v | **补帧（vfi）** |
|---|---|---|
| 草稿结构 | 单组件 | **双组件**：父 = 源 t2v 组件**原样重放**（实抓连组件 id/created_time 都没变）+ 子带 `parent_id`、`process_type: 3` |
| 子组件参数 | `video_aspect_ratio`/`seed`/`model_req_key`/`priority` | **全没有**；多了 `vid` / `lens_motion_type ""` / `motion_speed ""` / `template_id 0` / `v2v_opt.insert_frame{enable,target_fps:60,origin_fps:24,duration_ms}` / `origin_history_id`（**字符串**） |
| 引用 | — | `gen_video.scene="insert_frame"` + `video_ref_params{generate_type:0, item_id, origin_history_id（**数字**）}` |
| 计费 | `seedance_20_mini_720p_output_5s` / 4 | **`video_frame_interpolation` / `amount 0`（免费档）** |

`metrics_extra` 也是 click 形态（`promptSource "custom"`、无 `position`/`aiFeatureName`），
且 `originSubmitId`/`previewSubmitId` 指向**源任务** submit_id、`originId` 指源视频 item_id；
`sceneOptions` 多一个 `{"scene":"VideoFrameInterpolation"}` 条目。
草稿 `min_version` 是 **3.1.0**（t2v 是 3.0.5）。

⚠️ 实抓里 `insert_frame.duration_ms=4097`（源视频**实际**时长）≠ 请求的 4000 ——
我们拿不到实际时长，默认取请求时长，语义差异未验证。
⚠️ 引用三件套（vid/item_id/origin_history_id）只有走我们自己的产物链才拿得到
⇒ 补帧入口必须给本服务的 `source_task_id`（跨凭证引用已拦）。

### 13.2 全能参考视频（omni_reference / unified_edit_input，2026-09-20 实抓）

`min_version "3.3.9"` + `min_features ["AIGC_Video_UnifiedEdit"]`。
**混合参考素材**（实抓样本：2 视频 + 1 图 + 1 音频）：

* `prompt` 字段为 **""**，指令在 `unified_edit_input.meta_list` 的 text 条目里；
* `material_list`：video→`video_info.vid`（fps:0/duration/cover 空）·
  image→`image_info.image_uri`（**与图生图同一 ImageX 上传链路**）·
  audio→`audio_info.vid`+duration+name；
* 🔴 **引用结构**：meta_list 用 `material_ref.material_idx` 引用素材 ——
  **视频素材不进 meta_list**，图片/音频各一条，指令文本一条（照抄抓包）；
* `sceneOptions.materialTypes` 顺序编码：**2=video 1=image 3=audio**。

### 13.3 计费口径（两条实抓联立解出）

| 样本 | 时长 | 输入视频 | amount |
|---|---|---|---|
| t2v | 4s | 无 | **4** |
| 全能参考 | 5s | 10.35s（两段合计） | **15.35** |

⇒ **amount = 输出秒数 + Σ输入视频秒数**（≈1 积分/秒；音频不计入）。
`benefit_type` 只按分辨率定（都是 `seedance_20_mini_720p_output_5s`，
名字里的 output_5s 与实际时长无关 —— 4s 的也叫它，照抄别纠正）。
⚠️ 服务端暂探测不到输入视频时长（VOD CommitUploadInner 只回宽高/大小，
无 duration）⇒ 本服务预扣只按输出时长计并留痕，实扣以积分记录为准。

### 13.4 视频/音频上传（VOD，与图片的 ImageX 并列的第二条上传链）

`ApplyUploadInner` → POST `https://{UploadHost}/upload/v1/{StoreUri}`
（**与 ImageX 同款 TOS 上传网关**，带 `content-crc32`）→ `CommitUploadInner`
（body `{"SessionKey":…,"Functions":[]}`，content-type text/plain）→ **`vid`**。

* 端点固定 `vod.bytedanceapi.com`；AWS4 签名 service=**vod**（不是 imagex）；
* Apply 响应形态：`Result.InnerUploadAddress.UploadNodes[0]`，**Vid 在 Apply
  阶段就已分配**（与 SessionKey 解码内容一致）；
* Commit 响应：`Results[0]` = `{Vid, VideoMeta{Uri,Width,Height,Size}}`；
* STS 与图片同一把（`get_upload_token`，policy 同时授 vod:*/ImageX:*）；
  ⚠️ **AWS4 密钥推导的 service 必须跟请求一致** —— 曾因 scope 行改成 vod、
  推导仍用 imagex 常量而全量 SignatureDoesNotMatch；
* ⚠️ 上传 host（`*.snssdk.com`）**不能走系统代理**（502 ProxyError）——
  客户端 `trust_env=False` 直连；
* 全链路**免费**（不产生生成、不扣积分），已用真实小文件端到端验证。

---

## 14. 服务端能力表实读：新模型 **Seedream 5.0 Flash**（2026-09-23）

只读复现（**零成本、不出图、不扣积分**）：

```bash
python scripts/dump_video_models.py --new               # 新模型巡检（一行一条）
python scripts/dump_video_models.py --all --raw /tmp/cfg.json
```

`POST /mweb/v1/get_common_config`（body `{}`）本次返回 **10 个图片模型**（§11 那张表
列的是 9 个）。**新增的那一个**：

| 字段 | 值 |
|---|---|
| `model_req_key` | `high_aes_general_v50_flash` |
| 面板名字 | **Seedream 5.0 Flash** |
| `is_new_model` | **`true`**（`feats` 里也带 `new_model`） |
| `model_tip` | 「轻量化版 Seedream 5.0 Pro，更快更便宜」 |
| `generate_count_options` | **1..4**（`default_generate_count` = 4） |
| `resolution_map` | **1.5k / 2k** 两档；`default_resolution_type` = `2k` |
| 2k 比例表 | 1:1 2048² / 3:4 1728×2304 / 16:9 2560×1440 / 4:3 2304×1728 / 9:16 1440×2560 / 2:3 1664×2496 / 3:2 2496×1664 / 21:9 3024×1296 |
| `benefit_type`（服务端声明） | `image_basic_v50_flash_15k` / `image_basic_v50_flash_2k`，`amount` 均 **1** |
| `input_image_limit` | `[{"max_image_num": 10, "ability_name": "byte_edit"}]`（同 §11 的读不出口子） |

**它被登记成什么**：`app/models.py::UPSTREAM_MODEL_KEYS` 多一项 ⇒
`model: "high_aes_general_v50_flash"` 等价于「`jimeng-t2i` + 该上游模型」；
张数快照 `client.COUNT_OPTIONS_BY_MODEL` 同步为 `(1, 2, 3, 4)`。
**没有**变成新能力（它不是一个能力，是 t2i 族的一个模型选项）。

### 🔴 三条纪律（别越线）

1. **证据等级 = "上游自己宣告"，不是"实跑过"。** 依据只有这张只读能力表
   （"能读的就不许猜"）。§11 的反例还在眼前：v30l / v30l_art **表里也有**，
   实跑 `ret=1006` 权益不足 ⇒ **读得到 ≠ 用得了**。所以
   `credits_measured` 保持 `None`（**不报单价**），notes/文档都写明"未端到端实跑"。
2. **服务端声明的 `amount: 1` 不是实扣。** §12 已证：Pro（`v50p_large`）服务端
   `amount` 也是 1，而**实测 8 积分/张**。⇒ 任何"Flash 一张 1 积分"的推论都不成立，
   要对账只能 `submit_id` + `user_credit_history`（§11.5）。
3. **视频模型仍然不在这张表里。** 全 10 条的 `duration_option` /
   `video_aspect_ratio_option` / `fps` 恒 `null`/`0`，且 `common_config()`
   的 `model` 参数**被服务端忽略**（`_ = model`，按 tk 下发图片全集）
   ⇒ 「t2v-fast / t2v-pro 到底支持哪些比例与时长」**读不到**，
   缺口与 §13 一致：**只能靠补抓提交包**，别拿图片表的字段去推视频。

### 巡检动作（上游再上新模型时）

1. `scripts/dump_video_models.py --new` —— 看有没有新的 `is_new_model`；
2. 有的就按上表把 key 登记进 `UPSTREAM_MODEL_KEYS`、张数同步进
   `COUNT_OPTIONS_BY_MODEL`；
3. 门禁在 `tests/test_models.py::test_new_upstream_model_flash_is_registered_with_declared_count_options`
   （钉"可路由 + 张数与服务端一致 + 不报单价"）。

---

## 15. 真跑对账：Flash + 两个视频变体（2026-09-23）

用户明确授权真实提交后跑的**三笔**。口径：余额差分 + `user_credit_history`
记录 + 任务表 `upstream_submit_id` **三证吻合**才算数（余额 5962 → 5859，共花 103）。

| 提交的东西 | 档位 | 上游 `forecast_generate_cost` | **实扣** | 耗时 |
|---|---|---|---|---|
| `Seedream 5.0 Flash`（面板名直传） | 2k / 1 张 | 23 | **3** | 21s |
| `jimeng-t2v-fast` | 720p / 5s / **16:9** | 156 | **30** | 93s |
| `jimeng-t2v-pro` | 720p / 5s / **16:9** | 453 | **70** | 167s |

对账明细（`user_credit_history` 里的原文 title 一并记下，便于复现）：

| submit_id | 记录 title | amount |
|---|---|---|
| `be194863-31a9-48d5-a203-eece74ef7d7f` | 图片生成 | 3 |
| `437eb55b-8c37-4cd5-8ce2-484c0f38a3db` | 视频生成720P 5秒 | 30 |
| `6337a44d-4ade-4408-97dc-972a093b2f70` | 视频生成 | 70 |

### 三条被实测推翻/坐实的推论

1. ❌ **"`amount` = 实扣"** —— 推翻。我们构造的 `amount` 是 5（输出秒数），
   实扣却是 30 / 70。`amount` 只是**预扣字段**，真扣由上游定价（≈6/秒）。
   §13 那条"1 积分/秒"的口径要读成"我们传的字段值"，不是"花掉的钱"。
2. ❌ **"t2v-fast 的 5s 免费试用"** —— 推翻。UI 标 `useSeedanceFast5sFreeTrial: true`，
   实跑照样扣 30。
3. ✅ **"`forecast` 高估 4~12 倍"** —— 坐实并扩展到视频：23→3（7.7×）、
   156→30（5.2×）、453→70（6.5×）。

### 附带解决的两个开放问题

* **`t2v-fast` 的 16:9 可用**：提交侧亲传 `aspect_ratio=16:9`，出片元数据
  `width=1280, height=720`（正是 16:9）⇒ 比例**确实生效**，
  此前 notes 里"仅实抓 4:3"只是抓包当时选的那个值。
* **视频链路此前是坏的**：`t2v-fast` / `t2v-pro` **不在 `Service._submit` 的任何分支里**，
  掉进后编辑族 `else` ⇒ `assert cap.jimeng_tool` 炸，还被兜底逻辑报成
  "上游不可用·**可重试**"。修法：`models.T2V_VARIANTS` 共用一条路径 +
  `CapabilityNotWiredError`（500·不可重试）+ `SUBMIT_ROUTES` 静态对照门禁。

### 产物形态（视频）

任务表 `images` 列里的视频元素：
`{url, vid, width, height, format, item_id}`（`parse_task` 尽力而为解析，
2026-09-23 首次实读到完整形态）。

## 16. 视频模型面板全量侦察（2026-09-24，UI 实读零成本）

方法：bsk 驱动已登录网页端，`/ai-tool/generate/?type=video` 打开"选择模型"面板，
逐个点选模型后读 `localStorage.dreamina__generator_video_modelKey`
（**页面自己写的当前模型 key**，不是猜测）；档位来自各模型的参数弹层
（radiogroup 实读）。**未点生成、零积分消耗**。

### 面板名 → 上游 key → 火山方舟 Model ID 对照

| 即梦面板名（2026-09-24 UI） | 上游 key（UI 实读） | 档位（UI 实读） | 方舟 Model ID（官方文档） | 本服务 |
|---|---|---|---|---|
| 即梦 Seedance 2.5 | `dreamina_seedance_45_pro` | 比例 6 档；**480P/720P/1080P**；张数 1–4 | `doubao-seedance-2-5-260628`（480p/720p/1080p，4~30s） | ❌ 未登记 |
| 即梦 Seedance 2.5 (样片模式) | `dreamina_seedance_45_pro_draft` | 固定 480P 样片，确认后出高清正片；张数 1–4 | （方舟无样片概念） | ❌ 未登记 |
| 即梦 Seedance 2.0 VIP | `dreamina_seedance_40_pro_vision` | 720P（同 t2v-pro） | `doubao-seedance-2-0-260128`? | ✅ `jimeng-t2v-pro`（实扣 70） |
| 即梦 Seedance 2.0 Fast VIP | `dreamina_seedance_40_vision` | 720P（同 t2v-fast） | `doubao-seedance-2-0-fast-260128`? | ✅ `jimeng-t2v-fast`（实扣 30） |
| 即梦 Seedance 2.0 mini | `dreamina_seedance_40_mini` | 720P × 4s/5s（实跑对账） | `doubao-seedance-2-0-mini-260615` | ✅ `jimeng-t2v`（实扣 24） |
| 即梦 Seedance 2.0 | `dreamina_seedance_40_pro` | 比例 6 档；仅 720P | （疑对齐方舟 2.0 标准档） | ❌ 未登记 |
| 即梦 Seedance 2.0 Fast | `dreamina_seedance_40` | 比例 6 档；仅 720P | （疑对齐方舟 2.0 fast） | ❌ 未登记 |
| 即梦 Seedance 1.0 / 1.0 Fast | 未抓到（面板需滚动） | — | `doubao-seedance-1-0-pro-250528` / `-pro-fast-251015` | ❌ 未登记 |

### 三个结论

1. **UI 改版重命名（key 未变）**：9-20 抓包时的"Seedance 4.0 Vision / Pro Vision"
   现在面板显示为"**Seedance 2.0 Fast VIP / VIP**"，上游 key 仍是
   `40_vision` / `40_pro_vision` —— 本服务 t2v-fast / t2v-pro 登记继续有效，
   文档里的"网页端叫 4.0 Vision"要读成旧 UI 名。
2. **"VIP"是即梦会员通道概念，方舟没有对应档**：面板上非 VIP 的
   "2.0 Fast"(`40`) / "2.0"(`40_pro`) 是普通通道；与本服务已登记 key 不同。
3. **对照列带 `?` 的是命名推断**（按方舟档位规则对齐），不是同key实证；
   方舟按 token 计费、即梦按积分计费，两边计费体系独立。

### 待办（要接 2.5 的前置）

- [x] 2.5 样片提交包补抓（2026-09-24：`aigc_draft/generate` 全量 payload，
      benefit_type=`seedance_25_draft_480p_no_input_video_output`、预扣 amount=5）
- [x] 真跑对账计费（2026-09-24 两次：网页端 + 适配器端到端，**均实扣 45**，
      余额 5831→5786→5741，submit_id 三证吻合）
- [ ] 1.0 / 1.0 Fast 的 key（面板滚动后实读）
- [ ] `dreamina_seedance_45_pro`（2.5 正式版）提交包补抓 —— UI 实读
      480p/720p/1080p 三档（720p 预显 100 积分），benefit_type 未知未登记

### 适配器落地（2026-09-24）

* 新能力 **`jimeng-t2v-2.5-draft`**（`dreamina_seedance_45_pro_draft`，credits_measured=45）：
  480p × 5s 单档；端到端真跑 156s 出片 success。
* 提交差异（vs t2v）：`min_version "3.3.28"`、
  `min_features ["AIGC_Video_Seedance25ResultAction"]`、
  `video_gen_inputs[0].is_draft_mode: true`、metrics `videoStage:"draft"`。
* `VIDEO_RESOLUTIONS` 增 `"480p"`（实抓小写）。样片产物 = 480P 低清版，
  网页端"确认升级高清正片"流程本服务未适配。
* 探针：`scripts/probe_t2v_25_draft.py`（**计费动作**，跑一次扣 45）。

## 17. 图片美学模型 V8.2 / mj82（2026-10-02）

**上游 key** `jm_image_model_yc_mj82`，面板名 **图片美学模型 V8.2**
（能力表 `model_name` 逐字照抄）。用户 2026-10-02 UI 抓包带入。

### 17.1 提交包与 Seedream 系**完全同构**
同为 `image_base_component` + `abilities.generate.core_param` +组件级
`gen_option` ⇒ **不需要新的提交流程**，登记进 `UPSTREAM_MODEL_KEYS` 即走现成 t2i 分支。
命名风格完全不同（`jm_image_model_*`，非 `high_aes_general_*`），
但**结构相同** —— 别被命名骗以为要新写一套。

### 17.2 🔴 张数**不可控**：按恒 4 张设计与告知

服务端能力表实读：`generate_count_options=[4]`、`default_generate_count=4`。

**七发真跑的实测表**（`count` = 我们要的，回执草稿值 = 上游实际采用值）：

| 我们提交 | 回执草稿 `gen_count` | 实际出图（四字段一致） |
|---|---|---|
| 1 | 1 | **1** |
| 1 | 1 | **1** |
| 2 | **4** | 4 |
| 3 | **4** | 4 |
| 4 | 4 | 4 |
| 1 | **4** | 4 |
| 1 | **4** | 4 |

⇒ **同样传 1，有时出 1、有时出 4。** 前两发出 1 是因为我把
`(1,2,3,4)` 喂给 `count_options`，`resolve_count` 于是原样透传 1、上游照办；
后两发改传 `(1,)` 后**仍被抬成 4**。
**结论：mj82 的张数实际不可控**，`gen_count` 被上游按自己的 `[4]` 归一。
只有在"绕过吸附且恰好落 1"时才可能出 1 张，**这条路径不可依赖**
（不稳定，且计费随之变化：出 1 张扣 7、出 4 张扣 28）。
⇒ 按**恒 4 张**设计与告知调用方。

#### 两个都叫"张数"的字段，只有一个能用来判产物
| 字段 | 语义 | 能否判出图数 |
|---|---|---|
| `abilities.gen_option.gen_count`（草稿） | **实际执行张数** | ✅ 唯一可信 |
| `metrics_extra.generateCount` | 提交时的**请求/埋点**计数 | ❌ 恒为 1 |

🔴 用户 2026-10-02 的 UI 抓包正是这一机制的**直接证据**：
草稿 `gen_count=4` 而 `metrics_extra.generateCount=1` —— 埋点记 1、实际出 4。
⚠️ 这也复现了本项目早就踩过的坑（`client.py` 注释里写着
"`metrics_extra.generateCount` 只是埋点计数、实测它写 1 也照样出 4 张"）——
**同一个机制在 mj82 上再现**，别以为换个模型就换个规律。

#### 判"出图几张"必须四字段交叉核对
`item_list` 长度 / 各 item 的 `large_images` 之和 / `total_image_count` /
`finished_image_count`。四者一致才认。
（`parse_task` 每个 item 只取 `large_images[0]`，所以它**不会多报**；
但交叉核对能排除单一字段异常 —— 这条探针就是靠它才没被"1 张"骗过去。）

⚠️ **别把 `(1,2,3,4)` 喂给 `count_options`**：会让 `resolve_count`
原样透传 2/3 然后被上游静默改写成 4 ⇒ 我们报 n=3、实际出 4 张、按 4 张计费。
要 n 张就只传 n，其余靠 `service.create` 的 `len(declared)==1` 留痕告知。

### 17.3 计费：**分辨率 × 张数**（已解，2026-10-02）
消耗记录按 `submit_id` 对账：

| 分辨率档 | 张数 | 实扣 | 每张 |
|---|---|---|---|
| 1k | 4 | **20** | **5** |
| 2k | 4 | **28** | **7** |
| 1k | 1 | **7** | 7 |

⇒ 规则 = **每张单价随分辨率档走**（1k 5、2k 7），
而**张数下限是7**（只出 1 张也按7 扣，不是 5）
⇒ 单张时"1k 的 5/张"用不上，实际付7。
🔴 对账时**别用"张数 × 单价"线性估算**，1 张那档就是反例。

### 17.4 🔴 分辨率：服务端默认 **1k**，我们此前**恒发 2k**（真 bug，已修）

服务端 `default_resolution_type`（2026-10-02 实读）：

| 模型 | default | 可用档位 |
|---|---|---|
| **mj82** | **1k** | 1k / 2k |
| Seedream 5.0 Flash | 2k | 1.5k / 2k |
| Seedream 5.0 Pro | 2k | 1.5k / 2k / 4k |
| Seedream 5.0 Lite / 4.x | None | 2k / 4k |

⚠️ Lite / 4.x 的 `default` 是 `None`（**读不到 ≠ 没有默认**），
不能据此断言它们"没有默认档"。

mj82 的 `resolution_map`：1k 有 7 个比例（1024²/768×1024/1024×576…）、
2k 同 7 个（2048²/1728×2304/2560×1440…）。

#### 🔴 我们代码的 bug：`resolution_type` 硬编码 `"2k"`
`build_draft` / `build_blend_draft` 的该参数默认值是 `"2k"`，
而 `submit()` / `blend()` **根本不暴露它** ⇒
**调用方传 `size=1024x1024` 也按 2k 提交**。
症状：草稿里 `large_image_info` 是 `1024x1024 / resolution_type='2k'`
（**两个字段自相矛盾**），产物真出 **2048²**、按 2k 档**多扣钱**。

✅ **实测证据**（`submit_id=d4aa1a72-e709-491a-b6ed-281e433f04f8`）：
显式 `resolution_type="1k"` + 1024×1024 ⇒ 产物真出 **1024²**、实扣 **20**（5/张）
⇒ 该字段**确实生效**，此前是我们没传对。

✅ **已修**：`resolution_type` 改为**按 size 最长边就近吸附**
（`resolution_type_for_size`），显式传值时以传入的为准。
t2i 与 blend **两条路径一起修**（blend 是同一 bug 的第二处现场）。
门禁 `test_resolution_type_follows_size_instead_of_hardcoded_2k`。

### 17.5 ✅ 支持图生图（byte_edit）—— **已端到端真跑**
`feats` 含 `t2i` + `byte_edit` + `simple_image` + `per_piece` + `refuse_image`；
`input_image_limit=[{"max_image_num":20,"ability_name":"byte_edit"}]`。

**✅ 2026-10-02 真跑通过**（`submit_id=64a86c5c-c8b4-4ec0-b701-f30cec805aa4`）：
走**生产同一条路**（下载垫图 → ImageX 上传成 `image_uri` →
`blend(image_uris=[uri], model=mj82)`）——
· 提交包 `model` 回读 = `jm_image_model_yc_mj82`（**不是**默认 Lite）
  ⇒ 上游真的按 mj82 跑了 byte_edit，不只是我们把参数传下去；
· `gen_option.gen_count` = 4（`n=1` 被吸附成 4，告警如实）；
· 终态 `status=50`，用时 200s，出图 **4 张**（2048²png），**四字段一致**；
· 消耗记录 `amount=28`（4 张 2k = 7/张），`submit_id` 精确对上，余额 5594→5566。

⚠️ **别把"接线通"当"能用"**：2026-10-02 首版探针的 i2i 用例**被跳过**了
（依赖的产物没取到），当时只有"代码路径通"的证据。
`blend()` 有 `model` 参数 ≠ 上游认这个模型 —— 只有真跑能回答。
已补门禁 `test_mj82_blend_was_proven_end_to_end_not_just_wired`。

⚠️ 调用坑：`blend(source_from默认 "upload")` **必须给 `image_uri(s)`**，
直接传 `image_url` 会报 `必须给 image_uri`（`source_from="link"` 是**另一条**
路径，不走它以免测的不是生产链路）。

⚠️ 2026-10-02 同时修的接线缺口：**原先 `_submit` 的 i2i 分支不传模型**、
`resolve` 也只在 t2i 族返回上游模型 ⇒ 带图 + `model=mj82` 会撞
"t2i 不接受输入图"的 400。现已连能力带模型一起换。
是否支持 blend **只认服务端 `feats`**（`upstream_supports_blend`），
读不到 ⇒ 保守拒绝（"读得到 ≠ 用得了"的反向纪律）。

### 17.6 别名键必须与归一化输出**逐字一致**（踩坑记录）
`_norm_model_name` 会小写并把 `.`/空格换成 `-`：
`mj-v8.2` → `mj-v8-2`（**v 保留**）、`图片美学模型 V8.2` → `图片美学模型-v8-2`。
🔴 我曾把键写成 `mj-8-2`（m/j 顺序颠倒）⇒ **永远查不中**，
而既有门禁 `test_every_web_alias_maps_to_a_registered_upstream_model`
**抓不到**（它遍历表内键 —— 错的键自己撞自己仍"通过"）。
已补 `test_every_alias_key_is_in_normalised_form` 把这类沉默失配在导入期照出来。

## 18. 方舟图片模型名 → 即梦上游 key（2026-10-02）

`UPSTREAM_ARK_NAMES`（上游 key → 方舟名），**当前 3 条**：

| 即梦上游 key | 面板名 | 方舟模型名 | 实测单价 |
|---|---|---|---|
| `high_aes_general_v50p_large` | Seedream 5.0 Pro | `doubao-seedream-5-0-pro-260628` | 8/张 |
| `high_aes_general_v50_flash` | Seedream 5.0 Flash | `doubao-seedream-5-0-flash-260915` | 3/张 |
| `high_aes_general_v50` | Seedream 5.0 Lite | `doubao-seedream-5-0-260128` | 0 |

三条均由**用户点名指定**（不是我们从能力表推的）。
机制：别名表由本表**反向派生**（`{_norm(方舟名): 上游key}`）——
不手写字面量，两张表漂移时门禁立刻红。

### 🔴 结构性约束：一个方舟名只对一个上游 key
`v4*` 族有 **5 个** key（`v40`/`v40l`/`v41`/`v42`/`v43` = 4.0/4.5/4.1/4.6/4.7）。
若图省事把同一个方舟名指给整族，反向派生的 dict 推导会让
**后者覆盖前者** ⇒ 另外 4 个模型**静默失去**可用的方舟名，
而 `/v1/models` 里它们的 `ark_name` 还都显示着那个名字，**看着完全正常**。
⇒ 门禁 `test_ark_name_is_one_to_one_and_never_collapses`在登记时就拦。
⚠️ 因此 4.x 族目前**一个方舟名都没登记** —— 需要的话请给**每个档位**分别的
官方模型名，不要给通配。

### 未登记名的处置（刻意保留）
`doubao-seedream-4-5-251128` 等**未登记**的方舟名仍按**占位名**处理 ⇒
落回默认 Lite（0）。理由：认一个没对账过的名字 = 替调用方悄悄切到
收费档（Pro 8/张）——**降级只给错图，升级要扣钱**。
门禁 `test_unregistered_ark_names_stay_placeholders_...`（2026-10-02
把样本从 pro 换成 4.5 那条，保护机制本身一条没少）。
