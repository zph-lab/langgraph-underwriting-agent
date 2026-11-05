# 分析 predefine 里"060032学生平安保险(2009版)B款"的来历（用后删除）
import sys, os, io, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from rule_editor.etl.value_dict import iter_sample_lines
d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "非车销售")

# 1. 收集所有 predefine.content，统计模式
predefines = []
for s in iter_sample_lines(d):
    pre = s.get("predefine")
    if pre and pre.get("content"):
        c = pre["content"]
        if c not in predefines:
            predefines.append(c)
    if len(predefines) >= 15:
        break

print(f"=== 不同的 predefine content（{len(predefines)} 种）===")
for c in predefines:
    print("  ", c[:120])
    print()
