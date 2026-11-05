# -*- coding: utf-8 -*-
"""本地生成器 vs 接口返回 字段对照（批量回归）。

用法:
    python rule_test/_compare.py [--input rule_test/_inputs.jsonl] [--limit 0]
                                [--detail] [--max-detail 8]

只统计 success=True（或 resp_input 非 None）的接口存档记录；每条规则按类型输出
本地生成(gen) 与接口(api) 的叶子路径差异：
    接口有本地缺(miss)  = 生成器漏收集的真实字段
    本地有接口缺(extra) = 生成器多收集的伪字段
路径清理规则与 input_generator 口径一致：剥 left/right 相对树位前缀、
剥 YAICNonVehicleBom* 重复包裹前缀。
末尾输出按类型聚合汇总。
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rule_test.input_generator import build_input, load_sample


def walk_leaves(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(walk_leaves(v, prefix + "." + k if prefix else k))
    else:
        out.append((prefix, obj))
    return out


def clean(paths):
    """接口噪音键：left/right 相对树位、bom 前缀重复 —— 比较时剥掉。"""
    out = []
    for p in paths:
        seg = p.split(".")
        if seg[0] in ("left", "right"):
            continue
        if seg[0].startswith("YAICNonVehicleBom"):
            continue
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="rule_test/_inputs.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="最多跑 N 条（0=全部）")
    ap.add_argument("--detail", action="store_true", help="逐条打印差异明细")
    ap.add_argument("--max-detail", type=int, default=10, help="每条最多列出的 miss/extra 路径数")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print("存档不存在:", args.input, file=sys.stderr)
        return 1
    recs = [json.loads(x) for x in open(args.input, encoding="utf-8")
            if x.strip()]
    ok = [r for r in recs
          if r.get("success") is not False and r.get("resp_input") is not None]
    if args.limit:
        ok = ok[:args.limit]
    print("存档共 %d 条，成功 %d 条（本次对照跑 %d 条）" % (len(recs), len(ok), len(ok)))

    stat_miss = Counter()   # type -> 有 miss 的规则数（非漂移）
    stat_extra = Counter()  # type -> 有 extra 的规则数（非漂移）
    tot_miss = Counter()    # type -> miss 路径总数
    tot_extra = Counter()   # type -> extra 路径总数
    by_type = Counter()
    drift = []              # (rid, type, name, 现象)
    not_found = []

    def report(flag, rid, typ, name, miss, extra):
        print("=" * 66)
        print("%s %s | %s | type=%s then=%s" % (flag, rid, name, typ,
                                                r.get("has_then")))
        if miss:
            print("  接口有本地缺(%d):" % len(miss))
            for p in miss[:args.max_detail]:
                print("    -", p)
            if len(miss) > args.max_detail:
                print("    …等共 %d 条" % len(miss))
        if extra:
            print("  本地有接口缺(%d):" % len(extra))
            for p in extra[:args.max_detail]:
                print("    +", p)
            if len(extra) > args.max_detail:
                print("    …等共 %d 条" % len(extra))
        if not miss and not extra:
            print("  字段一致")

    for r in ok:
        rid = r["rule_id"]
        typ = r.get("type") or "?"
        rc, pd, row = load_sample("", rid)
        if rc is None:
            not_found.append((rid, typ, r.get("rule_name") or ""))
            continue
        gen = build_input(rc, pd or "", (row or {}).get("then_else"))
        api = r.get("resp_input") or {}
        gl = sorted(p for p, _ in walk_leaves(gen))
        al = sorted(p for p, _ in walk_leaves(api))
        gc, ac = set(clean(gl)), set(clean(al))
        miss = sorted(ac - gc)    # 接口有、本地无
        extra = sorted(gc - ac)   # 本地有、接口无
        by_type[typ] += 1
        # 疑似服务端漂移：本地快照与接口字段集合不相交/一侧为空另一侧非空
        # （说明该 ruleId 在 ADP 上已是另一套规则内容，与本地快照无关）
        drift_like = False
        if gc and ac:
            drift_like = not (gc & ac)
        elif gc or ac:
            drift_like = True
        if drift_like:
            drift.append((rid, typ, r.get("rule_name") or "",
                          "gen%d条 ↔ api%d条，无交集" % (len(gc), len(ac))))
            if args.detail:
                report("DRIFT?", rid, typ, r.get("rule_name") or "", miss, extra)
            continue
        if miss:
            stat_miss[typ] += 1
            tot_miss[typ] += len(miss)
        if extra:
            stat_extra[typ] += 1
            tot_extra[typ] += len(extra)
        if args.detail or (miss or extra):
            flag = "OK " if not miss and not extra else "DIFF"
            report(flag, rid, typ, r.get("rule_name") or "", miss, extra)

    print("=" * 66)
    print("本地样本未找到（无法对照）: %d 条" % len(not_found))
    for rid, typ, name in not_found[:10]:
        print("  ", rid, typ, name)
    if drift:
        print("疑似服务端漂移（ruleId 已不是本地快照内容）: %d 条" % len(drift))
        for rid, typ, name, why in drift[:args.max_detail]:
            print("  ", rid, "|", typ, "|", why)
        if len(drift) > args.max_detail:
            print("  …等共 %d 条" % len(drift))
    print("-" * 66)
    n_cmp = sum(by_type.values())
    print("按类型汇总（口径对照 %d 条，不含疑似漂移 %d 条）:" % (n_cmp, len(drift)))
    for typ in sorted(by_type):
        n = by_type[typ]
        if not n:
            continue
        print("  %-10s 共%-3d | 接口有本地缺:%-3d 条(路径%-4d) | 本地多收集:%-3d 条(路径%-4d)" % (
            typ, n, stat_miss[typ], tot_miss[typ], stat_extra[typ], tot_extra[typ]))
    n_miss = sum(stat_miss.values())
    n_extra = sum(stat_extra.values())
    print("合计: 存在接口有本地缺的规则 %d 条，存在本地多收集的规则 %d 条" % (n_miss, n_extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
