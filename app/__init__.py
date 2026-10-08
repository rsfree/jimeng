#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jimeng-service —— 即梦（jimeng.jianying.com）的图片/视频生成出口。

对外契约（冻结，见 `docs/INTERFACE.md`）：
  POST /async/v1/images/generations        受理，只回一个 task_id
  GET  /async/v1/images/generations/{id}   非终态回排队态；终态回 {data, created, usage}
  POST /v1/images/generations              同步：创建+轮询合并（预算内直接回结果）
  GET  /v1/models                          能力清单（**只有这一条路径**；免鉴权）
"""

# 🔴 版本号**由 CI 自动管理**（push main ⇒ 自增 patch，见 CI 的 release job）。
# 2026-10-02 实测：手工 bump 0.2.0 时，CI 正好把 0.1.9→0.1.10，直接冲突
# （rebase 时两个提交都改了同一行）。⇒ **不要在这里手工改版本号**，
# 要发版就推 main，让 CI 改。
__version__ = "0.1.21"
