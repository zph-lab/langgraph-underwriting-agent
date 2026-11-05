# 验证 060032 在 bom 里是否存在（用后删除）
import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from rule_editor.etl.bom_catalog import BomCatalog
c = BomCatalog("data/bom_catalog.sqlite")
c.connect()
c.build_from_db()

# 搜 060032 相关对象/字段
print("=== 搜 '060032' 相关对象 ===")
n = 0
for o in c.object_by_path.values():
    if "060032" in (o.get("label") or "") or "060032" in (o.get("field_path") or ""):
        n += 1
        if n <= 5:
            print("  obj:", o.get("label"), "|", o.get("field_path"))
print("对象命中:", n)

print("\n=== 搜 '学生平安' 相关 ===")
n = 0
for o in c.object_by_path.values():
    if "学生平安" in (o.get("label") or ""):
        n += 1
        if n <= 5:
            print("  obj:", o.get("label"), "|", o.get("field_path"))
for f in c.field_by_path.values():
    if "学生平安" in (f.get("label") or ""):
        n += 1
        if n <= 8:
            print("  field:", f.get("label"), "|", f.get("field_path"))
print("合计命中:", n)

print("\n=== 搜 '人身险信息' 下的险别列表 ===")
for o in c.object_by_path.values():
    if "险别列表" in (o.get("label") or "") and "060032" in (o.get("label") or ""):
        print("  obj:", o.get("label"), "|", o.get("field_path"))
c.close()
