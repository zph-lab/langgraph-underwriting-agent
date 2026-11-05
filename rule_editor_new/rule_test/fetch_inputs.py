# -*- coding: utf-8 -*-
"""从 ADP /test/getInputByRuleId 抓取样本规则的 input 骨架（存档研究用）。

用法:
    python rule_test/fetch_inputs.py --token <TOKEN> [--limit 120]
        [--n-simple 15] [--n-deep 20] [--n-pre 10] [--n-method 15]
        [--n-then 20] [--dyn-per-domain 6] [--seed 42] [--append]
        [--out rule_test/_inputs.jsonl]

输出每行: {rule_id, rule_name, rule_content, resp_input, api_raw, success, ...}
rule_content = 本地 samples 行的 if_rule.ruleContent（dict 已解析）。

扩样策略（默认合计 ~96 条，覆盖比 fetch_inputs 旧版 16 条宽得多）：
    1. 动态域优先：先选 then/else 参数引用 dynamicsAttributeList.<域>（非 Helper）
       的规则，每个域最多 --dyn-per-domain 条（CUWReturnType/FeeType 等），
       验证 then/else 动态域枚举值 → input 骨架的收集。
    2. then/else 富样本：再补 --n-then 条 then/else 非空的规则。
    3. 按复杂度配额：method / predefine / deep_logic / simple 各补足。
同一 rule_id 去重；样本随机顺序（--seed 可复现）。

--append：保留输出文件已有记录（按 rule_id 去重），失败后可续跑。
"""
import argparse
import glob
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://192.168.0.205:8902"


