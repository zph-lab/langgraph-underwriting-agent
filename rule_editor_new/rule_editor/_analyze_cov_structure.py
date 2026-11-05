# 分析 060032险别列表 对象的结构（用后删除）
import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from rule_editor.etl.bom_catalog import BomCatalog
c = BomCatalog("data/bom_catalog.sqlite")
c.connect()
c.build_from_db()

# 看 appbase.productLifeInfo06.coverage060032List 这个对象
obj = c.object_by_path.get("appbase.productLifeInfo06.coverage060032List")
print("=== coverage060032List 对象 ===")
print("label:", obj.get("label"), "| type:", obj.get("type"), "| classPath:", obj.get("class_path"))

# 看它下面有没有子对象（列表元素）
print("\n=== coverage060032List 的直接子对象 ===")
subs = c.direct_child_objects("appbase.productLifeInfo06.coverage060032List")
print("子对象数:", len(subs))
for o in subs[:5]:
    print("  ", o.get("label"), "|", o.get("field_path"), "| classPath:", o.get("class_path"))

# 看它下面有没有直接字段
print("\n=== coverage060032List 的直接字段 ===")
flds = c.direct_child_fields("appbase.productLifeInfo06.coverage060032List")
print("直接字段数:", len(flds))
for f in flds[:10]:
    print("  ", f.get("label"), "|", f.get("field_path"), "| classPath:", f.get("class_path"))

# 找 Coverage060032 的字段（学生类别 cresvtxt9）
print("\n=== classPath=Coverage060032 的字段 ===")
n = 0
for f in c.field_by_path.values():
    if "Coverage060032" in (f.get("class_path") or "") and n < 5:
        print("  ", f.get("label"), "|", f.get("field_path"))
        n += 1
c.close()
