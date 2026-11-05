# -*- coding: utf-8 -*-
"""规则测试报文 input 骨架生成器。

输入一条规则的 ruleContent（if 条件树，samples 行 if_rule.ruleContent 即现成），
输出 /test/getInputByRuleId 形态的 input 骨架：字段按 field_path 逐层嵌套、叶子 null，
命中值由测试者手工填写。业务对象字段（appbase.*）与动态域集枚举字段
（dynamicsAttributeList.*）均收集，与 ADP 返回对齐便于对照。

预定义别名展开：规则中 `规则预定义变量.<别名>…` 引用 → 解析 predefine 行，
输出别名绑定列表/字段的真实 BOM 路径（如 指定'当前险别'为一个…来自(in) '保单'的人身险信息的…列表
→ appbase.productLifeInfo06.coverage060032List）。

依赖 rule_editor 的 BomCatalog（data/bom_catalog.sqlite），换 BOM 表改 config 即可复用。

用法:
    python rule_test/input_generator.py --rule-id YA_030080 \
        --sample-file 非车销售/100_samples.jsonl
    python rule_test/input_generator.py --rule-content '{"nodeType":...}' \
        --predefine "指定'当前险别'为一个…来自(in) '保单'的…列表;"
"""
import argparse
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 预定义行解析（与编辑器同款：列表/非列表两形态）。
# 样本 predefine content 常为"单行多条 指定'…'…; 指定'…'…;"，先按 ; 拆句；
# 句末可带"符合条件：…"过滤子句，路径取到该子句为止。
_LIST_RE = re.compile(
    r"^指定'([^']+)'为一个\s*(\S*)\s*来自\s*\(\s*in\s*\)\s*")
_FIELD_RE = re.compile(
    r"^指定'([^']+)'为\s*'([^']+)'的\s*")

# 规则预定义变量前缀（生成端 fieldPath 形态，与编辑器一致）
PD_PREFIX = "规则预定义变量."