def fetch_input(rule_id, token):
    """POST /test/getInputByRuleId?ruleId=... → (resp.input dict, api_raw json)。
    失败时 resp 返回 None、api_raw 为错误信息 dict。"""
    url = BASE + "/test/getInputByRuleId"
    headers = {
        "adp-token": token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Origin": BASE,
        "Referer": BASE + "/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode({"ruleId": rule_id}).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        j = json.loads(body)
        if not j.get("success"):
            return None, {"errCode": j.get("errCode"), "errMsg": j.get("errMsg")}
        return j.get("resp") or {}, j
    except urllib.error.HTTPError as e:
        return None, {"http": e.code, "body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return None, {"exc": str(e)}


def rule_complexity(rc):
    """粗略按 ruleContent 大小/是否含 method/嵌套层数标记类型。"""
    if not isinstance(rc, dict):
        return "empty"
    s = json.dumps(rc, ensure_ascii=False)
    has_method = "METHOD" in s or '"methods"' in s or "methodName" in s
    has_rc_pre = "规则预定义变量" in s
    depth = 0
    node = rc
    while isinstance(node, dict) and ("left" in node or "right" in node):
        nxt = node.get("left")
        if not isinstance(nxt, dict):
            nxt = node.get("right")
        if nxt == node:
            break
        depth += 1
        node = nxt
    if has_method:
        return "method"
    if has_rc_pre:
        return "predefine"
    if depth >= 3:
        return "deep_logic"
    return "simple"


_DYN_RE = __import__("re").compile(r"dynamicsAttributeList\.([A-Za-z0-9_]+)")


def row_meta(row, rid, rc):
    """提取行特征：复杂度类型 / then/else 是否非空 / then/else 引用的动态域集合 / predefine。"""
    te = row.get("then_else") or {}
    items = (te.get("thenList") or []) + (te.get("elseList") or [])
    doms = set()
    for it in items:
        for d in ((it.get("fieldParsings") or {}), (it.get("paramValue") or {})):
            for v in d.values():
                if isinstance(v, str):
                    for mm in _DYN_RE.finditer(v):
                        if mm.group(1) != "Helper":
                            doms.add(mm.group(1))
    return {
        "rid": rid,
        "type": rule_complexity(rc),
        "has_then": bool(items),
        "doms": doms,
        "has_pd": bool(row.get("predefine")),
    }


def scan_pool(glob_expr):
    """全量扫描样本，收集去重后的轻量特征池（不保留 ruleContent，省内存）。"""
    pool = {}
    files = sorted(glob.glob(glob_expr))
    if not files:
        return pool, []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    rid = row.get("rule_id") or (row.get("rule_node") or {}).get("ruleId") or ""
                    if not rid or rid in pool:
                        continue
                    rc_raw = ((row.get("if_rule") or {}).get("ruleContent")) or ""
                    try:
                        rc = json.loads(rc_raw) if isinstance(rc_raw, str) else rc_raw
                    except Exception:
                        rc = None
                    meta = row_meta(row, rid, rc)
                    meta["file"] = fp
                    pool[rid] = meta
        except FileNotFoundError:
            continue
    return pool, files


def pick(metas, n):
    """从已随机化序列按顺序取 n 个未选条目。"""
    return metas[:n]


def select(metas, args):
    """分层选样：动态域 → then/else 富样本 → 各复杂度配额。返回选中的 meta 列表。"""
    rng = random.Random(args.seed)
    order = list(metas)
    rng.shuffle(order)
    chosen = []
    chosen_ids = set()

    def take(it):
        for m in it:
            if m["rid"] not in chosen_ids:
                chosen.append(m)
                chosen_ids.add(m["rid"])
                yield m

    # 1) 动态域优先：每域最多 dyn_per_domain 条（多域的规则优先）。
    dyn_pool = [m for m in order if m["doms"]]
    rng.shuffle(dyn_pool)
    dyn_count = {}
    for m in dyn_pool:
        if all(dyn_count.get(d, 0) < args.dyn_per_domain for d in m["doms"]):
            for d in m["doms"]:
                dyn_count[d] = dyn_count.get(d, 0) + 1
            list(take([m]))
            if args.limit and len(chosen) >= args.limit:
                return chosen
    # 2) then/else 非空的富样本
    for m in take([x for x in order if x["has_then"]]):
        if len(chosen) >= args.n_then:
            break
    if args.limit and len(chosen) >= args.limit:
        return chosen
    # 3) 各复杂度配额补齐
    for typ, n in (("method", args.n_method), ("predefine", args.n_pre),
                   ("deep_logic", args.n_deep), ("simple", args.n_simple)):
        cnt = 0
        for m in take([x for x in order if x["type"] == typ]):
            cnt += 1
            if cnt >= n or (args.limit and len(chosen) >= args.limit):
                break
    return chosen


def load_rows_by_ids(glob_expr, chosen_ids):
    """第二次扫描样本，只取被选中 rule_id 的完整行（rid → row）。"""
    rows = {}
    files = sorted(glob.glob(glob_expr))
    if not files:
        return rows
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    rid = row.get("rule_id") or (row.get("rule_node") or {}).get("ruleId") or ""
                    if rid in chosen_ids and rid not in rows:
                        rows[rid] = row
        except FileNotFoundError:
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--limit", type=int, default=0,
                    help="抓取总上限（0=不设限，默认按配额 ~96）")
    ap.add_argument("--out", default="rule_test/_inputs.jsonl")
    ap.add_argument("--append", action="store_true", help="保留输出文件已有记录并按 rule_id 去重")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-simple", type=int, default=15)
    ap.add_argument("--n-deep", type=int, default=20)
    ap.add_argument("--n-pre", type=int, default=10)
    ap.add_argument("--n-method", type=int, default=15)
    ap.add_argument("--n-then", type=int, default=20)
    ap.add_argument("--dyn-per-domain", type=int, default=6)
    args = ap.parse_args()

    token = args.token.strip() or os.environ.get("ADP_TOKEN", "")
    if not token:
        print("需要 --token 或环境变量 ADP_TOKEN", file=sys.stderr)
        return

    # glob 缺省用 config 的 samples_dir
    if not args.glob:
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from rule_editor.config import load_config, get
            load_config()
            sd = get("data", "samples_dir", "") or ""
        except Exception:
            sd = ""
        if sd:
            args.glob = os.path.join(sd, "*_samples.jsonl")
        else:
            args.glob = "非车销售/*_samples.jsonl"
    print("扫描 glob:", args.glob)

    pool, files = scan_pool(args.glob)
    metas = list(pool.values())
    print("本地候选（去重 rule_id）:", len(metas))
    picked = select(metas, args)
    print("选中候选:", len(picked))
    from collections import Counter as _C
    print("类型分布:", dict(_C(m["type"] for m in picked)),
          "| then/else 非空:", sum(1 for m in picked if m["has_then"]),
          "| 动态域覆盖:", sorted({d for m in picked for d in m["doms"]}))
    for m in picked:
        print("  ", m["type"], m["rid"], "then:", m["has_then"],
              "doms:", ",".join(sorted(m["doms"])) or "-")

    chosen_ids = {m["rid"] for m in picked}
    rows = load_rows_by_ids(args.glob, chosen_ids)

    # 已有输出去重（--append）
    existing = {}
    if args.append and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                existing[rec.get("rule_id") or ""] = rec
    to_fetch = [m for m in picked if m["rid"] not in existing]
    if to_fetch and existing:
        print("已有存档 %d 条，本次待抓 %d 条" % (len(existing), len(to_fetch)))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    n_ok = 0
    # 注意：append 模式只追加本次新增（已有记录保留在文件里，绝不重写）；
    # 否则会把已有记录原样再写一遍造成重复（曾踩过：16 条变 32 条）。
    with open(args.out, "a" if args.append else "w", encoding="utf-8") as out:
        for m in to_fetch:
            if args.limit and (len(existing) + n_ok) >= args.limit:
                break
            row = rows.get(m["rid"])
            rid, rname = m["rid"], ""
            rc = None
            if row:
                rname = (row.get("rule_node") or {}).get("ruleName") or ""
                rc_raw = ((row.get("if_rule") or {}).get("ruleContent")) or ""
                try:
                    rc = json.loads(rc_raw) if isinstance(rc_raw, str) else rc_raw
                except Exception:
                    rc = None
            resp, raw = fetch_input(rid, token)
            rec = {
                "rule_id": rid,
                "rule_name": rname,
                "type": m["type"],
                "has_then": m["has_then"],
                "doms": sorted(m["doms"]),
                "has_pd": m["has_pd"],
                "rule_content": rc,
                "resp_input": (resp or {}).get("input"),
                "api_raw": raw,
                "success": resp is not None,
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            if resp is not None:
                n_ok += 1
                print("[ok]", rid, rname)
            else:
                print("[fail]", rid, raw)
            time.sleep(0.2)
    print("完成，本次成功", n_ok, "/", len(to_fetch), "→", args.out,
          "（累计", len(existing) + n_ok, "条成功）")


if __name__ == "__main__":
    main()
