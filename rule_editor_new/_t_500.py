# -*- coding: utf-8 -*-
"""进程内复现服务器 500：predefine_param_context 同参直调，打印完整 traceback。"""
import traceback
from rule_editor.service import EditorService

line = "指定'123'为 动态域集的帮助帮助.取得对象0的字符串属性(开发人员使用)1() ( <double> , <double> )"
predefines = "预定义 " + line

svc = EditorService()
try:
    d = svc.predefine_param_context(line, 45, 200, predefines)
    print("stage:", d.get("stage"))
    cands = d.get("candidates") or []
    print("candidates:", len(cands))
    for c in cands[:5]:
        print("  ", {k: c.get(k) for k in ("label", "kind", "type", "template")})
except Exception:
    traceback.print_exc()

# 顺带跑标注链路（预览用的 /api/annotate 同口径）
try:
    w = svc.annotate(predefines, predefines)
    print("annotate OK, words:", len(w))
except Exception:
    traceback.print_exc()