def collect_field_paths(node, out):
    """递归遍历 ruleContent，收集引用的叶子路径：
    - FIELD 节点 → fieldPath（如 appbase.cfeeBsnsTyp / dynamicsAttributeList.…）
    - METHOD 节点 → methodPath 去 "()"（如 result.hasFeeRuleMatched），Helper 辅助方法除外
      （接口对照：Helper.isStringContainAnyofSubString 未进 input，result.hasFeeRuleMatched 进了）
    """
    if isinstance(node, dict):
        nt = node.get("nodeType")
        if nt == "FIELD" or node.get("dataType") == "field":
            fp = node.get("fieldPath") or node.get("field_path") or ""
            if fp:
                out.add(fp)
            return
        if nt == "METHOD" or node.get("methodPath"):
            mp = (node.get("methodPath") or "").rstrip("()")
            if mp and "dynamicsAttributeList.Helper" not in mp:
                out.add(mp)
            # 方法自身可能内嵌 FIELD 参数，继续递归其 paramValue
            for v in node.values():
                collect_field_paths(v, out)
            return
        for v in node.values():
            collect_field_paths(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_field_paths(v, out)


def norm_label(s):
    """去中文/英文引号与空格，用于中文组合路径↔field_path 索引匹配。"""
    return (s or "").replace("'", "").replace("\u2018", "").replace("\u2019", "").replace(" ", "")


class BomIndex:
    """封装 rule_editor 的 BomCatalog 检索索引：中文组合路径 label → field_path。"""

    def __init__(self, svc=None):
        # svc：复用已有 EditorService（编辑器服务进程内传入 get_svc() 单例，
        # 避免重复加载 BOM 目录）；CLI 场景不传则自行构造。
        if svc is None:
            from rule_editor.config import load_config
            load_config()
            from rule_editor.service import EditorService
            svc = EditorService()
        # path_index: [{label, field_path, kind, node_label, type, is_enum, source}]
        # label 为"的"连接的中文组合路径（如 保单的人身险信息的060032学生平安保险B款险别列表）
        self._by_label = {}
        for it in getattr(svc.search, "path_index", []):
            lab = it.get("label") or ""
            if lab:
                self._by_label.setdefault(norm_label(lab), it.get("field_path") or "")
        self.ts = svc.search

    def field_path_of_label(self, chinese_label):
        """中文组合路径（可含 '保单' 前缀引号/内部空格）→ field_path；无则 ''。"""
        key = norm_label(chinese_label)
        if not key:
            return ""
        fp = self._by_label.get(key)
        if fp:
            return fp
        # 宽松：去掉前导 root（如 '保单'的… → 保单的…）已由 norm 处理；再试 prefix 展开
        for lab, fp in self._by_label.items():
            if lab.endswith(key) or key.endswith(lab):
                return fp
        return ""

    def parse_predefine(self, content):
        """解析一行 predefine 文本 → [{alias, is_list, fp}]。
        fp：绑定列表/字段的真实 BOM field_path（列表=列表对象字段；字段=叶子字段）。
        """
        c = (content or "").replace("\u2018", "'").replace("\u2019", "'")
        out = []
        # 单行可含多条"指定…; 指定…;"（样本格式），按半角/全角分号拆句；
        # 兼容句间换行（旧格式每行一句）。
        for stmt in re.split(r"[;；\n]", c):
            stmt = stmt.strip()
            if not stmt.startswith("指定"):
                continue
            m = _LIST_RE.match(stmt)
            if m:
                alias, tname = m.group(1), m.group(2)
                # 路径取到"符合条件"子句为止（子句/; 已被上面切分剥除）
                rest = stmt[m.end():]
                path = re.split(r"符合条件\s*[：:]?", rest, 1)[0].strip()
                fp = self.field_path_of_label(path)
                out.append({"alias": alias, "is_list": True,
                            "type_name": tname.strip(),
                            "fp": fp, "path_text": path})
                continue
            m2 = _FIELD_RE.match(stmt)
            if m2:
                alias = m2.group(1)
                # 非列表：指定'别名'为'root'的字段（无"符合条件"子句）
                body = (m2.group(2) + "的" +
                        stmt[m2.end():].split(";", 1)[0].strip())
                fp = self.field_path_of_label(body)
                out.append({"alias": alias, "is_list": False, "type_name": "",
                            "fp": fp, "path_text": body})
        return out

    def resolve_pd_ref(self, fp, predefines):
        """规则中 规则预定义变量.<别名>[.子字段] → 绑定列表/字段的真实 field_path。
        返回 [] 表示未命中（调用方忽略该引用，避免骨架出现中文伪字段）。"""
        if not fp.startswith(PD_PREFIX):
            return [fp]
        tail = fp[len(PD_PREFIX):]
        parts = tail.split(".")
        alias = parts[0]
        if not predefines:
            return []
        for pd in self.parse_predefine(predefines):
            if pd["alias"] == alias and pd["fp"]:
                return [pd["fp"]]
        return []


_SVC = None


def _bom_index():
    global _SVC
    if _SVC is None:
        _SVC = BomIndex()
    return _SVC


# 2026-09-10：骨架只按"如果"条件树（ruleContent）收集，不再解析 then/else 方法参数的
# `${...}` 表达式引用——HHZ2026941103 实测 ADP /test/getInputByRuleId 骨架不含 then 写入
# 目标（"向结果中增加动态域集的040021产品运输方式"的 TransportMode040021），误收集会让
# 前端"测试报文"卡片多出一个无处取值的槽位，用户误填占位符即报 1034 单条测试初始化异常；
# 存量对照（_compare.py 96 条）中该链路贡献的 2 条"本地多收集"随之消除。
def build_input(rule_content, predefine_text="", then_else=None):
    """核心：ruleContent（"如果"条件树）→ input 骨架 dict（叶子 null）。

    then_else 参数保留仅为兼容旧调用方（routes.py/_compare.py/CLI），已不再参与收集。
    """
    if isinstance(rule_content, str):
        rule_content = json.loads(rule_content)
    idx = _bom_index()
    refs = set()
    collect_field_paths(rule_content, refs)
    targets = set()
    for fp in refs:
        resolved = idx.resolve_pd_ref(fp, predefine_text)
        targets.update(resolved)

    root = {}
    for fp in sorted(targets):
        parts = fp.split(".")
        cur = root
        for p in parts[:-1]:
            nxt = cur.get(p)
            if isinstance(nxt, dict):
                cur = nxt
            else:
                # 叶子与分支冲突（如既有 appbase:null 又要建 appbase.upperdpts）：
                # 升级为分支对象（与接口"有子字段即嵌套"的形态一致）
                nxt = {}
                cur[p] = nxt
                cur = nxt
        leaf = parts[-1]
        # 若该叶子早已是分支（另一路径下有子字段），保持分支不覆盖为 null
        if not isinstance(cur.get(leaf), dict):
            cur[leaf] = None
    return root


def find_sample_dirs():
    """默认扫描目录：优先 config samples_dir，否则常见目录。返回目录名列表。"""
    dirs = []
    try:
        from rule_editor.config import load_config, get
        load_config()
        sd = get("data", "samples_dir", "") or ""
        if sd:
            dirs.append(sd)
    except Exception:
        pass
    if not dirs:
        dirs.append("非车销售")
    return dirs


def load_sample(sample_file, rule_id):
    """从 *_samples.jsonl 行读 rule_content + predefine content。
    sample_file 为空时自动扫 config samples 目录。"""
    if rule_id and (not sample_file or not os.path.exists(sample_file)):
        import glob
        for d in find_sample_dirs():
            cand = os.path.join(d, "*_samples.jsonl")
            for fp in sorted(glob.glob(cand)):
                try:
                    with open(fp, encoding="utf-8") as f:
                        for ln in f:
                            ln = ln.strip()
                            if not ln:
                                continue
                            row = json.loads(ln)
                            rid = (row.get("rule_id")
                                   or (row.get("rule_node") or {}).get("ruleId") or "")
                            if rid != rule_id:
                                continue
                            rc_raw = (row.get("if_rule") or {}).get("ruleContent") or ""
                            try:
                                rc = json.loads(rc_raw)
                            except Exception:
                                rc = {}
                            pd = (row.get("predefine") or {}).get("content") or ""
                            return rc, pd, row
                except FileNotFoundError:
                    continue
        return None, None, None
    if not os.path.exists(sample_file):
        return None, None, None
    with open(sample_file, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            row = json.loads(ln)
            rid = row.get("rule_id") or (row.get("rule_node") or {}).get("ruleId") or ""
            if rid != rule_id:
                continue
            rc_raw = (row.get("if_rule") or {}).get("ruleContent") or ""
            try:
                rc = json.loads(rc_raw)
            except Exception:
                rc = {}
            pd = (row.get("predefine") or {}).get("content") or ""
            return rc, pd, row
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule-id", default="")
    ap.add_argument("--sample-file", default="", help="samples jsonl 文件（按 rule_id 定位）")
    ap.add_argument("--rule-content", default="", help="ruleContent JSON 字符串")
    ap.add_argument("--predefine", default="", help="预定义行文本（展开 规则预定义变量 引用）")
    args = ap.parse_args()

    rc, pd, row = None, None, None
    if args.rule_content:
        rc = json.loads(args.rule_content)
        pd = args.predefine
        then_else = None
    elif args.rule_id:
        # sample-file 缺省空 → load_sample 自动扫 config samples 目录
        rc, pd, row = load_sample(args.sample_file, args.rule_id)
        then_else = (row or {}).get("then_else") if row else None
    if rc is None:
        print("未找到规则：需要 --rule-content 或 (--sample-file + --rule-id)", file=sys.stderr)
        return 1
    out = build_input(rc, pd or "", then_else)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
