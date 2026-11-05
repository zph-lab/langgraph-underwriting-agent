#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 ruleContent 写入 ADP 决策库（对应接口文档「1、保存如果」）。

用法示例：
  # 用已有 ruleContent 文件写入（自动登录 <账号>/<密码>）
  python write_rule_to_adp.py ^
    --url http://192.168.0.205:8902/decision/rule ^
    --rule-content rulecontent_convert_result.json ^
    --rule-id TEST_001 ^
    --rule-desc "保单的投保日期 大于 null" ^
    --path /调节 ^
    --repository-id 15fed48c97604299a7d7cf3f8e425b1a ^
    --catalogue-id b6a9d546d48948ae90e74c2730584bdf

  # 直接用 desc + BOM 生成后再写入
  python write_rule_to_adp.py ^
    --bom ../datas/test1/bom.json ^
    --desc "保单的二级机构 是 动态域集的归属机构的陕西 ..." ^
    --url http://192.168.0.205:8902/decision/rule ^
    --token <adp-token> ^
    --rule-id TEST_002 --path /调节

  # 跳过登录，直接使用已有 token
  python write_rule_to_adp.py --url ... --token <token> --rule-content rc.json ...
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


# 默认值（测试环境）
DEFAULT_USER = "hebao"
DEFAULT_PASSWORD = ""  # 通过参数或部署配置提供，禁止提交明文密码
DEFAULT_REPOSITORY_ID = "a8d57c03e69a4da59996f2bb1624476b"
DEFAULT_CATALOGUE_ID = "2beb676c0a3c454e8c5708c8cf19044a"
#车险：
# DEFAULT_REPOSITORY_ID = "15fed48c97604299a7d7cf3f8e425b1a"
# DEFAULT_CATALOGUE_ID = "b6a9d546d48948ae90e74c2730584bdf"
# path = /调节
#非车销售 （人身险规则库安徽，决策表前置规则）
# DEFAULT_REPOSITORY_ID = "a8d57c03e69a4da59996f2bb1624476b"
# DEFAULT_CATALOGUE_ID = "2beb676c0a3c454e8c5708c8cf19044a"
# path = /决策表前置规则


# ---------- HTTP ----------
def http_json(base, method, path, body=None, headers=None, timeout=120):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, "ERR:%s:%s" % (type(e).__name__, e)


# ---------- RSA 登录（ADP 标准流程） ----------
def der_read_len(d, i):
    l = d[i]
    i += 1
    if l & 0x80:
        n = l & 0x7F
        l = int.from_bytes(d[i:i + n], "big")
        i += n
    return l, i


def der_tlv(d, i):
    tag = d[i]
    i += 1
    l, i = der_read_len(d, i)
    return tag, d[i:i + l], i + l


def rsa_pubkey_from_spki(b64):
    der = base64.b64decode(b64)
    _, spki, _ = der_tlv(der, 0)
    _, alg, i = der_tlv(spki, 0)
    _, bits, _ = der_tlv(spki, i)
    assert bits[0] == 0
    _, rsa, _ = der_tlv(bits, 1)
    _, n_b, i2 = der_tlv(rsa, 0)
    _, e_b, _ = der_tlv(rsa, i2)
    n = int.from_bytes(n_b, "big")
    e = int.from_bytes(e_b, "big")
    return n, e, (n.bit_length() + 7) // 8


def rsa_pkcs1_encrypt(plain, n, e, k):
    msg = plain.encode("utf-8")
    ps_len = k - 3 - len(msg)
    if ps_len < 8:
        raise ValueError("RSA 块长不足")
    ps = b""
    while len(ps) < ps_len:
        ps += bytes(b for b in os.urandom(ps_len - len(ps)) if b != 0)
    em = b"\x00\x02" + ps + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode()


def login(api_base, user, password):
    """ADP 标准登录：generateKey -> RSA 加密密码 -> login -> adp-token"""
    st, raw = http_json(api_base, "GET", "/login/generateKey", timeout=30)
    if st != 200:
        raise RuntimeError("generateKey 失败: %s" % raw[:300])
    gk = json.loads(raw)
    if not gk.get("success"):
        raise RuntimeError("generateKey 失败: %s" % raw[:300])
    n, e, k = rsa_pubkey_from_spki(gk["resp"]["publicKey"])
    pwd = rsa_pkcs1_encrypt(password, n, e, k)
    st, raw = http_json(api_base, "POST", "/login",
                        {"userId": user, "password": pwd, "sign": gk["resp"]["key"]},
                        timeout=30)
    login = json.loads(raw)
    if not login.get("success") or not login.get("resp", {}).get("adp-token"):
        raise RuntimeError("登录失败: %s" % raw[:300])
    return login["resp"]["adp-token"]


def _strip_lr(node):
    """递归剔除接口 input 骨架里的 left/right 键。

    ADP /test/getInputByRuleId 返回的原生骨架把比较/表达式条件树的相对树位
    （left/right）也当成入参键吐了出来（叶子恒为 null，非业务字段），
    与 rule_test/_compare.py clean() 的口径一致：left/right 属噪音，测试报文里剔除。
    """
    if isinstance(node, dict):
        return {k: _strip_lr(v) for k, v in node.items() if k not in ("left", "right")}
    if isinstance(node, list):
        return [_strip_lr(x) for x in node]
    return node


