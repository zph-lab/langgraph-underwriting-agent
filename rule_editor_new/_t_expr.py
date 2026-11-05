# -*- coding: utf-8 -*-
"""探测：表达式预定义各光标位置的候选行为"""
import io, sys, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = "http://127.0.0.1:8009/api"

L = ("指定'纯风险保费'为( '当前险别'的保额/限额(元) / 500000 - 4 ) "
     "* ( '保单'的临时信息的险别保费计算的三者险基准纯风险保费A "
     "- '保单'的临时信息的险别保费计算的三者险基准纯风险保费B )")

# 关键光标锚点：( 后、'当前险别 中、'当前险别'的、保额、/ 500000、- 、( '保单、保费A 后、- '保
anchors = [
    ("为( 后",            12),
    ("为( '当前",          16),
    ("为( '当前险别'的",    19),
    ("为( ...'的保额",      22),
    ("保额/限额(元) 完整",  L.index("500000") - 1),
    ("/ 500000 后",        L.index("- 4")),
    ("- 4 ) 后",           L.index("* (")),
    ("* ( '保单",          L.index("临时信息")),
    ("...保费A - ",        L.index("- '保单'")),
    ("- '保单'的临时",      L.index("险别保费计算的三者险基准纯风险保费B")),
]
for name, caret in anchors:
    r = requests.get(f"{BASE}/predefine_param_context",
                     params={"line": L, "caret": caret, "limit": 8}, timeout=30)
    d = r.json()
    cands = [c.get("label", "")[:38] for c in (d.get("candidates") or [])[:4]]
    print(f"[{name}] caret={caret} stage={d.get('stage')} pi={d.get('param_index')} "
          f"n={len(d.get('candidates') or [])}")
    for c in cands:
        print("   ", c)

# 简单场景：新起一行只有 (
for line, caret, tag in [
    ("指定'X'为(", 7, "仅左括号"),
    ("指定'X'为( ", 8, "左括号+空格"),
    ("指定'X'为( '", 9, "括号+单引号"),
    ("指定'X'为( 5", 9, "括号+数字"),
]:
    r = requests.get(f"{BASE}/predefine_param_context",
                     params={"line": line, "caret": caret, "limit": 8}, timeout=30)
    d = r.json()
    print(f"[{tag}] stage={d.get('stage')} n={len(d.get('candidates') or [])} "
          f"labels={[c.get('label','')[:20] for c in (d.get('candidates') or [])[:3]]}")
