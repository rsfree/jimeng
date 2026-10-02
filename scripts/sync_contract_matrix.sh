#!/bin/bash
# 判据：状态码 + 自家文案（不能是 nginx 504 / FastAPI 422 机器味）。
#
# 🔴 **这里是最容易出事的探针**：2026-10-02 实测「未知模型名」用例返回 200
# 并真实出图（`seedream-9-9-ultra` 被 `PLACEHOLDER_PREFIXES` 的 `seedream`
# 前缀吞掉 ⇒ 静默降级成默认 Lite）。**加新用例前先确认它落不进 `Service.create`
# 之后** —— 凡是「参数合法、能解析出能力」的请求都会真建任务。
# 自检：`python scripts/sync_form_matrix.py` 是另一条真跑链路，别混用。
BASE=${JM_BASE:-http://jimeng.1task.cn}
K=${JM_API_KEY:-$(/usr/bin/sed -n 's/^API_KEYS=//p' "$(dirname "$0")/../.env" 2>/dev/null | /usr/bin/cut -d, -f1 | /usr/bin/tr -d ' \"')}
[ -n "$K" ] || { echo "缺少 API Key：给 JM_API_KEY=<key> 或在仓库 .env 写 API_KEYS" >&2; exit 2; }
H_AUTH="Authorization: Bearer $K"
H_CT="Content-Type: application/json"
EP="$BASE/v1/images/generations"

probe() { # $1=标签 $2=期望码 $3..=body
  local label="$1" want="$2"; shift 2
  local out code body
  out=$(curl --noproxy '*' -s -m 30 -o /tmp/_b.json -w '%{http_code}' \
        -X POST "$@" "$EP")
  code="$out"
  body=$(python3 - <<'PY'
import json
try:
    d=json.load(open('/tmp/_b.json'))
except Exception:
    print(open('/tmp/_b.json').read()[:160]); raise SystemExit
e=d.get("error") or {}
print(f"code={e.get('code')} type={e.get('type')} msg={(e.get('message') or '')[:110]}")
PY
)
  if [ "$code" = "$want" ]; then printf '  ✅ %-34s %s  %s\n' "$label" "$code" "$body"
  else printf '  ❌ %-34s got=%s want=%s  %s\n' "$label" "$code" "$want" "$body"; fi
}

echo "── 鉴权层（期望 401） ─────────────────────────────"
probe "无 Authorization"       401 -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x"}'
probe "错 key"                 401 -H "Authorization: Bearer sk-wrong-key-000" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x"}'
probe "非 Bearer 前缀"         401 -H "Authorization: Token $K" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x"}'

echo "── 字段校验层（期望 400，且文案是自家的） ──────────"
probe "t2i 缺 prompt"          400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i"}'
# 🔴🔴 这三条是**未修缺陷的取证用例，不是零成本用例** ——
# `PLACEHOLDER_PREFIXES` 含 `seedream` 前缀，任何 `seedream-<不存在>` 都被
# `is_placeholder()` 判真 ⇒ 跳过别名解析 ⇒ 落默认 Lite ⇒ **真出图**。
# 2026-10-02 实测 `seedream-9-9-ultra` / `seedream-4-9` 均返回 200 并出图。
#
# ⇒ 默认**跳过**。修完 models.py（凡 `seedream*` 开头且不在两张表里的一律
# 400 + 给可用清单）之后，用 `--regress-seedream` 打开它们当门禁。
# 现状打开 = 每次白烧 3 张图。
# ✅ 2026-10-02 已修：不再改成 400（用户口径「不存在的型号用免费模型兜底出图
# 没啥问题」），而是**补留痕** —— 这类请求的 `degradations` 必须带一条说明。
# ⇒ 判据从「状态码 400」变成「200 且 degradations 非空」，仍会真出图（1 张/次），
#    所以**默认跳过**，要验留痕时用 REGRESS_SEEDREAM=1 打开。
if [ "$REGRESS_SEEDREAM" = "1" ]; then
  for m in seedream-9-9-ultra seedream-4-9 seedream-5-0; do
    out=$(curl --noproxy '*' -s -m 90 -X POST -H "$H_AUTH" -H "$H_CT" \
          -d "{\"model\":\"$m\",\"prompt\":\"兜底留痕门禁\",\"image\":[],\"n\":1}" \
          "$EP" 2>/dev/null)
    deg=$(printf '%s' "$out" | python3 -c 'import sys,json
try: print(len(json.load(sys.stdin).get("degradations") or []))
except Exception: print(-1)' 2>/dev/null)
    if [ "$deg" -ge 1 ] 2>/dev/null; then
      printf '  ✅ %-34s 留痕 %s 条（兜底已声明）\n' "未知型号 $m" "$deg"
    else
      printf '  ❌ %-34s degradations=%s —— 静默降级！\n' "未知型号 $m" "$deg"
    fi
  done
else
  echo "  ⏭️  兜底留痕门禁×3 已跳过（会真出图 1 张/次）"
  echo "      要验留痕：REGRESS_SEEDREAM=1 bash $0"
fi
probe "未登记 3.0（要给理由）"  400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"图片-3-0","prompt":"x"}'
probe "image 传字符串"          400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","image":"https://a/b.png"}'
probe "未知字段"                400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","foo":1}'
probe "视频字段给图片端点"      400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","duration":5}'
probe "带图但不指定 model"      400 -H "$H_AUTH" -H "$H_CT" -d '{"prompt":"x","image":["https://a/b.png"]}'
probe "n=0"                    400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","n":0}'
probe "n 非整数"                400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","n":"two"}'
probe "i2i 缺 prompt"          400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-i2i","image":["https://a/b.png"]}'
probe "hd 缺输入图"             400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-hd"}'
probe "detail-fix 无引用"        400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-detail-fix"}'
probe "i2i 垫图超上限(5张)"     400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-i2i","prompt":"x","image":["https://a/1.png","https://a/2.png","https://a/3.png","https://a/4.png","https://a/5.png"]}'
probe "size 不可解析"           400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-t2i","prompt":"x","size":"big"}'
probe "source_task_id 不存在"   400 -H "$H_AUTH" -H "$H_CT" -d '{"model":"jimeng-detail-fix","source_task_id":"jimeng_deadbeef"}'