def get_input_by_rule_id(base, rule_id, token, timeout=30):
    """POST {base}/test/getInputByRuleId（form-urlencoded ruleId）→ (input dict | None, err)。

    与 rule_test/fetch_inputs.py fetch_input 同口径：成功取 resp.input（叶子 null 的
    入参骨架，ADP 原生形态），并剔掉接口夹带的 left/right 树位噪音键。
    规则未就绪 / 登录失效 / 网络异常等失败时返回 (None, 原因)，
    调用方据此退化到本地 build_input 生成骨架。base 传服务根 origin（scheme://host:port）。
    """
    from urllib.parse import urlencode
    h = {"Content-Type": "application/x-www-form-urlencoded", "adp-token": token}
    req = urllib.request.Request(
        base + "/test/getInputByRuleId",
        data=urlencode({"ruleId": rule_id}).encode("utf-8"),
        headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, "HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return None, "请求异常: %s" % e
    try:
        j = json.loads(body)
    except Exception:
        return None, "响应非 JSON: %s" % body[:200]
    if not j.get("success"):
        return None, "success=false: %s" % (j.get("errMsg") or body[:200])
    inp = (j.get("resp") or {}).get("input")
    if not isinstance(inp, dict) or not inp:
        return None, "接口未返回 input 骨架"
    inp = _strip_lr(inp)
    if not inp:
        return None, "接口 input 骨架仅含 left/right 树位噪音"
    return inp, ""


# ---------- 主流程 ----------
def build_body(args, rule_content_obj):
    return {
        "path": args.path,
        "decisionRepositoryId": args.repository_id,
        "decisionRepCatalogueId": args.catalogue_id,
        "id": None,
        "ruleTips": None,
        "ruleContent": json.dumps(rule_content_obj, ensure_ascii=False),
        "ruleDesc": args.desc or "",
        "ruleName": args.rule_name,
        "ruleId": args.rule_id,
        "codeServices": [],
    }


def resolve_login_base(url, login_base):
    """根据保存接口地址推导登录根路径。"""
    if login_base:
        return login_base
    from urllib.parse import urlparse
    u = urlparse(url)
    return "%s://%s/api" % (u.scheme, u.netloc)


def resolve_predefine_url(url):
    """根据保存接口地址推导保存预定义接口（/predefine）。"""
    from urllib.parse import urlparse
    u = urlparse(url)
    if "/api/" in u.path:
        return url.rsplit("/", 1)[0] + "/predefine"
    return "%s://%s/predefine" % (u.scheme, u.netloc)


def resolve_then_else_url(url):
    """根据保存接口地址推导保存回写方法接口（/rule/method/define）。"""
    from urllib.parse import urlparse
    u = urlparse(url)
    if "/api/" in u.path:
        return url.rsplit("/", 1)[0] + "/rule/method/define"
    return "%s://%s/rule/method/define" % (u.scheme, u.netloc)


def put_predefine(predefine_url, rule_id, content, bom_base_info_id,
                  token, timeout=120):
    """PUT 预定义内容到 ADP（保存预定义接口）。

    返回 (http_status, raw_response_text)。内容为预定义完整文本
    （编辑区'指定'开头的行，可直接含"符合条件"过滤子句）。
    """
    body = {"bomBaseInfoId": bom_base_info_id,
            "content": content or "",
            "ruleId": rule_id}
    return http_json("", "PUT", predefine_url, body,
                     headers={"adp-token": token}, timeout=timeout)


# ===================== then/else 文本 -> 上传元素 =====================
# 参数槽位：方法 label 中"孤立单数字"（0/1/2…，前后非数字；险别代码 061002 不算）
_SLOT_RE = re.compile(r"(?<!\d)\d(?!\d)")
# 新 BOM（2026-09 起）整块槽位：{N} / {N, 描述}（如 '...销售费用{0, 费用类型}的费用率为：{1, 费用率}'）。
# 只挖数字会把 { } 和说明词残留进固定段，锚点带全角冒号/花括号，用户照模板填值必切分失败
# （表现为"参数 paramX 为空"）。整块槽位（数字自 0 连号）按块切，固定段只留纯文本。
_BRACE_SLOT_RE = re.compile(r"\{\s*(\d+)\s*(?:[,，]\s*([^{}]*?)\s*)?\}")


def _brace_slot_hits(label):
    """label 中整块槽位命中列表；数字非 0 起连号 → None（按老式孤立数字切）。"""
    lb = (label or "").rstrip("()").strip()
    hits = list(_BRACE_SLOT_RE.finditer(lb))
    if hits and [int(h.group(1)) for h in hits] == list(range(len(hits))):
        return lb, hits
    return None


# ADP 导出缺陷的第三种槽位形态：数字槽位后紧跟尖括号说明词整块
# （'向结果增加核保结果0, <核保结果>，规则名称: 1, <规则名称>…'）。
# 数字与 <说明词> 是同一参数位的名+说明，只挖数字会把说明词残留进固定段/
# 模板（'<参数1>, <核保结果>' 双占位、说明词被归到方法本体），须整块挖除，
# 尖括号说明词即语义占位符（与 render_placeholder_template 的 <核保结果> 口径一致）。
_ANGLE_SLOT_RE = re.compile(r"(?<!\d)\d(?!\d)\s*[,，]?\s*<([^<>]{1,12})>")

# 方法注册号提示表（「类名_field」→ ADP 完整 methodId，源自非车销售样本 methodId 统计）。
# 无注册表时派生 expressionId 缺 ADP 端序号（如 _7）会导致 /rule/method/define 返回 1034；
# 查此表补全。未覆盖的方法回退为「类名_field」（无序号）。
# 注意：序号为 ADP 方法注册号（跨 bomModelId 通常一致，样本/手工验证均 _7）；
# 车险等其它 BOM 如遇未覆盖/序号不同，请用 --methods 注册表或手动补。
_METHOD_ID_HINTS = {
    "ResultXom_addUnderwriteRuleInfo": "ResultXom_addUnderwriteRuleInfo_7",
    "ResultXom_addFeeRateForRound": "ResultXom_addFeeRateForRound_5",
    "ResultXom_addFeeAdjustmentRate": "ResultXom_addFeeAdjustmentRate_16",
    "Helper_setMapXomStringAttributeForDeveloper": "Helper_setMapXomStringAttributeForDeveloper_112",
    "Helper_setMapXomListObjectAttributeOfSpecifiedFieldForDeveloper": "Helper_setMapXomListObjectAttributeOfSpecifiedFieldForDeveloper_110",
    "AppBase_setFeeInfoFinalRate": "AppBase_setFeeInfoFinalRate_20",
    "ResultXom_addCpRiskInfo": "ResultXom_addCpRiskInfo_1",
    "AppBase_addMaxFeeRate": "AppBase_addMaxFeeRate_1",
}


def load_methods_from_bom(bom_path):
    """从 bom.json 收集方法节点（fieldPath 以 '()' 结尾）、全量组合 label→fieldPath 映射
    与仅叶子字段（dataType=field）的 label→fieldPath 映射（供表达式参数替换内部字段）。"""
    with open(bom_path, encoding="utf-8") as f:
        bom = json.load(f)
    methods = []
    label_to_fp = {}
    field_fp = {}
    ROOT_PREFIX = "业务模型对象的"

    def walk(node, anc):
        fp = node.get("fieldPath") or ""
        label = node.get("label") or ""
        composed = ("的".join(anc) + "的" if anc else "") + label
        if composed.startswith(ROOT_PREFIX):
            composed = composed[len(ROOT_PREFIX):]
        if fp and composed:
            label_to_fp.setdefault(composed, fp)
            if node.get("dataType") == "field":
                field_fp.setdefault(composed, fp)
        if fp.endswith("()") or node.get("dataType") == "method":
            if label:
                methods.append({
                    "label": label,
                    "field": node.get("field") or "",
                    "fieldPath": fp,
                    "classPath": node.get("classPath") or "",
                    "fieldId": node.get("fieldId") or "",
                    "detailsList": node.get("detailsList") or [],
                })
        for ch in node.get("children") or []:
            if isinstance(ch, dict):
                walk(ch, anc + [label])

    walk(bom, [])
    return methods, label_to_fp, field_fp


def load_methods_registry(path):
    """加载回写方法注册表（context.json 的 bom_methods 或裸数组）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("bom_methods") or []
    return data


def default_methods_registry():
    """默认回写方法注册表：优先按当前 config 的 samples 目录匹配
    bom_methods_<目录名>.json（如非车销售 → bom_methods_非车.json），回退 bom_methods_非车.json。"""
    dirpath = os.path.dirname(os.path.abspath(__file__))
    try:
        _root = os.path.dirname(dirpath)   # 项目根
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from rule_editor.config import get as _get
        sd = (_get("data", "samples_dir", "") or "").strip()
        if sd:
            # config._anchor_paths 已把 *_dir 键锚定为绝对路径（如 "D:\...\车险"），
            # 此处只取末级目录名作注册表文件名；误用整串绝对路径会拼出不存在的
            # "bom_methods_D:\...\车险.json" → 静默回退非车注册表（BOM 与模板文案错位）。
            sd = os.path.basename(sd.rstrip("\\/"))
            p = os.path.join(dirpath, "bom_methods_%s.json" % sd)
            if os.path.exists(p):
                return load_methods_registry(p)
    except Exception:
        pass
    p = os.path.join(dirpath, "bom_methods_非车.json")
    return load_methods_registry(p) if os.path.exists(p) else None


def generate_methods_registry(samples_glob, out_path):
    """从样本 thenList/elseList 提取方法注册表并写出 json（供 --gen-methods 使用）。

    samples_glob：样本文件 glob（如 "车险/*_samples.jsonl"）。
    每个方法（methodId/expressionId）保留首个完整元素结构（含 expressionArray 模板）。
    返回 (方法种类数, 扫描样本行数)。
    """
    import glob as _glob
    methods = {}
    order = []
    total = 0
    for fp in sorted(_glob.glob(samples_glob)):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                total += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                te = obj.get("then_else") or {}
                for it in (te.get("thenList") or []) + (te.get("elseList") or []):
                    mid = it.get("methodId") or it.get("expressionId") or ""
                    if not mid or mid in methods:
                        continue
                    rec = dict(it)
                    rec.pop("order", None)   # order 是每条规则内的序号，不保留样例值
                    methods[mid] = rec
                    order.append(mid)
    payload = {
        "source": "%s 的 thenList/elseList 提取" % samples_glob,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(order),
        "bom_methods": [methods[m] for m in order],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return len(order), total


def _norm_method_label(label):
    """方法 label 归一化：去尾部 ()、去空白（BOM label 尾带 ()，rule_model 不带）。"""
    if not label:
        return ""
    return re.sub(r"\s+", "", label.rstrip("()").strip())


def generate_methods_registry_from_model(bom_path, model_methods_path, out_path):
    """从 BOM + ADP 导出的回写方法表（rule_model.txt）生成方法注册表（供 --gen-methods-model）。

    换 BOM 表后无需样本即可得到全量回写方法的真实 expressionId：
    - BOM 方法节点（fieldPath 以 () 结尾）提供 label/classPath/field/detailsList；
    - rule_model.txt 每行：bomModelId \\t 模板label \\t methodId \\t 类#方法#参数，
      以归一化 label（去尾部 ()、去空白）匹配 methodId。
    返回 (生成方法数, BOM 方法总数)。
    """
    methods, _, _ = load_methods_from_bom(bom_path)
    # rule_model.txt -> 归一化label -> methodId 列表；(类名, 方法名) -> [(methodId, 参数个数, label)]
    model = {}
    sig = {}
    with open(model_methods_path, encoding="utf-8") as f:
        for ln in f:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            key = _norm_method_label(parts[1])
            mid = parts[2].strip()
            if not key or not mid:
                continue
            model.setdefault(key, []).append(mid)
            # 第4列签名 `类全名#方法名#param1,param2,…`：同 label 重载（如 addFeeAdjustmentRate
            # _15 三参/_16 四参）靠参数个数精确配对，避免只取 ids[0] 丢重载。
            if len(parts) > 3 and "#" in parts[3]:
                sp = parts[3].split("#")
                cls_s = (sp[0] or "").rsplit(".", 1)[-1].strip()
                mtd = (sp[1] if len(sp) > 1 else "").strip()
                if cls_s and mtd:
                    ps = [p for p in (sp[2] if len(sp) > 2 else "").split(",") if p.strip()]
                    sig.setdefault((cls_s, mtd), []).append((mid, len(ps), key))
    # BOM 方法 -> methodId：签名（类#方法#参数个数）+label 优先，未用过者先分配
    used = set()
    out = {}
    for m in methods:
        key = _norm_method_label(m.get("label") or "")
        cls_s = (m.get("classPath") or "").rsplit(".", 1)[-1]
        mtd = (m.get("field") or "").rstrip("()")
        pc = len(m.get("detailsList") or [])
        cands = sig.get((cls_s, mtd)) or []
        exact = [c for c in cands if c[1] == pc]
        prio = ([c for c in exact if c[2] == key] or exact
                or [c for c in cands if c[2] == key])
        mid = next((c[0] for c in prio if c[0] not in used), None)
        if mid is None:
            mid = next((c for c in (model.get(key) or []) if c not in used), None)
        if mid is None or mid in out:
            continue
        used.add(mid)
        out[mid] = {
            "methodId": mid,
            "methodName": None,
            "expressionId": mid,
            "label": m.get("label"),
            "classPath": m.get("classPath"),
            "field": m.get("field"),
            "fieldPath": m.get("fieldPath"),
            "detailsList": m.get("detailsList") or [],
        }
    payload = {
        "source": "由 %s + %s 生成" % (os.path.basename(bom_path),
                                     os.path.basename(model_methods_path)),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(out),
        "bom_methods": list(out.values()),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return len(out), len(methods)


def split_template(label):
    """按参数槽位切分方法 label → (segments, slots)。

    '向结果增加销售费用率为：0, 规则名称为:1, 描述信息为: 2()'
      → segments=['向结果增加销售费用率为：', ', 规则名称为:', ', 描述信息为: ', '()']
        slots=[0, 1, 2]
    新 BOM 整块槽位 '{0, 描述}'/'{1}' 按块切（槽位说明词一并挖掉，固定段为纯文本）：
      '初始化结果增加销售费用{0, 费用类型}的费用率为：{1, 费用率}'
      → segments=['初始化结果增加销售费用', '的费用率为：', '']，slots=[0, 1]
    """
    # BOM 方法 label 尾带方法标记 ()（如 '设置保单的费率折扣系数为()'），
    # 用户自然语言文本不含，统一剔除避免前缀匹配失败。
    label = (label or "").rstrip("()").strip()
    bs = _brace_slot_hits(label)
    if bs:
        lb, hits = bs
        parts, slots, prev = [], [], 0
        for h in hits:
            parts.append(lb[prev:h.start()])
            slots.append(int(h.group(1)))
            prev = h.end()
        parts.append(lb[prev:])
        return parts, slots
    # 尖括号复合槽位 '0, <核保结果>' 整块挖除；间隙中的孤立数字仍按老式切
    # （兼容混合写法：部分槽位带 <说明词>、部分为纯数字）。
    hits = [(m.start(), m.end(), int(label[m.start()]))
            for m in _ANGLE_SLOT_RE.finditer(label)]
    bounds = [0] + [x for h in hits for x in (h[0], h[1])] + [len(label)]
    for a, b in zip(bounds[0::2], bounds[1::2]):
        for m in _SLOT_RE.finditer(label, a, b):
            hits.append((m.start(), m.end(), int(m.group())))
    hits.sort()
    parts, slots, prev = [], [], 0
    for s_, e_, n_ in hits:
        parts.append(label[prev:s_])
        slots.append(n_)
        prev = e_
    parts.append(label[prev:])
    return parts, slots


def slot_descs(label):
    """整块槽位 {N, 描述} → {paramN+1: 描述}；老式/无说明词 → {}。

    extract_params 用其剥离用户照模板填写的花括号与说明词尾巴（'{车险, 费用类型}' → '车险'）。
    """
    bs = _brace_slot_hits(label)
    if not bs:
        return {}
    return {"param%d" % (int(h.group(1)) + 1): (h.group(2) or "").strip()
            for h in bs[1] if (h.group(2) or "").strip()}


def _unwrap_slot(v, desc=None):
    """剥离整块槽位填写形态 '{车险, 费用类型}' → '车险'（非花括号值原样返回）。"""
    v = (v or "").strip()
    if v.startswith("{"):
        v = v[1:].strip()
    if v.endswith("}"):
        v = v[:-1].strip()
    if desc and v.endswith(desc):
        v = v[:-len(desc)].strip().rstrip(" ,，、").strip()
    return v


def segments_from_expression_array(expr_array):
    """注册表 expressionArray（text/input 交替）→ extract_params 用的 segments 数组。

    expressionArray 是 ADP 真实渲染结构（不含 BOM label 的参数说明词）：
      [text "设置保单销售费用", input param1, text " 费用类型的最终费用率为:",
       input param2, text ", 规则名称为:", input param3, text ", 描述信息为: ", input param4]
    → segments = ["设置保单销售费用", " 费用类型的最终费用率为:", ", 规则名称为:",
                  ", 描述信息为: ", ""]，参数数 = len(segments)-1。

    返回 (segments, n_params) 或 (None, 0)（无 input 节点）。
    """
    if not expr_array:
        return None, 0
    segs = []
    buf = []
    n = 0
    n_text = 0
    for node in expr_array:
        if not isinstance(node, dict):
            continue
        t = node.get("type")
        if t == "text":
            buf.append(node.get("desc") or "")
            n_text += 1
        elif t == "input":
            segs.append("".join(buf))
            buf = []
            n += 1
    if n == 0:
        return None, 0
    tail = "".join(buf)
    # 可选尾参：input 数超过 text 锚点段数、且尾部 input 没有自己的固定文本锚点
    # （模板表现为占位符连排，如 ResultXom_addFeeRateForRound_20 的 '<string><string>'）。
    # ADP 端该尾参可不传（2,760 条车险真实样本 2,748 条只传锚定参数），编辑器统一
    # 不渲染、不切分、不校验——否则用户面对无处填写的槽位必报"参数 paramX 为空"。
    while n > n_text and segs and not segs[-1]:
        segs.pop()
        n -= 1
    segs.append(tail)   # 最后一个参数后的尾部 text
    return segs, n


def input_placeholder(desc, return_type):
    """input 段占位符：desc 有语义（含尖括号且非通用'参数/值'、非整段模板型超长）
    → 原样保留；字段引用提示（无尖括号）→ 原样；通用/超长/空 → <returnType>。"""
    d = (desc or "").strip()
    if d:
        if "<" in d and ">" in d:
            inner = d.strip("<>")
            # 语义占位通常短（<核保结果>/<费用率>/<描述信息>）；超长多为 ADP 注册缺陷
            # （占位符复制整段模板，如 migrateSet* 的 <设置保单的费率折扣系数为>）→ 落类型
            if len(inner) <= 8 and not re.search(r"(参数|任意值)$", inner):
                return d          # 语义占位：<核保结果>/<费用率>/<描述信息>
        elif "<" not in d:
            return d              # 字段引用提示：动态域集的帮助的当前规则的名称
    return "<%s>" % (return_type or "string")


def render_placeholder_template(expr_array):
    """注册表 expressionArray → 占位模板文本（text 固定段原样 + input 语义/类型占位）。

    供方法候选插入：数字槽位（BOM label）→ 参数位语义/类型占位，如
      '设置保单销售费用0, 费用类型的最终费用率为:1, 费用率,...'
    → '设置保单销售费用<string> 费用类型的最终费用率为:<费用率>, ...'
    """
    if not expr_array:
        return None
    _segs, n_params = segments_from_expression_array(expr_array)
    # n_params 与参数切分同一口径（无锚点可选尾参不渲染，避免占位符连排）；
    # 纯 text 数组（无 input，_segs=None）不限额，行为同旧。
    parts = []
    seen = 0
    for node in expr_array:
        if not isinstance(node, dict):
            continue
        t = node.get("type")
        if t == "text":
            parts.append(node.get("desc") or "")
        elif t == "input":
            seen += 1
            if _segs and seen > n_params:
                continue
            parts.append(input_placeholder(node.get("desc"), node.get("returnType")))
    return "".join(parts)


_SLOT_NUM_RE = re.compile(r"(?<!\d)\d(?!\d)")   # 独立单个数字 = BOM label 参数槽位


def template_head_has_slots(tpl):
    """占位模板畸形判定：首个占位符之前的固定文本仍含独立数字槽位（0/1/2…）。

    样本提取的 expressionArray 若未把参数内联（text 段整句含数字槽位、input 全部后置），
    渲染结果会是'整句+末尾堆占位符'，如
      '帮助.设置对象0的字符串属性(开发人员使用)1的值为2<string><string><string>'
    正常模板 text 段已按参数位切分（槽位数字已挖掉），不会命中。
    """
    if not tpl:
        return False
    head = re.split(r"<[^<>]*>", tpl, maxsplit=1)[0]
    return bool(_SLOT_NUM_RE.search(head))


def degenerate_label_template(label, return_types=None):
    """BOM label 数字槽位 → 参数占位符（expressionArray 缺失/畸形时的兜底渲染）。

    把 label 中的槽位数字 0..n-1 按参数顺序替换为 <returnType>（类型已知）或
    <参数N>（未知），如
      '帮助.设置对象0的字符串属性(开发人员使用)1的值为2'
    → '帮助.设置对象<string>的字符串属性(开发人员使用)<string>的值为<string>'
    多位数字（字段编号等）不动；数字个数多于参数类型时余量用 <参数N>。
    尖括号复合槽位 '0, <核保结果>' 整块替换为语义占位 <核保结果>（说明词不残留）。
    """
    lb = (label or "").rstrip("()")
    if not lb or not (_ANGLE_SLOT_RE.search(lb) or _SLOT_NUM_RE.search(lb)):
        return lb or None
    types = [t for t in (return_types or []) if t]
    seq = []

    def _repl(_m):
        i = len(seq)
        seq.append(i)
        desc = (_m.group(1) or "").strip("<> \t")
        if desc:   # 复合槽位：尖括号说明词即语义占位符
            return "<%s>" % desc
        return "<%s>" % (types[i] if i < len(types) else "参数%d" % (i + 1))

    # 复合槽位（数字+逗号+尖括号说明词，捕获组1）优先整体消费，其余独立数字单槽
    return re.sub(r"(?<!\d)\d(?!\d)\s*[,，]?\s*(<[^<>]{1,12}>)|(?<!\d)\d(?!\d)",
                  _repl, lb)


def param_types_from_details(details_list):
    """BOM 方法 detailsList（原生参数定义 {id,type,paramId}）→ {paramN: type}。

    BOM 树 95% 方法节点带 detailsList，paramId 与 label 槽位数字一一对应
    （槽位 0→param1…），type 比 expressionArray 的 returnType 权威
    （如 param1=object，而退化 expressionArray 误标 string）。
    """
    out = {}
    for d in details_list or []:
        if isinstance(d, dict) and d.get("paramId"):
            t = (d.get("type") or "").strip()
            if t:
                out[d["paramId"]] = t
    return out


def expression_array_is_degenerate(expr_array):
    """expressionArray 畸形判定：无 input / text 段残留数字槽位（样本提取未内联参数）。"""
    if not expr_array:
        return True
    segs, n = segments_from_expression_array(expr_array)
    if not segs or n <= 0:
        return True
    tpl = render_placeholder_template(expr_array)
    return bool(tpl and template_head_has_slots(tpl))


def method_match_score(text, segs):
    """方法模板固定文本在 text 中按序可定位的总长（首处定位失败即止）。

    方法匹配度：完整模板命中（用户已填参）> 仅首段命中（方法名阶段）。
    首段等长的重名方法（setMapXomStringAttribute 与 …ForDeveloper 的引导段
    都是"帮助.设置对象"）靠更完整的固定文本命中区分，避免误配。
    """
    pos, total = 0, 0
    for d in (s for s in segs if s):
        idx = text.find(d, pos)
        if idx < 0:
            break
        total += len(d)
        pos = idx + len(d)
    return total


def effective_parse_basis(label, expr_array, details_list=None):
    """方法文本 → 参数切分基准 (segments, slots, param_types, keep_punct)。

    优先级：
    1. 注册表 expressionArray 正常（text 段无数字槽位）→ text/input 结构切分；
    2. 畸形/缺失 → BOM label 数字槽位切分（split_template）——label 槽位数字
       是 BOM 原生参数位标记（detailsList 无文本信息，位置只能取自 label）。
    param_types：detailsList 优先，回退 expressionArray input returnType。
    keep_punct：label 槽位段锚点保留括号等固定文本（extract_params 传参）。
    """
    segs, slots = None, None
    keep_punct = False
    if expr_array and not expression_array_is_degenerate(expr_array):
        segs_ea, n_ea = segments_from_expression_array(expr_array)
        if segs_ea and n_ea > 0:
            segs, slots = segs_ea, list(range(n_ea))
    if segs is None:
        segs, slots = split_template(label)
        keep_punct = True
    types = param_types_from_details(details_list)
    if not types and expr_array:
        for e in expr_array:
            if isinstance(e, dict) and e.get("type") == "input" and e.get("paramId"):
                rt = (e.get("returnType") or "").strip()
                if rt:
                    types[e["paramId"]] = rt
    return segs, (slots or []), types, keep_punct, slot_descs(label)


def _norm_tpl_text(s):
    """模板固定段归一化（去空白、全半角冒号/逗号统一），用于重载消歧比对。"""
    s = (s or "").strip()
    return re.sub(r"\s+", "", s.replace("：", ":").replace("，", ","))


def pick_registry_entry(cands, label):
    """methodId 同源的多条目（方法重载）→ 按 BOM label 与 expressionArray
    text 段一致性择优；无 expressionArray / 全不命中 → 回退第一条。

    重载方法（同类同名，methodId 仅 ADP 序号不同：ResultXom_addFeeRateForRound_5/_6）
    的 derived 前缀相同，`_` 序号边界无法区分；须按 label 固定段与注册表 text 段
    按序可定位长度消歧——_5 的 '向结果增加销售费用率为:' 在 _6 的 label 中 0 命中，
    _6 的 '向结果增加销售费用率,费用类型为:…' 全长命中，据此选对条目。
    """
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    hay = _norm_tpl_text((label or "").rstrip("()"))
    best, best_ratio = None, -1.0
    for x in cands:
        segs = [_norm_tpl_text(d.get("desc")) for d in (x.get("expressionArray") or [])
                if isinstance(d, dict) and d.get("type") == "text" and d.get("desc")]
        segs = [s for s in segs if s]
        total = sum(len(s) for s in segs)
        ratio = (method_match_score(hay, segs) / total) if total else 0.0
        if ratio > best_ratio:
            best, best_ratio = x, ratio
    return best


def find_registry_entry(method, registry):
    """按 BOM 方法（classPath.类 + field）在注册表中匹配方法条目（methodId 前缀/名称匹配）。

    methodId 前缀匹配须带 `_` 序号边界（类_方法名_序号），否则
    setMapXomStringAttribute 会误配到 setMapXomStringAttributeForDeveloper_112，
    取错 expressionId（上传注册错方法）与模板。
    前缀命中的多条目（方法重载，methodId 仅序号不同）再按 label 消歧
    （pick_registry_entry），否则上传重载方法会取错 expressionId/结构。
    """
    if not registry:
        return None
    cls = method.get("classPath") or ""
    cls = cls.rsplit(".", 1)[-1] if cls else ""
    field = method.get("field") or ""
    derived = "%s_%s" % (cls, field)
    cands = [m for m in registry
             if isinstance(m, dict) and (
                 (m.get("methodId") or "") == derived or
                 (m.get("methodId") or "").startswith(derived + "_") or
                 m.get("methodName") == field or
                 (m.get("methodName") or "").endswith(field))]
    if not cands:
        # BOM 节点的 Java 方法名可与 ADP 方法模型错位（如 BOM field=
        # migrateSetcrenewtyp 但 label"设置保单Fack的上年赔款金额为"在
        # rule_model 对应 migrateSetnclaimamount）→ 按 label 文案匹配降级：
        # 候选以 BOM 为准，expressionId 一律以 rule_model.txt 生成的注册表为准。
        key = _norm_method_label(method.get("label") or "")
        if key:
            cands = [m for m in registry if isinstance(m, dict)
                     and _norm_method_label(m.get("label") or "") == key]
    # 同 label 重载（label 完全相同、仅参数个数不同，如 addFeeAdjustmentRate
    # _15 三参/_16 四参）先按参数个数过滤，label 消歧对它们无能为力。
    dl = method.get("detailsList") or []
    if dl and len(cands) > 1:
        same = [m for m in cands if len(m.get("detailsList") or []) == len(dl)]
        if same:
            cands = same
    return pick_registry_entry(cands, method.get("label"))


def _strip_placeholder(s):
    """去掉方法模板中的 <占位符>（如 <核保结果>），用户文本通常省略它们。"""
    return re.sub(r"<[^>]*>", "", s)


def _anchor(seg, keep_punct=False):
    """槽位后片段 → 核心关键字锚点：去占位符 + 去标点/空白（逗号冒号括号引号）。

    keep_punct=True（BOM label 槽位段）：保留括号——label 固定文本中的
    '(开发人员使用)' 等是用户照抄的一部分，去括号会导致锚点匹配不上。
    """
    pat = r"[\s<>,，:：\"']" if keep_punct else r"[\s<>,，:：()（）\"']"
    return re.sub(pat, "", _strip_placeholder(seg))


def _lead_word(seg, keep_punct=False):
    """模板段 → 下一参数核心引导词（段尾'XX为'，如 ', 费用率,规则名称为:' → '规则名称为'）。

    用户输入常省略槽位值后的描述词（', 费用率,' 的'费用率'），但保留下一参数引导词
    （'规则名称为'/'描述信息为'），完整锚点缺失时用引导词降级切分。
    """
    m = re.search(r"([\u4e00-\u9fa5A-Za-z]+为)\s*[:：]?\s*$", _strip_placeholder(seg))
    if m:
        return m.group(1)
    return _anchor(seg, keep_punct)


def extract_params(text, segments, slots, keep_punct=False, slot_desc=None):
    """用"核心关键字锚点"切分输入文本提取参数值（paramId 1-based：槽位 0→param1）。

    keep_punct：segments 来自 BOM label 槽位切分时传 True（锚点保留括号等固定文本）。
    slot_desc：整块槽位 {paramN: 说明词}（slot_descs 产物）；提供时切出的值剥离
    用户照模板保留的花括号与说明词（'{车险, 费用类型}' → '车险'）。
    """
    params = {}

    def _put(slot, v):
        key = "param%d" % (slot + 1)
        params[key] = _unwrap_slot(v, (slot_desc or {}).get(key)) if slot_desc else v

    rest = text
    for i, slot in enumerate(slots):
        if i == 0:
            prefix = segments[0]
            if prefix and rest.startswith(prefix):
                rest = rest[len(prefix):]
        if i < len(slots) - 1:
            anchor = _anchor(segments[i + 1], keep_punct)
            idx = rest.find(anchor) if anchor else -1
            used = anchor
            if idx < 0 and anchor:
                # 完整锚点缺失（用户省略槽位值后的描述词）→ 用下一参数核心引导词降级
                lead = _lead_word(segments[i + 1], keep_punct)
                if lead and lead != anchor:
                    idx = rest.find(lead)
                    used = lead if idx >= 0 else anchor
            if idx >= 0:
                _put(slot, rest[:idx].rstrip(" ,，:：\t"))
                rest = rest[idx + len(used):].lstrip(" :：,，\t")
            else:
                _put(slot, rest.strip())
                rest = ""
        else:
            _put(slot, rest.strip())
    if not slots:
        # 无数字槽位的方法（如 '设置保单的费率折扣系数为'）隐含单个参数：
        # 先去模板首段前缀，剩余整体作为 param1
        rest = rest.strip()
        prefix = segments[0] if segments else ""
        if prefix and rest.startswith(prefix):
            rest = rest[len(prefix):].strip()
        if rest:
            params["param1"] = rest
    return params


def resolve_field_parsing(value, label_to_fp):
    """参数值：BOM 字段/动态域 label -> ${fieldPath}（方法去 ()）；已 ${...} 或字面量原样。"""
    if not isinstance(value, str):
        return value
    if value.startswith("${"):
        return value
    if value in label_to_fp:
        fp = label_to_fp[value]
        if fp.endswith("()"):
            fp = fp.rstrip("()")
        return "${%s}" % fp
    return value


def _looks_like_expr(v):
    """参数值是否为表达式（含运算/方法调用括号/拼接），表达式不应整体加引号。"""
    if not isinstance(v, str):
        return False
    return bool(re.search(r"[()+\-*/%]", v)) or "${" in v


def resolve_expr_field_parsing(expr, label_to_fp):
    """表达式参数 → fieldParsings：把内部 BOM 字段/动态域 label 替换为 ${fieldPath}（最长优先）。

    对齐样本表达式格式（paramValue 字段保留中文路径、fieldParsings 换 ${...}）：
       '"非特殊审批…"+保单的保险期间+"天…"'
        → '"非特殊审批…"+${appbase.tinsrnclimit}+"天…"'
    已 ${...} 的区域跳过，避免重复替换。
    """
    if not isinstance(expr, str) or not expr:
        return expr
    masked = re.sub(r"\$\{[^}]*\}", "", expr)   # 去掉已 ${} 区域做存在性判断
    result = expr
    for label in sorted(label_to_fp, key=len, reverse=True):
        if not label or len(label) < 2:
            continue
        if label in masked and label in result:
            fp = label_to_fp[label]
            if fp.endswith("()"):
                fp = fp.rstrip("()")
            result = result.replace(label, "${%s}" % fp)
    return result


def param_kinds_from_registry(reg, details_list=None):
    """从注册表 expressionArray 推断每个 input 参数位的语义：
    'field'（BOM 字段/域项，paramValue 裸路径 + fieldParsings ${}）或
    'text'（纯文本字面量，两处一致且带引号）。
    判断依据：codeService 非空 → field；desc 为完整字段路径（含'动态域集'）→ field；
    其余（<占位符> 等）→ text。
    BOM detailsList 纠偏：expressionArray 畸形（整句 text 段）时 desc 不可靠，
    type=object/集合类的参数位按字段处理（如 param1=保单对象 → ${appbase}）。
    """
    kinds = {}
    for e in (reg or {}).get("expressionArray") or []:
        if e.get("type") != "input" or not e.get("paramId"):
            continue
        pid = e["paramId"]
        desc = e.get("desc") or ""
        if e.get("codeService") or ("动态域集" in desc and "<" not in desc):
            kinds[pid] = "field"
        else:
            kinds[pid] = "text"
    if details_list and expression_array_is_degenerate((reg or {}).get("expressionArray")):
        for pid, t in param_types_from_details(details_list).items():
            if t == "object" or "list" in t.lower() or "collect" in t.lower():
                kinds[pid] = "field"
    return kinds


def build_params(params, label_to_fp, param_kinds=None, field_fp=None):
    """把参数值分为 paramValue 与 fieldParsings 两套：
    BOM 字段 → paramValue 原文 + fieldParsings ${fieldPath}；
    文本字面量 → 两者一致（裸字符串原样透传，不加引号）。

    参数位类型不拦截：字段位/文本位一视同仁，识别到的值按内容处理——
    能解析为 BOM 字段 → ${fieldPath}；字符串字面量 → 原样（param1/param2 等
    字段位值本就是裸串，文本位保持一致，避免上传后值里多出一对引号）；
    表达式（含运算/方法调用）→ 原样 + 内部字段转 ${}。
    field_fp（仅叶子字段的 label→fieldPath）用于表达式参数内部字段替换，避免误替换对象/方法路径。
    """
    param_value = {}
    field_parsings = {}
    for k, v in (params or {}).items():
        if isinstance(v, str):
            v = v.strip()   # extract_params 可能保留前导/尾随空格，先归一
        fp = resolve_field_parsing(v, label_to_fp)
        if fp == v:
            if _looks_like_expr(v):
                # 表达式参数：paramValue 原样（字段保留中文路径），fieldParsings 内部字段转 ${}
                param_value[k] = v
                field_parsings[k] = resolve_expr_field_parsing(v, field_fp or label_to_fp)
            else:
                # 文本字面量：原样透传（不再自动补引号，2026-09-21）
                param_value[k] = v
                field_parsings[k] = v
        else:
            param_value[k] = v
            field_parsings[k] = fp
    return param_value, field_parsings


def build_item(method, params, order, label_to_fp, registry=None, method_id=None,
                field_fp=None):
    """把 方法节点 + 参数值 → 上传元素（paramValue/fieldParsings 已解析）。

    无注册表时 expressionId 由「类名_field」派生（缺 ADP 端序号，需注册表或手动补）。
    """
    if registry:
        reg = find_registry_entry(method, registry)
        if reg:
            method_id = reg.get("methodId") or method_id
            if reg.get("expressionArray"):
                pv, fp = build_params(params, label_to_fp,
                                      param_kinds_from_registry(
                                          reg, method.get("detailsList")), field_fp)
                return {k: v for k, v in reg.items() if k in (
                    "methodId", "expressionId", "expressionArray", "paramValue",
                    "fieldParsings", "order", "methodName", "returnType",
                    "codeService", "beforeParamType", "methodPath")} | {
                    "paramValue": pv,
                    "fieldParsings": fp,
                    "order": str(order),
                }
    if not method_id:
        cls = method["classPath"].rsplit(".", 1)[-1]
        derived = "%s_%s" % (cls, method["field"])
        method_id = _METHOD_ID_HINTS.get(derived) or derived
    pv, fp = build_params(params, label_to_fp, field_fp=field_fp)
    return {
        "methodId": method_id,
        "expressionId": method_id,
        "paramValue": pv,
        "fieldParsings": fp,
        "order": str(order),
    }


def convert_then_else_line(text, methods, label_to_fp, registry=None, field_fp=None):
    """单行 thenelse 文本（可带'那么/否则'前缀）→ 上传元素。返回 (element, matched_label, error)。"""
    t = (text or "").strip()
    for kw in ("那么", "否则"):
        if t.startswith(kw):
            t = t[len(kw):].strip()
    if not t:
        return None, "", "空行"
    best = None
    for m in methods:
        segs, _ = split_template(m["label"])
        if not segs or not segs[0]:
            continue
        if t.startswith(segs[0]):
            key = (method_match_score(t, segs),
                   len("".join(s for s in segs if s)))
            if best is None or key > best[0]:
                best = (key, m)
    if best is None:
        return None, "", "未匹配到 BOM 方法（文本应以方法模板首段开头）"
    # 参数切分基准（统一走 effective_parse_basis）：注册表 expressionArray 正常时
    # 优先用其 text/input 结构切分（无 BOM label 说明词干扰）；畸形（text 段残留
    # 数字槽位）/缺失 → BOM label 数字槽位（BOM 原生参数位标记）。
    m0 = best[1]
    reg = find_registry_entry(m0, registry)
    segments, slots, _types, keep, descs = effective_parse_basis(
        m0["label"], (reg or {}).get("expressionArray"), m0.get("detailsList"))
    params = extract_params(t, segments, slots, keep, descs)
    item = build_item(m0, params, 0, label_to_fp, registry, field_fp=field_fp)
    return item, m0["label"], ""


def then_else_elem(item):
    """上传元素 → ADP 要求的正确结构（nodeType/expressionId/returnType/paramValue/fieldParsings）。"""
    return {
        "nodeType": item.get("nodeType") or "EXPRESSION",
        "codeService": item.get("codeService"),
        "expressionId": item.get("expressionId") or item.get("methodId") or "",
        "returnType": item.get("returnType") or "string",
        "paramValue": item.get("paramValue") or {},
        "fieldParsings": item.get("fieldParsings") or {},
    }


def convert_then_else_text(then_text, else_text, bom_path, registry=None):
    """then/else 文本（多行 \n 分隔）→ (thenList, elseList) 正确结构元素。

    每行一个动作；'那么/否则' 前缀可选（按传入的 then_text/else_text 分区）。
    任何一行解析失败抛 ValueError（含行内容）。
    """
    methods, label_to_fp, field_fp = load_methods_from_bom(bom_path)

    def conv(text):
        out = []
        for ln in (text or "").splitlines():
            if not ln.strip():
                continue
            item, matched, err = convert_then_else_line(ln, methods, label_to_fp, registry, field_fp)
            if err:
                raise ValueError("动作行解析失败『%s』: %s" % (ln.strip(), err))
            out.append(then_else_elem(item))
        return out

    return conv(then_text), conv(else_text)


def generate_payload(desc, bom, predefine_content=None, then_text=None,
                     else_text=None, methods_path=None):
    """生成上传参数（纯离线，不访问 ADP、不需要 token）。

    2026-09-21 生成/上传分离第一步：规则描述 → ruleContent、那么/否则文本 →
    回写方法定义元素。返回 dict（可直接 JSON 序列化给前端缓存，第二步上传）：
      {"only_pre": bool, "only_then": bool,
       "ruleContent": dict|None, "thenList": list|None, "elseList": list|None}
    only_pre/only_then 语义与原 generate_and_upload 一致：无规则行时仅传
    预定义或仅传那么/否则，不生成 ruleContent。
    任何一行动作解析失败抛 ValueError（含行内容）。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rule_desc_to_content import RuleGenerator
    desc_empty = not (desc or "").strip()
    # 仅传预定义（无规则行）时跳过规则生成
    only_pre = desc_empty and bool(predefine_content)
    # 仅传那么/否则（无规则行、无预定义）时同样跳过规则生成
    only_then = desc_empty and not predefine_content and bool(then_text or else_text)
    rc_obj = None
    if not (only_pre or only_then):
        gen = RuleGenerator(bom)
        if predefine_content:
            rc_obj = gen.generate_with_predefines(desc, [predefine_content])
        else:
            rc_obj = gen.generate(desc)
    then_list = else_list = None
    if then_text or else_text:
        registry = (load_methods_registry(methods_path) if methods_path
                    else default_methods_registry())
        then_list, else_list = convert_then_else_text(then_text, else_text, bom, registry)
    return {"only_pre": only_pre, "only_then": only_then,
            "ruleContent": rc_obj, "thenList": then_list, "elseList": else_list}


def fetch_existing_rule_id(url, rule_id, token, timeout=30):
    """查 ADP 已存在规则的主键 id（GET /decision/rule/{ruleId} 详情接口）。

    保存接口（POST /decision/rule）是"保存/更新"一体的 upsert：body.id 为空
    按新增处理，ruleId 撞已有规则即报「规则编号:xxx 已存在」；body.id 带已有
    记录主键则执行更新。本函数取详情 resp.id 供回填。
    路径候选与 verify 回查一致（/api/decision/rule 与 /decision/rule）。
    查询失败/不存在/无 token 场景均返回 None（调用方保持新增行为）。
    """
    if not (token or "").strip() or not rule_id:
        return None
    from urllib.parse import urlparse
    u = urlparse(url)
    base = "%s://%s" % (u.scheme, u.netloc)
    from urllib.parse import quote
    for p in ("/api/decision/rule/%s" % quote(rule_id),
              "/decision/rule/%s" % quote(rule_id)):
        st, raw = http_json(base, "GET", p,
                            headers={"adp-token": token}, timeout=timeout)
        if st != 200 or not raw or raw.startswith("ERR:"):
            continue
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("success"):
            resp = j.get("resp") or {}
            rid = resp.get("id") if isinstance(resp, dict) else None
            if rid:
                return rid
    return None


def upload_payload(gen, rule_id, repository_id, catalogue_id, url, token,
                   desc="", path="/调节", rule_name="测试接口", timeout=120,
                   verify=False, predefine_content=None, bom_base_info_id=None,
                   predefine_url=None, bom_model_id=None, then_else_url=None,
                   overwrite=True):
    """上传 generate_payload 产出的参数到 ADP 决策库（需有效 token，不再账号密码登录）。

    2026-09-21 生成/上传分离第二步：上传内容与第一步"所见即所传"，不再重新生成。
    步骤与拆分前完全一致：预定义 PUT（先，失败阻断）→ 规则 POST → verify 回查 →
    那么/否则 POST。返回 (http_status, raw_response_text, pre_result,
    then_result, real_token)。pre_result/then_result 为上传结果 dict（未传时 None）：
      {"url":..., "st":..., "raw":..., "ok": bool}
    then_result 额外携带已转换的 thenList/elseList（供调用方核对上传内容）。

    predefine_content：预定义完整内容（渲染后的'指定'行，多行以 \n 连接）。
    bom_base_info_id：保存预定义必填。predefine_url：默认按 url 推导。
    gen 含 thenList/elseList 时需 bom_model_id，规则 POST 成功后
    POST /rule/method/define。
    """
    if not (token or "").strip():
        raise RuntimeError("缺少 adp-token：上传须提供有效 token"
                           "（已出于安全移除账号密码直连登录）")
    real_token = token
    rc_obj = gen.get("ruleContent")
    only_pre = bool(gen.get("only_pre"))
    only_then = bool(gen.get("only_then"))

    # 预定义 PUT（先）：规则 ruleContent 引用"规则预定义变量.X"，须先保证预定义已存在。
    # 仅当传了 predefine_content（含 bom_base_info_id）时执行；失败则阻断规则上传。
    pre_result = None
    if predefine_content:
        if not bom_base_info_id:
            raise RuntimeError("提供 predefineContent 时需 bomBaseInfoId（保存预定义必填）")
        p_url = predefine_url or resolve_predefine_url(url)
        st0, raw0 = put_predefine(p_url, rule_id, predefine_content,
                                  bom_base_info_id, real_token, timeout)
        pre_ok = st0 == 200 and '"success":true' in raw0
        pre_result = {"url": p_url, "st": st0, "raw": raw0, "ok": pre_ok}
        if not pre_ok:
            raise RuntimeError("预定义上传失败: HTTP %s %s" % (st0, (raw0 or "")[:300]))
        if only_pre:
            return st0, raw0, pre_result, None, real_token

    if only_then:
        # 仅传那么/否则：跳过规则 POST（规则须已存在于 ADP，由 then/else 接口校验）
        st, raw = 200, '{"success":true}'
    else:
        # 2026-09-21 覆盖更新：ruleId 已存在时回填主键 id 走 upsert 更新，
        # 避免保存接口报「规则编号:xxx 已存在」；查不到（不存在/接口异常）保持新增
        existing_id = (fetch_existing_rule_id(url, rule_id, token)
                       if overwrite else None)
        body = {
            "path": path,
            "decisionRepositoryId": repository_id,
            "decisionRepCatalogueId": catalogue_id,
            "id": existing_id,
            "ruleTips": None,
            "ruleContent": json.dumps(rc_obj, ensure_ascii=False),
            "ruleDesc": desc,
            "ruleName": rule_name,
            "ruleId": rule_id,
            "codeServices": [],
        }
        st, raw = http_json("", "POST", url, body,
                            headers={"adp-token": real_token}, timeout=timeout)

    if not only_then and verify and st == 200 and '"success":true' in raw:
        from urllib.parse import urlparse
        u = urlparse(url)
        base = "%s://%s" % (u.scheme, u.netloc)
        for p in ("/api/decision/rule/%s" % rule_id,
                  "/decision/rule/%s" % rule_id):
            st2, raw2 = http_json(base, "GET", p,
                                  headers={"adp-token": real_token}, timeout=30)
            if st2 == 200 and '"success":true' in raw2:
                break

    # then/else 上传（规则 POST 成功后，保证 ruleId 已存在）；
    # thenList/elseList 来自第一步生成结果，不再重新转换（所见即所传）
    then_result = None
    then_list, else_list = gen.get("thenList"), gen.get("elseList")
    if then_list is not None or else_list is not None:
        if not bom_model_id:
            raise RuntimeError("提供 thenText/elseText 时需 bomModelId（保存回写方法必填）")
        te_url = then_else_url or resolve_then_else_url(url)
        te_body = [{"bomModelId": bom_model_id, "ruleId": rule_id,
                    "elseList": else_list or [], "thenList": then_list or []}]
        stt, rawt = http_json("", "POST", te_url, te_body,
                              headers={"adp-token": real_token}, timeout=timeout)
        te_ok = stt == 200 and '"success":true' in rawt
        # 附带转换好的 then/else 结构随结果返回（测试报文骨架自 2026-09-10 起只按
        # "如果"条件树生成，不再消费该结构，保留供调用方核对上传内容）。
        then_result = {"url": te_url, "st": stt, "raw": rawt, "ok": te_ok,
                       "thenList": then_list, "elseList": else_list}
    return st, raw, pre_result, then_result, real_token


def generate_and_upload(desc, rule_id, repository_id, catalogue_id,
                        url, bom, path, token=None,
                        rule_name="测试接口", timeout=120, verify=False,
                        predefine_content=None, bom_base_info_id=None,
                        predefine_url=None,
                        then_text=None, else_text=None, bom_model_id=None,
                        methods_path=None, then_else_url=None):
    """端到端：生成上传参数 + 写入 ADP 决策库 = generate_payload + upload_payload。

    2026-09-21 生成/上传分离后保留为一键兼容入口（命令行与 /api/upload_rule 用）。
    返回 (http_status, raw_response_text, rc_obj, pre_result, then_result,
    real_token)，与拆分前完全一致。
    """
    gen = generate_payload(desc, bom, predefine_content, then_text,
                           else_text, methods_path)
    st, raw, pre_result, then_result, real_token = upload_payload(
        gen, rule_id, repository_id, catalogue_id, url, token, desc=desc,
        path=path, rule_name=rule_name, timeout=timeout, verify=verify,
        predefine_content=predefine_content, bom_base_info_id=bom_base_info_id,
        predefine_url=predefine_url, bom_model_id=bom_model_id,
        then_else_url=then_else_url)
    return st, raw, gen.get("ruleContent"), pre_result, then_result, real_token



def main():
    ap = argparse.ArgumentParser(description="把 ruleContent 写入 ADP 决策库（保存如果接口）")
    ap.add_argument("--url", help="保存接口地址，如 http://host:port/api/decision/rule（--gen-methods 时不需要）")
    ap.add_argument("--login-base", help="登录接口根路径（默认取 --url 的协议+host，拼 /api）")
    ap.add_argument("--token", help="已有 adp-token，提供后跳过登录")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)

    rc = ap.add_mutually_exclusive_group()
    rc.add_argument("--rule-content", help="ruleContent JSON 文件路径")
    rc.add_argument("--bom", help="BOM json 路径（配合 --desc/--samples 现生成 ruleContent）")
    ap.add_argument("--desc", help="规则描述文本（配合 --bom 使用；也可作为 ruleDesc 写入）")
    ap.add_argument("--desc-file", help="规则描述文件路径（内容直接作为 ruleDesc，支持 HTML）")
    ap.add_argument("--samples", help="批量写入: samples.jsonl 格式文件，逐条生成并 POST")
    ap.add_argument("--with-predefine", action="store_true",
                    help="同时 PUT 预定义(保存预定义接口), 需 --bom-base-info-id")
    ap.add_argument("--with-then-else", action="store_true",
                    help="同时 POST 那么/否则(保存回写方法接口)")
    ap.add_argument("--predefine-url", help="保存预定义接口地址(默认按 --url 推导为 /api/predefine)")
    ap.add_argument("--predefine-content", help="预定义 content 文本")
    ap.add_argument("--predefine-content-file", help="预定义 content 文件路径")
    ap.add_argument("--bom-base-info-id", help="bomBaseInfoId(保存预定义必填)")
    ap.add_argument("--bom-model-id", help="bomModelId(保存回写方法必填)")
    ap.add_argument("--bomid-map", help="bomModelId->bomBaseInfoId 对应表文件(bomid对应表)")
    ap.add_argument("--then-else", help="那么/否则 json 文件: {bomModelId, ruleId, thenList, elseList}")
    ap.add_argument("--then-else-url", help="保存回写方法接口地址(默认按 --url 推导为 /rule/method/define)")
    ap.add_argument("--then-text", help="那么文本（多行 \n 分隔，每行一个动作；配合 --bom 转换后上传）")
    ap.add_argument("--else-text", help="否则文本（多行 \n 分隔，每行一个动作；配合 --bom 转换后上传）")
    ap.add_argument("--methods", help="回写方法注册表 json（context.json 的 bom_methods），取真实 expressionId")
    ap.add_argument("--gen-methods", help="（已弃用，仅临时排查）从样本生成方法注册表后退出：样本 glob（如 车险/*_samples.jsonl）。样本 expressionId 为历史快照可能与当前模型错位，正式口径用 --gen-methods-model（rule_model.txt 为准）")
    ap.add_argument("--gen-methods-model", help="从 BOM + rule_model.txt 生成方法注册表后退出：BOM json 路径（无需样本）")
    ap.add_argument("--model-methods", help="ADP 导出的回写方法表（rule_model.txt）路径（配合 --gen-methods-model）")
    ap.add_argument("--gen-methods-out", help="注册表输出路径（默认 desc_to_rulecontent/bom_methods_<目录名>.json）")
    ap.add_argument("--get-input", metavar="RULE_ID",
                    help="只抓单条规则的 input 骨架（ADP /test/getInputByRuleId）打印后退出；"
                         "默认 ADP 根 http://192.168.0.205:8902，可 --url/--token 覆盖")

    ap.add_argument("--rule-id", help="规则编号（单条模式必填）")
    ap.add_argument("--rule-name", default="测试接口")
    ap.add_argument("--path", help="规则路径，如 /调节（--gen-methods 时不需要）")
    ap.add_argument("--repository-id", default=DEFAULT_REPOSITORY_ID)
    ap.add_argument("--catalogue-id", default=DEFAULT_CATALOGUE_ID)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--verify", action="store_true", help="成功后尝试 GET 回读验证")
    args = ap.parse_args()

    # --gen-methods：只生成方法注册表，不连接 ADP
    if args.gen_methods:
        # 相对 glob 锚定项目根（desc_to_rulecontent 的父目录，如 车险/*_samples.jsonl）
        g = args.gen_methods
        head = g.split("*")[0]
        if not os.path.isabs(head):
            g = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), g)
            head = g.split("*")[0]
        if not args.gen_methods_out:
            base = os.path.basename(head.rstrip("/\\")) or "default"
            args.gen_methods_out = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "bom_methods_%s.json" % base)
        cnt, total = generate_methods_registry(g, args.gen_methods_out)
        print("扫描样本 %d 行，方法 %d 种" % (total, cnt))
        print("已写出:", args.gen_methods_out)
        return 0

    # --gen-methods-model：从 BOM + rule_model.txt 生成方法注册表（无需样本）
    if args.gen_methods_model:
        if not args.model_methods:
            ap.error("--gen-methods-model 需要 --model-methods（ADP 导出的回写方法表 rule_model.txt）")
        if not os.path.isfile(args.gen_methods_model):
            ap.error("BOM 文件不存在: %s" % args.gen_methods_model)
        if not os.path.isfile(args.model_methods):
            ap.error("rule_model 文件不存在: %s" % args.model_methods)
        if not args.gen_methods_out:
            head = os.path.dirname(args.gen_methods_model)
            base = os.path.basename(head.rstrip("/\\")) or "default"
            args.gen_methods_out = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "bom_methods_%s.json" % base)
        cnt, total = generate_methods_registry_from_model(
            args.gen_methods_model, args.model_methods, args.gen_methods_out)
        print("BOM 方法 %d 个，生成注册表 %d 个方法" % (total, cnt))
        print("已写出:", args.gen_methods_out)
        return 0

    # --get-input：单条规则抓 ADP input 骨架（未给 --token 则先登录）
    if args.get_input:
        url = args.url or "http://192.168.0.205:8902/decision/rule"
        from urllib.parse import urlparse
        u = urlparse(url)
        origin = "%s://%s" % (u.scheme, u.netloc)
        token = args.token or login(resolve_login_base(url, args.login_base),
                                    args.user, args.password)
        inp, err = get_input_by_rule_id(origin, args.get_input, token,
                                        timeout=args.timeout)
        if inp is None:
            print("获取失败: %s" % err, file=sys.stderr)
            return 1
        print(json.dumps(inp, ensure_ascii=False, indent=2))
        return 0

    bomid_map = {}
    if args.bomid_map:
        with open(args.bomid_map, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] != "bommodelid":
                    bomid_map[parts[0].strip()] = parts[1].strip()
    if args.bom_model_id and not args.bom_base_info_id and args.bom_model_id in bomid_map:
        args.bom_base_info_id = bomid_map[args.bom_model_id]
        print("由 bomid-map 解析 bomBaseInfoId:", args.bom_base_info_id)
    if args.bom_base_info_id and not args.bom_model_id:
        for mid, bid in bomid_map.items():
            if bid == args.bom_base_info_id:
                args.bom_model_id = mid
                print("由 bomid-map 解析 bomModelId:", args.bom_model_id)
                break

    if not args.url:
        ap.error("--url 必填")
    if not args.path:
        ap.error("--path 必填")
    if not args.samples and not args.rule_content and not args.bom:
        ap.error("需要 --samples / --rule-content / --bom 之一")
    if not args.samples and not args.rule_id:
        ap.error("非批量模式必须提供 --rule-id")

    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            args.desc = f.read()

    if args.predefine_content_file:
        with open(args.predefine_content_file, encoding="utf-8") as f:
            args.predefine_content = f.read()

    if args.predefine_url is None:
        args.predefine_url = resolve_predefine_url(args.url)
    if args.then_else_url is None:
        args.then_else_url = resolve_then_else_url(args.url)

    def post_then_else(bom_model_id, rule_id, then_list, else_list):
        # 元素统一归一化为 ADP 正确结构（nodeType/expressionId/returnType/paramValue/fieldParsings）
        body = [{"bomModelId": bom_model_id, "ruleId": rule_id,
                 "elseList": [then_else_elem(e) for e in (else_list or [])],
                 "thenList": [then_else_elem(e) for e in (then_list or [])]}]
        return http_json("", "POST", args.then_else_url, body,
                         headers={"adp-token": args.token}, timeout=args.timeout)

    # 1) ruleContent 来源
    if args.samples:
        import os as _os
        sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from rule_desc_to_content import RuleGenerator, plain_desc
        if not args.bom:
            ap.error("--samples 模式必须提供 --bom")
        if not args.token:
            ap.error("--samples 模式请提供 --token（避免逐条登录）")
        with open(args.samples, encoding="utf-8") as f:
            samples = [json.loads(l) for l in f if l.strip()]
        gen = RuleGenerator(args.bom)
        ok = fail = 0
        for s in samples:
            ir = s.get("if_rule") or {}
            desc = plain_desc(ir.get("ruleDesc") or "")
            rc = gen.generate_with_predefines(
                desc, [(s.get("predefine") or {}).get("content")])
            rule_id = s.get("rule_id") or ir.get("ruleId")

            te = s.get("then_else") or {}
            bom_base = args.bom_base_info_id or s.get("bom_base_info_id")
            bom_model = (args.bom_model_id or te.get("bomModelId")
                         or s.get("bom_model_id"))

            def send_all(rid):
                """预定义 PUT + then/else POST + 规则 POST, 全部使用同一个 ruleId。"""
                if args.with_predefine:
                    if not bom_base:
                        ap.error("批量模式 PUT 预定义需要 --bom-base-info-id")
                    st0, raw0 = put_predefine(args.predefine_url, rid,
                                              (s.get("predefine") or {}).get("content"),
                                              bom_base, args.token, args.timeout)
                    print("PRE  %s -> HTTP %s %s" % (rid, st0, raw0[:200]))
                if args.with_then_else:
                    st0, raw0 = post_then_else(bom_model, rid,
                                               te.get("thenList"), te.get("elseList"))
                    print("TE   %s -> HTTP %s %s" % (rid, st0, raw0[:200]))
                body = build_body(args, rc)
                body["ruleId"] = rid
                body["ruleDesc"] = desc
                st, raw = http_json("", "POST", args.url, body,
                                    headers={"adp-token": args.token},
                                    timeout=args.timeout)
                print("RULE %s -> HTTP %s %s" % (rid, st, raw[:200]))
                return st, raw

            st, raw = send_all(rule_id)
            try:
                j = json.loads(raw)
            except Exception:
                j = None
            if j and j.get("success"):
                ok += 1
            elif j and "已存在" in str(j.get("errMsg")):
                written = False
                for _ in range(3):
                    unique = "TEST_%s_%d" % (rule_id, int(time.time() * 1000) % 1000000)
                    print("冲突 %s -> 改用 %s 三部分重发" % (rule_id, unique))
                    st2, raw2 = send_all(unique)
                    if '"success":true' in raw2:
                        written = True
                        break
                    try:
                        j2 = json.loads(raw2)
                    except Exception:
                        j2 = None
                    if not (j2 and "已存在" in str(j2.get("errMsg"))):
                        break
                ok += 1 if written else 0
                fail += 0 if written else 1
            else:
                fail += 1
                print("FAIL %s -> HTTP %s %s" % (rule_id, st, raw[:300]))
        print("批量结果: 成功 %d / 失败 %d" % (ok, fail))
        return
    if args.rule_content:
        with open(args.rule_content, encoding="utf-8") as f:
            rc_obj = json.load(f)
        # 2) token
        if args.token:
            token = args.token
            print("使用提供的 token")
        else:
            args.login_base = resolve_login_base(args.url, args.login_base)
            token = login(args.login_base, args.user, args.password)
            args.token = token
            print("登录成功（%s）" % args.user)
        # 3) 预定义 PUT(先) + 规则 POST(后)
        if args.with_predefine:
            if not args.bom_base_info_id:
                ap.error("PUT 预定义需要 --bom-base-info-id")
            st0, raw0 = put_predefine(args.predefine_url, args.rule_id,
                                      args.predefine_content,
                                      args.bom_base_info_id, token, args.timeout)
            print("预定义 PUT HTTP %s" % st0)
            print(raw0)
        if args.with_then_else:
            if args.then_text or args.else_text:
                # 文本模式：then/else 文本 -> 方法模板解析 -> 正确结构元素
                if not args.bom:
                    ap.error("--then-text/--else-text 需要 --bom（构建方法索引）")
                if not args.bom_model_id:
                    ap.error("--then-text/--else-text 需要 --bom-model-id")
                registry = (load_methods_registry(args.methods) if args.methods
                            else default_methods_registry())
                then_list, else_list = convert_then_else_text(
                    args.then_text, args.else_text, args.bom, registry)
                bom_model = args.bom_model_id
                print("那么/否则文本转换: then %d 条, else %d 条" % (len(then_list), len(else_list)))
            else:
                if not args.then_else:
                    ap.error("--with-then-else 需要 --then-else <json文件> 或 --then-text/--else-text")
                with open(args.then_else, encoding="utf-8") as f:
                    te = json.load(f)
                bom_model = args.bom_model_id or te.get("bomModelId")
                then_list, else_list = te.get("thenList"), te.get("elseList")
            st0, raw0 = post_then_else(bom_model, args.rule_id,
                                       then_list, else_list)
            print("那么/否则 POST HTTP %s" % st0)
            print(raw0)
        body = build_body(args, rc_obj)
        print("ruleId:", args.rule_id)
        st, raw = http_json("", "POST", args.url, body, headers={"adp-token": token},
                            timeout=args.timeout)
        print("保存响应 HTTP %s" % st)
        print(raw)
    else:
        # 直接由 desc 生成并上传（复用 generate_and_upload；支持 --with-predefine / --with-then-else）
        if not args.desc:
            ap.error("使用 --bom 时必须提供 --desc")
        pre_content = args.predefine_content if args.with_predefine else None
        pre_bom_base = args.bom_base_info_id if args.with_predefine else None
        te_text = args.then_text if args.with_then_else else None
        el_text = args.else_text if args.with_then_else else None
        te_model = args.bom_model_id if args.with_then_else else None
        # CLI 保留账号密码登录（开发者显式 --user/--password），但 generate_and_upload
        # 已不接收密码参数，故在调用前显式换取 token；Web 上传链路则强制由用户提供 token。
        up_token = args.token
        if not up_token:
            up_token = login(resolve_login_base(args.url, args.login_base),
                             args.user, args.password)
            print("登录成功（%s）" % args.user)
        st, raw, rc_obj, pre_result, then_result, _up_token = generate_and_upload(
            desc=args.desc, rule_id=args.rule_id,
            repository_id=args.repository_id, catalogue_id=args.catalogue_id,
            url=args.url, bom=args.bom, path=args.path, token=up_token,
            rule_name=args.rule_name, timeout=args.timeout, verify=args.verify,
            predefine_content=pre_content, bom_base_info_id=pre_bom_base,
            predefine_url=args.predefine_url,
            then_text=te_text, else_text=el_text, bom_model_id=te_model,
            methods_path=args.methods, then_else_url=args.then_else_url)
        print("保存响应 HTTP %s" % st)
        print(raw)
        if pre_result:
            print("预定义 PUT %s -> HTTP %s (ok=%s)" %
                  (pre_result["url"], pre_result["st"], pre_result["ok"]))
            print(pre_result["raw"])
        if then_result:
            print("那么/否则 POST %s -> HTTP %s (ok=%s)" %
                  (then_result["url"], then_result["st"], then_result["ok"]))
            print(then_result["raw"])

    # 4) 回读验证（可选）
    try:
        j = json.loads(raw)
    except Exception:
        j = None
    if j and j.get("success") and args.verify:
        from urllib.parse import urlparse
        u = urlparse(args.url)
        base = "%s://%s" % (u.scheme, u.netloc)
        for p in ("/api/decision/rule/%s" % args.rule_id,
                  "/decision/rule/%s" % args.rule_id):
            st2, raw2 = http_json(base, "GET", p, headers={"adp-token": token}, timeout=30)
            print("回读 %s -> HTTP %s %s" % (p, st2, raw2[:400]))
            if st2 == 200 and '"success":true' in raw2:
                break


if __name__ == "__main__":
    sys.exit(main())
