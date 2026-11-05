"""FastAPI 路由（v2：自由文本 + 空格分隔词检索 + 规则上传）。"""
from __future__ import annotations

import os
import sys
import threading
import uuid

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import get, project_root
from ..service import EditorService
from . import auth

router = APIRouter()
svc: EditorService | None = None
_svc_lock = threading.Lock()

# 进程启动即生成、重启必变。前端据此判断"服务是否重启过"：bootId 与
# localStorage 里存的不一致 -> 丢弃旧草稿/页面快照，避免陈旧缓存堆积。
BOOT_ID = uuid.uuid4().hex


@router.get("/boot_id")
def boot_id():
    return {"bootId": BOOT_ID}


# ---------- 临时登录系统（2026-09-30，账号见 config.yaml login 段） ----------

class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@router.post("/login")
def login(body: LoginBody):
    if not auth.enabled():
        return {"ok": True}                      # 未启用登录：直接放行
    if auth.verify(body.username, body.password):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(auth.COOKIE, auth.issue(), httponly=True,
                        samesite="lax", max_age=7 * 24 * 3600)
        return resp
    return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    auth.discard(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@router.get("/auth_check")
def auth_check(request: Request):
    if not auth.enabled():
        return {"authenticated": True}
    return {"authenticated": auth.valid(request.cookies.get(auth.COOKIE))}


def get_svc() -> EditorService:
    """线程安全的单例初始化（避免并发首次请求创建多个实例）。"""
    global svc
    if svc is None:
        with _svc_lock:
            if svc is None:
                svc = EditorService()
    return svc


def _eff_limit(limit: int) -> int:
    """候选条数上限归一化：limit<=0 视为无限制（2026-09-28，与
    /repositories、/catalogues 的 "limit=0 不限" 口径统一）。"""
    return limit if limit and limit > 0 else 10 ** 9


@router.get("/search")
def search(word: str = "", prev: str = "", prevprev: str = "", limit: int = 200,
           predefines: str = ""):
    """词检索：返回当前词候选。

    word 为空（刚按空格）-> 根据 prev / prevprev（上一个词/上上个词）智能候选：
        上上个词是运算符（prev 是手输值）-> 给运算符 + 连接词；
        上一词是运算符/连接词/如果 -> 给 BOM 一级对象（保单/结果/动态域集）；
        上一词是 BOM 对象/字段 -> 给运算符 + 连接词。
    word 非空 -> 匹配的多级对象路径 + 匹配的运算符/连接词。
    predefines：编辑区预定义行（\n 分隔）。解析成功后一级对象候选追加
    "规则预定义变量"，其下级别名/子字段进检索池（输前缀逐级下钻）。
    """
    s = get_svc()
    cands = s.search_word(word, prev, prevprev, _eff_limit(limit), predefines)
    return {"word": word, "prev": prev, "prevprev": prevprev,
            "candidates": cands, "stats": s.stats()}


@router.get("/selector_context")
def selector_context(line: str = "", caret: int = 0):
    return get_svc().search.selector_context(line, caret)


@router.get("/predefine_search")
def predefine_search(kind: str = "type", word: str = "", limit: int = 200,
                     type_name: str = "", field: str = ""):
    """预定义主句/条件子句候选检索。

    kind="type" -> BOM 中 type=list 节点的 actualTypeNames[0]（去重）；
    kind="path" -> type=list 节点组合路径，如 '保单'的人身险信息的xxx险别列表。
    kind="cond_field" -> 条件字段候选（type_name 对应列表节点的子字段）；
    kind="cond_op" -> 条件运算符候选（field 指定字段名，按字段类型给运算符）。
    kind="field_path" -> 非列表右值候选：对象/字段路径（'root'的p1的p2）+ BOM 方法
    候选（kind="method"，call 式模板 帮助.0与1相差的天数(<date> , <date>)，
    类型来自 bom.json detailsList；2026-09-11）。
    type_name 非空时只返回该类型对应的列表路径（与"为一个"后的类型一一对应）。
    检索策略与 /search 一致：完全相等 > 前缀 > 包含；空词给全量前 limit。
    """
    s = get_svc()
    return {"candidates": s.search_predefine(kind, word, _eff_limit(limit),
                                             type_name, field)}


@router.get("/method_search")
def method_search(word: str = "", limit: int = 200):
    """then/else 动作候选：只给 BOM 方法节点。

    word 为空 -> 全量方法（limit 个，limit<=0 无限制）；非空 -> 完全相等 > 前缀 > 包含。
    返回 {candidates: [{label, field_path, class_path, param_count, kind:"method"}]}
    """
    return {"candidates": get_svc().search_methods(word, _eff_limit(limit))}


@router.get("/method_param_context")
def method_param_context(line: str = "", caret: int = -1, limit: int = 200):
    """then/else 参数位上下文检索（2026-09-03）。

    输入当前行动作文本（line）+ 光标偏移（caret），定位光标所在参数位：
    - stage="method"：未匹配到方法（仍在输方法名），candidates 为方法候选；
    - stage="param"：已定位参数位。与预定义实参位口径统一（2026-09-28）：
      占位符标明类型（<double>/<string>/<date>…，或 detailsList/returnType
      声明）的位按类型族过滤给路径候选（空词也弹该类型头部候选，
      param_ptype 回显类型）；类型未知的 field 位保留旧行为（有词才检索），
      text 位不给候选（candidates 空）；返回该参数值在行内区间
      {start, end} 供前端整段替换；
    - stage="none"：无法定位（光标在模板固定文本上等），不弹候选。
    """
    return get_svc().method_param_context(line, caret, _eff_limit(limit))


@router.get("/predefine_param_context")
def predefine_param_context(line: str = "", caret: int = -1, limit: int = 200,
                            predefines: str = ""):
    """预定义 指定''为 右值方法实参位上下文检索（2026-09-11）。

    输入当前行文本 + 光标偏移：
    - stage="param"：光标在方法调用实参区 → 按 detailsList 参数类型过滤
      （numeric/date/string/list）给路径候选；start/end 为光标处占位符
      （<double> 等）或文本片段区间，选中后仅替换该片段（表达式其余不动）；
    - stage="rvalue"：非方法调用/光标在实参区外 → field_path 混合候选（原逻辑）；
    - stage="fallback"：主句不成立或光标在主句内 → 前端回退原 fetchPredefine。
    predefines（2026-09-29）：编辑区全部预定义行（\n 分隔），rvalue 位候选
    混入本块已定义别名及其元素类型子字段（表达式 `'当前险别'的…` 式引用）。
    """
    return get_svc().predefine_param_context(line, caret, _eff_limit(limit),
                                             predefines)


@router.get("/desc")
def desc(text: str = ""):
    """回显/预览整段规则文本。"""
    s = get_svc()
    return {"desc": s.desc(text)}


def _annotate_text(s: EditorService, text: str, predefines: str = "") -> list[dict]:
    """整段文本逐行标注（预览与上传硬校验共用同一口径，避免两处不一致）。

    逐行分流：'预定义'引导行（及其块内续行）走 annotate_predefine_line
    （主句模板切词 + "符合条件"子句校验），其余行走规则标注。
    predefines：编辑区预定义行（\n 分隔），其词条参与规则行标注/校验
    （"规则预定义变量的<别名>的<子字段>"识别为合法对象/字段）。

    2026-09-28：
    - 每个词附带 "_line"（0-based 行号），前端整段单次请求后按行号分组渲染；
    - 跨行连接词校验：上一"规则行"末词已构成完整值（number/boolean/value/date
      且不以逗号收尾）而本行首词又是字段/对象时，说明两个完整条件跨行连写、
      漏了"并且/或者"——生成端(_scan_value 值后只认空格包围的连接词)会把后一
      条件静默吞掉，此处标红爆错（预览 + 上传硬校验同时生效）。
    """
    # 2026-09-23："预定义"是预定义模式唯一入口词，"指定"仅为语句正文。
    # 裸'指定…'行只在文本/编辑区存在'预定义'引导行时按预定义正文处理
    # （多预定义第2…N条续行——foldEntryLines 不把其折进'预定义'行）；
    # 完全未出现'预定义'时整行标红（须以"预定义"开头），预览与上传同口径拦截。
    pd_entry = any(l.strip().startswith("预定义")
                   for l in ((text or "") + "\n" + (predefines or "")).split("\n"))
    # 条件收尾词类：值类 + 区间尾词（"以 X 结尾/属于 X 中的一个/在 X 和 Y 之间"
    # 的 结尾/中的一个/之间 同样完成"字段 运算符 值"三段闭合，其后接字段=缺连接词）
    _VAL_TYPES = ("number", "boolean", "value", "date", "operator_tail")
    words: list[dict] = []
    pend_line = None  # 上一条以完整值收尾的规则行行号（1-based，供报错文案）
    for idx, ln in enumerate((text or "").split("\n")):
        ls = ln.strip()
        ws: list[dict] = []
        if ls.startswith("预定义"):
            ws = s.annotate_predefine_line(ln, predefines)
            pend_line = None
        elif ls.startswith("指定"):
            if pd_entry:
                ws = s.annotate_predefine_line(ln, predefines)
            else:
                ws = [{"text": ls, "type": "text", "valid": False,
                       "error": "预定义行须以「预定义」引导词开头（指定 仅为语句正文）"}]
            pend_line = None
        elif ls.startswith("那么") or ls.startswith("否则"):
            ws = s.annotate_then_else_line(ln)
            pend_line = None
        elif not ls:
            # 空行：生成端把 \s+（含换行/空行）折叠为空格，条件流仍相连，
            # 故不重置 pend_line，保持与 _plain() 口径一致。
            pass
        else:
            if ls.startswith("如果"):
                pend_line = None  # 新条件块起始，丢弃上行行尾状态
            ws = s.annotate(ln, predefines)
            if ws and pend_line is not None:
                first = ws[0]
                if (first.get("type") in ("field", "object")
                        and first.get("valid") is not False):
                    first["valid"] = False
                    first["error"] = ("语法错误: 第%d行与本行两个完整条件之间"
                                      "缺少连接词（并且/或者）" % pend_line)
            last = ws[-1] if ws else None
            ltxt = (last or {}).get("text", "")
            if last is not None and last.get("type") in _VAL_TYPES \
                    and not ltxt.rstrip().endswith((",", "，", "、")):
                pend_line = idx + 1
            else:
                pend_line = None
        for w in ws:
            w["_line"] = idx
        words.extend(ws)
    _mark_dup_aliases(words)
    return words


def _mark_dup_aliases(words: list[dict]) -> None:
    """跨行校验：多条「指定'别名'…」的别名不得重名（2026-09-28）。

    pd_alias 词分两类：主句定义词与右值引用词（'123' / 裸形引用，词法器
    _pd_expr_words 产出，带 pd_ref=True 标记）——只有定义词计入重名分组，
    `指定'446'为 '123'` 引用上一行别名不是重复定义（2026-09-29 修正口径）。
    同名定义 ≥2 处即全部标红（预览 + 上传硬校验同一管线自动两端生效）。
    报错行号为折叠后行序，与跨行连接词校验同一口径。"""
    seen: dict[str, list[dict]] = {}
    for w in words:
        # 只数定义词：右值引用（如 指定'446'为 '123' 引用上一行别名）同为
        # pd_alias 形态，但带 pd_ref 标记，不计入重复定义（2026-09-29 误报修）
        if w.get("type") != "pd_alias" or w.get("pd_ref"):
            continue
        alias = (w.get("text") or "").strip().strip("'‘’")
        if alias:
            seen.setdefault(alias, []).append(w)
    for alias, ws in seen.items():
        if len(ws) < 2:
            continue
        lines = "、".join(str(w.get("_line", 0) + 1) for w in ws)
        for w in ws:
            w["valid"] = False
            w["error"] = ("预定义别名『%s』重复定义（第%s行），"
                          "多个指定的别名须各不相同" % (alias, lines))


@router.get("/annotate")
def annotate(text: str = "", predefines: str = ""):
    """词类型标注：解析整段文本，逐词标注类型（前端据此着色）。"""
    return {"words": _annotate_text(get_svc(), text, predefines)}


@router.get("/normalize_rule")
def normalize_rule(text: str = ""):
    """粘贴整理（2026-09-29）：主系统规则文案 → 编辑器可解析形态。

    如果行按运算符/连接词锚点补空格，那么/否则行删 "1. " 序号，
    预定义行与其余内容原样保留；前端粘贴事件调用，整理后插回编辑框。"""
    return {"text": get_svc().normalize_pasted_rule(text)}


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/upload_defaults")
def upload_defaults():
    """上传表单隐藏参数的默认值（config.yaml upload 节）。

    前端页面加载时拉取，预填到默认折叠的"上传参数（高级）"区输入框；
    展开后仍可手工修改覆盖。ruleId/repositoryId/catalogueId
    三项不走此接口，始终由用户填写。
    token（2026-09-30）：支持在 config.yaml upload.token 预置 ADP JWT，
    前端仅在输入框为空时填入（不覆盖 ?token= / postMessage 注入值）；
    未配置则仍走注入或手工填写，必填口径不变。
    """
    return {
        "url": get("upload", "url", "") or os.environ.get("ADP_RULE_URL", ""),
        "ruleName": get("upload", "rule_name", "") or "测试接口",
        "bomBaseInfoId": get("upload", "bom_base_info_id", ""),
        "bomModelId": get("upload", "bom_model_id", ""),
        "token": get("upload", "token", ""),
    }


# ---------- 决策库对照表（前端"选库"面板数据源） ----------
def _load_repositories() -> list[dict]:
    """读 bom_catalog.sqlite 的 repositories 表（etl/repositories.py 导入）。

    表不存在（未导入过）返回空列表，前端提示补充数据源即可，不报错。
    """
    import sqlite3
    db = get("etl", "bom_db", "data/bom_catalog.sqlite")
    if not os.path.isabs(db):
        db = os.path.join(project_root(), db)
    if not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT id, name FROM repositories ORDER BY rowid")
            return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


@router.get("/repositories")
def repositories(word: str = "", limit: int = 0):
    """决策库列表（repositoryId↔库名）。word 按库名/ID 模糊过滤；limit=0 不限。"""
    rows = _load_repositories()
    w = (word or "").strip().lower()
    if w:
        rows = [r for r in rows if w in (r["name"] or "").lower() or w in (r["id"] or "")]
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    return {"repositories": rows, "total": total}


@router.get("/catalogues")
def catalogues(repositoryId: str = "", word: str = "", limit: int = 0):
    """决策分类列表（catalogueId↔分类名），按 repositoryId 检索（etl/repositories.py 导入）。

    repositoryId 为空或库无分类记录返回空列表；word 按分类名/ID 模糊过滤；limit=0 不限。
    """
    import sqlite3
    repo = (repositoryId or "").strip().lower()
    if not repo:
        return {"catalogues": [], "total": 0}
    db = get("etl", "bom_db", "data/bom_catalog.sqlite")
    if not os.path.isabs(db):
        db = os.path.join(project_root(), db)
    rows: list[dict] = []
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                cur = conn.execute(
                    "SELECT cat_id, name FROM catalogues WHERE repo_id=? ORDER BY rowid",
                    (repo,))
                rows = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
            finally:
                conn.close()
        except sqlite3.Error:
            rows = []   # 表不存在（未导入过）等：空列表兜底
    w = (word or "").strip().lower()
    if w:
        rows = [r for r in rows if w in (r["name"] or "").lower() or w in (r["id"] or "")]
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    return {"catalogues": rows, "total": total}


# ---------- 规则描述 -> 上传 ADP 决策库 ----------
class UploadRuleReq(BaseModel):
    ruleDesc: str                          # 规则描述（预览区渲染后的规范文本，用于生成与上传）
    ruleDescRaw: str = ""                  # 编辑器原文（可选；提供则合法性校验用它，与前端爆红一致）
    ruleId: str                            # 规则编号
    repositoryId: str = ""                 # 决策库 ID（空则取 config 默认/环境变量）
    catalogueId: str = ""                  # 决策分类 ID（空则取默认/环境变量）
    url: str = ""                          # 保存接口地址（空则取环境变量 ADP_RULE_URL）
    token: str = ""                        # adp-token（必填；已移除账号密码直连登录，空则拒绝上传）
    bomPath: str = ""                      # 覆盖 BOM 路径（相对 project_root；空则用 config）
    path: str = "/调节"                    # 规则路径
    ruleName: str = "测试接口"
    verify: bool = False
    predefineContent: str = ""           # 预定义完整内容（预览渲染后的'指定'行，多行以 \n 连接；非空则先 PUT /predefine）
    bomBaseInfoId: str = ""              # 保存预定义必填（配合 predefineContent 上传预定义）
    predefineUrl: str = ""               # 保存预定义接口地址（默认按 url 推导为 /api/predefine）
    thenText: str = ""                   # 那么动作文本（多行 \n 分隔，每行一个动作；非空则规则 POST 后转换并上传 /rule/method/define）
    elseText: str = ""                   # 否则动作文本（多行 \n 分隔）
    bomModelId: str = ""                 # 保存那么/否则必填（规则所属 BOM 模型）
    methodsPath: str = ""                # 回写方法注册表 json（bom_methods），取真实 expressionId


def _default_bom_path() -> str:
    """返回绝对 BOM 路径：优先 config.yaml，否则按常见目录兜底。"""
    rel = get("data", "bom_path", "") or ""
    if rel:
        return os.path.join(project_root(), rel)
    # 兜底常见路径
    for cand in ("车险/bom.json", "非车销售/bom.json"):
        p = os.path.join(project_root(), cand)
        if os.path.exists(p):
            return p
    raise RuntimeError("未找到 BOM 文件，请在 config.yaml 配置 data.bom_path 或提供 bomPath")


# ---------------------------------------------------------------------------
# 测试报文（input 骨架）生成：上传成功后把生成的 ruleContent 交给
# rule_test/input_generator.py 按 /test/getInputByRuleId 接口口径生成入参骨架
# （叶子为 null，命中的值测试时手工填写），放进上传响应，前端右侧"测试报文"卡片展示。
# ---------------------------------------------------------------------------

_BI = None


def _bom_index():
    """label→field_path 索引（复用服务内检索索引，进程内缓存一份）。"""
    global _BI
    if _BI is None:
        import sys
        p = os.path.join(project_root(), "rule_test")
        if p not in sys.path:
            sys.path.insert(0, p)
        from input_generator import BomIndex
        _BI = BomIndex(svc=get_svc())
    return _BI


def _build_test_input(rule_content, predefine_content, then_result):
    """ruleContent + 预定义 → 测试报文 input 骨架。

    骨架只按"如果"条件树收集（2026-09-10：then/else 引用不再参与，ADP 实测
    getInputByRuleId 不含 then 写入目标，误收集会多出无处取值的槽位）；
    then_result 仅保留参数兼容，未上传 then/else 时无影响。
    """
    import sys
    p = os.path.join(project_root(), "rule_test")
    if p not in sys.path:
        sys.path.insert(0, p)
    from input_generator import build_input
    te = None
    if then_result and (then_result.get("thenList") or then_result.get("elseList")):
        te = {"thenList": then_result.get("thenList") or [],
              "elseList": then_result.get("elseList") or []}
    return build_input(rule_content or {}, predefine_content or "", te)


def _then_else_refs(then_result):
    """then/else 上传元素 → 文本里引用的 BOM 对象参数清单（供填空模式展示）。

    那么/否则参数不参与命中判定（2026-09-10 起测试报文骨架也不含它们），但
    fieldParsings 里的 ${fieldPath} 就是动作文本中出现的 BOM 对象引用，经
    path_index 反查中文显示名后返回，前端按"可填参数"列出（仅展示用）。
    返回 [{fp, label, src}]，按出现顺序去重。
    """
    import re as _re
    if not isinstance(then_result, dict):
        return []
    fp2label = {}
    for it in getattr(get_svc().search, "path_index", []):
        fp = it.get("field_path") or ""
        lab = it.get("label") or ""
        if fp and lab and fp not in fp2label:
            fp2label[fp] = lab
    refs, seen = [], set()

    def add(fp, raw):
        fp = (fp or "").strip()
        if not fp or fp in seen:
            return
        seen.add(fp)
        refs.append({"fp": fp, "label": fp2label.get(fp) or (raw or fp),
                     "src": src})

    for key, src in (("thenList", "那么"), ("elseList", "否则")):
        for item in then_result.get(key) or []:
            if not isinstance(item, dict):
                continue
            pv = item.get("paramValue") or {}
            fpv = item.get("fieldParsings") or {}
            for pid in sorted(set(list(pv.keys()) + list(fpv.keys()))):
                parsed = fpv.get(pid)
                raw = pv.get(pid)
                texts = []
                if isinstance(parsed, str):
                    texts.append(parsed)
                if isinstance(raw, str):
                    texts.append(raw)
                found = []
                for t in texts:
                    found.extend(_re.findall(r"\$\{([^}]+)\}", t))
                for fp in found:
                    add(fp, raw)
                # 参数值为嵌套 FIELD 节点（BOM 路径直挂）时也收
                if isinstance(raw, dict) and raw.get("nodeType") == "FIELD" \
                        and raw.get("fieldPath"):
                    add(raw.get("fieldPath"), raw.get("label") or "")
    return refs


def _predefine_refs(predefine_content):
    """预定义'指定…'行 → 文本里出现的 BOM 对象清单（供填空模式展示）。

    口径与 _then_else_refs 一致：仅展示、不进报文、不参与命中判定。逐行解析：
      - 列表式 指定'别名'为一个X来自(in) 'root'的路径 [符合条件:当前这个X的字段…]
        → 收"路径"列表节点本身 + 条件字段（列表路径的"的"+字段名 组合匹配 BOM）
      - 非列表 指定'别名'为['root'的]路径 [符合条件…]; → 收该路径节点
    经 path_index（中文组合 label → field_path）反查英文路径；查不中的（编辑
    中间态/方法调用右值）跳过。返回 [{fp, label, src}]，按出现顺序去重。
    """
    import re as _re
    txt = (predefine_content or "").strip()
    if not txt:
        return []
    s = get_svc().search

    def norm(t):
        return ((t or "").replace("\u2018", "").replace("\u2019", "")
                .replace("'", "").replace(" ", "").replace("\u3000", ""))

    by_key = {}
    for it in getattr(s, "path_index", []):
        k = norm(it.get("label"))
        if k and k not in by_key:
            by_key[k] = it

    def lookup(key):
        it = by_key.get(key)
        if it is None:   # 全/半角括号等价回退
            t = s._paren_trans(key)
            it = by_key.get(t)
            if it is None:
                it = next((v for k, v in by_key.items()
                           if v.get("label") and s._paren_trans(k) == t), None)
        return it

    refs, seen = [], set()

    def add(key, raw):
        it = lookup(norm(key))
        if not it or not it.get("field_path"):
            return
        fp = it["field_path"]
        if fp in seen:
            return
        seen.add(fp)
        refs.append({"fp": fp, "label": it.get("label") or norm(raw), "src": "预定义"})

    for ln in txt.split("\n"):
        ln = ln.strip().replace("\u2018", "'").replace("\u2019", "'")
        ln = _re.sub(r"^预定义\s*", "", ln)
        if not ln.startswith("指定"):
            continue
        m = s.PREDEFINE_LINE_RE.match(ln)
        if m:
            pk = norm(m.group(3))
            add(pk, m.group(3))
            cond = s.parse_predefine_condition(ln)
            if cond:
                add(pk + "的" + norm(cond["field"]), cond["field"])
            continue
        fm = _re.match(
            r"^指定'[^']*'为\s*(?:'([^']*)'的)?([^;]+?)\s*(?:符合条件.*)?;?\s*$", ln)
        if fm and fm.group(1) is not None:
            add((fm.group(1) + "的" + fm.group(2)).strip(), fm.group(2))
    return refs


def _validate_rule_text(req: UploadRuleReq):
    """合法性硬校验（一键上传与分步"生成"共用）。通过返回 None，否则返回错误响应。

    优先用编辑器原文（与前端预览爆红同一文本），未提供原文时退回 ruleDesc
    （兼容旧调用）。与预览同一分流口径（_annotate_text）：'预定义'引导行及其
    块内'指定'续行走预定义标注、
    规则行走规则标注，且规则行需注入预定义词条（predefines）才能识别
    "规则预定义变量.X"为合法字段。
    """
    check_text = (req.ruleDescRaw or req.ruleDesc).strip()
    if not check_text:
        return {"success": False, "errMsg": "规则描述为空，无法生成"}
    pd_lines = "\n".join(ln for ln in check_text.split("\n")
                         if ln.strip().startswith("指定")
                         or ln.strip().startswith("预定义"))
    bad = [(w.get("text", ""), w.get("error") or "不合法")
           for w in _annotate_text(get_svc(), check_text, pd_lines)
           if w.get("valid") is False]
    if bad:
        detail = "；".join("『%s』%s" % (t, e) for t, e in bad)
        return {
            "success": False,
            "errMsg": "规则描述不合法，已阻止生成与上传：" + detail,
            "invalidWords": [{"text": t, "error": e} for t, e in bad],
        }
    return None


def _finish_upload(st, raw, rc_obj, pre_result, then_result, rule_id,
                   predefine_content, url, real_token, rule_desc):
    """上传后的公共响应组装（upload_rule / upload_generated 共用）。

    成功判定（规则 + 那么/否则整体）、refs 收集（填空模式展示）、
    测试报文骨架拉取：优先 ADP /test/getInputByRuleId（复用已登录 token，
    叶子 null 的 ADP 原生骨架）；接口不可达/未返回时退化本地 ruleContent 生成
    （_build_test_input）；均失败才回 testInputError，不影响上传结果本身。
    """
    import sys
    sys.path.insert(0, os.path.join(project_root(), "desc_to_rulecontent"))
    from write_rule_to_adp import get_input_by_rule_id

    try:
        j = __import__("json").loads(raw)
        ok = bool(j.get("success"))
        err = j.get("errMsg") or j.get("msg")
    except Exception:
        ok = st == 200
        err = None
    # 整体成功 = 规则成功 且（提供了那么/否则时）那么/否则成功
    if ok and then_result is not None:
        ok = bool(then_result.get("ok"))
        if not ok:
            err = "那么/否则上传失败: HTTP %s %s" % (then_result.get("st"), (then_result.get("raw") or "")[:200])
            # 仅传那么/否则（无规则行）时，规则须已存在于 ADP，否则 1034
            if not (rule_desc or "").strip() and (then_result.get("thenList") is not None
                                                  or then_result.get("elseList") is not None):
                err += "（仅传那么/否则时，ruleId 对应的规则须已存在于 ADP；请先上传'如果'规则行或改用已存在的 ruleId）"
    resp = {
        "success": ok,
        "httpStatus": st,
        "errMsg": err,
        "ruleId": rule_id,
        "ruleContent": rc_obj,
        "predefine": pre_result,
        "thenElse": then_result,
        # 那么/否则文本里引用的 BOM 对象参数（填空模式展示用，不参与命中判定）
        "thenElseRefs": _then_else_refs(then_result),
        # 预定义'指定…'行里出现的 BOM 对象（同上，仅展示用）
        "predefineRefs": _predefine_refs(predefine_content),
    }
    if ok:
        adp_inp = None
        adp_err = ""
        try:
            from urllib.parse import urlparse
            _u = urlparse(url if "://" in url else "http://" + url)
            origin = "%s://%s" % (_u.scheme, _u.netloc)
            adp_inp, adp_err = get_input_by_rule_id(origin, rule_id, real_token)
        except Exception as e:
            adp_err = "接口调用异常: %s" % e
        if adp_inp is not None:
            resp["testInput"] = adp_inp
            resp["testInputSource"] = "adp"
        else:
            resp["testInputSource"] = "local"
            try:
                resp["testInput"] = _build_test_input(
                    rc_obj, predefine_content, then_result)
            except Exception as e:
                resp["testInputError"] = ("测试报文生成失败: %s（接口取骨架：%s）"
                                          % (e, adp_err or "未获取"))
    return resp


@router.post("/upload_rule")
def upload_rule(req: UploadRuleReq = Body(...)):
    """前端接口（一键"生成并上传"，兼容保留）：生成 ruleContent 并立即上传 ADP。

    2026-09-21 起前端编辑页已改为两步按钮（/api/generate_rule + /api/upload_generated），
    本接口保留给命令行/旧入口，行为不变。

    请求体示例：
      {"ruleDesc":"保单的投保日期 大于 2020-01-01",
       "ruleId":"TEST_001",
       "repositoryId":"a8d57c03e69a4da59996f2bb1624476b",
       "catalogueId":"2beb676c0a3c454e8c5708c8cf19044a"}
    """
    err = _validate_rule_text(req)
    if err:
        return err

    import sys
    sys.path.insert(0, os.path.join(project_root(), "desc_to_rulecontent"))
    from write_rule_to_adp import generate_and_upload

    repo = req.repositoryId or os.environ.get("DEFAULT_REPOSITORY_ID", "")
    cat = req.catalogueId or os.environ.get("DEFAULT_CATALOGUE_ID", "")
    if not repo or not cat:
        return {"success": False, "errMsg": "缺少 repositoryId / catalogueId（或未配置环境变量）"}

    url = req.url or get("upload", "url", "") or os.environ.get("ADP_RULE_URL", "")
    if not url:
        return {"success": False, "errMsg": "缺少 url（未填写且 config.yaml upload.url / 环境变量 ADP_RULE_URL 均未配置）"}

    # 出于安全，上传不再支持账号密码直连登录：必须由用户提供有效 adp-token
    if not (req.token or "").strip():
        return {"success": False, "errMsg": "缺少 token：请提供有效的 adp-token"
                                             "（从主系统登录后获取，或由 iframe URL 参数 ?token= 自动注入）"}

    bom = req.bomPath or _default_bom_path()
    if not os.path.exists(bom):
        return {"success": False, "errMsg": "BOM 文件不存在: %s" % bom}

    try:
        st, raw, rc_obj, pre_result, then_result, real_token = generate_and_upload(
            desc=req.ruleDesc, rule_id=req.ruleId, repository_id=repo,
            catalogue_id=cat, url=url, bom=bom, path=req.path, token=req.token,
            rule_name=req.ruleName or get("upload", "rule_name", "测试接口"),
            verify=req.verify,
            predefine_content=req.predefineContent,
            bom_base_info_id=req.bomBaseInfoId or get("upload", "bom_base_info_id", ""),
            predefine_url=req.predefineUrl or None,
            then_text=req.thenText, else_text=req.elseText,
            bom_model_id=req.bomModelId or get("upload", "bom_model_id", ""),
            methods_path=req.methodsPath)
    except Exception as e:  # 生成/token缺失/网络/预定义/那么否则异常统一返回
        return {"success": False, "errMsg": "上传失败: %s" % e}

    return _finish_upload(st, raw, rc_obj, pre_result, then_result,
                          req.ruleId, req.predefineContent, url, real_token,
                          req.ruleDesc)


# ---------------------------------------------------------------------------
# 2026-09-21 生成/上传两步分离（前端"生成上传参数"+"上传到 ADP"两按钮）：
#   ① POST /api/generate_rule — 只做合法性校验 + 文本→ruleContent/那么否则
#      参数生成，不访问 ADP、不需要 token，返回 generated 给前端缓存审阅；
#   ② POST /api/upload_generated — 直接上传 ① 的 generated（须提供
#      token/url/决策库参数），上传内容与"所见"完全一致，不再重新生成。
#   文本/参数在生成后发生变化时由前端拦截（要求重新生成），后端 ② 以
#   generated 内容为准。POST /api/upload_rule（一键）保留兼容。
# ---------------------------------------------------------------------------

class UploadGeneratedReq(BaseModel):
    generated: dict                        # /api/generate_rule 返回的 generated 对象
    upload: UploadRuleReq                  # 上传参数（url/token/repositoryId/catalogueId/ruleName 等；文本字段以 generated 为准）


@router.post("/generate_rule")
def generate_rule(req: UploadRuleReq = Body(...)):
    """第一步：生成上传参数（离线，不访问 ADP，不需要 token）。"""
    err = _validate_rule_text(req)
    if err:
        return err

    import sys
    sys.path.insert(0, os.path.join(project_root(), "desc_to_rulecontent"))
    from write_rule_to_adp import generate_payload

    bom = req.bomPath or _default_bom_path()
    if not os.path.exists(bom):
        return {"success": False, "errMsg": "BOM 文件不存在: %s" % bom}
    # 生成阶段就检查上传必需的关联 ID，避免"生成成功、上传必败"的空转
    if req.predefineContent and not (req.bomBaseInfoId
                                     or get("upload", "bom_base_info_id", "")):
        return {"success": False,
                "errMsg": "含预定义行时需 bomBaseInfoId（保存预定义必填）"}
    if (req.thenText or req.elseText) and not (req.bomModelId
                                               or get("upload", "bom_model_id", "")):
        return {"success": False,
                "errMsg": "含那么/否则时需 bomModelId（保存回写方法必填）"}

    try:
        gen = generate_payload(desc=req.ruleDesc, bom=bom,
                               predefine_content=req.predefineContent,
                               then_text=req.thenText, else_text=req.elseText,
                               methods_path=req.methodsPath)
    except Exception as e:  # 文本解析失败（动作行/条件行）统一返回，不影响编辑器
        return {"success": False, "errMsg": "生成失败: %s" % e}
    generated = {"ruleId": req.ruleId, "ruleDesc": req.ruleDesc,
                 "predefineContent": req.predefineContent,
                 "thenText": req.thenText, "elseText": req.elseText}
    generated.update(gen)
    # 2026-09-30 填空模式（本地命中测试）改为点"生成上传参数"即渲染：
    # 生成阶段就带回那么/否则与预定义引用的 BOM 对象清单（口径与上传响应一致，
    # _then_else_refs 读 thenList/elseList、_predefine_refs 读 predefineContent，
    # 均离线可算，不访问 ADP），前端据此在生成后即列出需填字段。
    generated["thenElseRefs"] = _then_else_refs(generated)
    generated["predefineRefs"] = _predefine_refs(req.predefineContent)
    return {"success": True, "generated": generated}


@router.post("/upload_generated")
def upload_generated(req: UploadGeneratedReq = Body(...)):
    """第二步：上传 /api/generate_rule 生成的参数（所见即所传，不重新生成）。"""
    gen = req.generated or {}
    rule_id = (gen.get("ruleId") or "").strip()
    if not rule_id or "ruleContent" not in gen:
        return {"success": False,
                "errMsg": "generated 结构不合法：请先点击'生成上传参数'"}
    rule_desc = gen.get("ruleDesc") or ""
    predefine_content = gen.get("predefineContent") or ""

    import sys
    sys.path.insert(0, os.path.join(project_root(), "desc_to_rulecontent"))
    from write_rule_to_adp import upload_payload

    repo = req.upload.repositoryId or os.environ.get("DEFAULT_REPOSITORY_ID", "")
    cat = req.upload.catalogueId or os.environ.get("DEFAULT_CATALOGUE_ID", "")
    if not repo or not cat:
        return {"success": False, "errMsg": "缺少 repositoryId / catalogueId（或未配置环境变量）"}

    url = req.upload.url or get("upload", "url", "") or os.environ.get("ADP_RULE_URL", "")
    if not url:
        return {"success": False, "errMsg": "缺少 url（未填写且 config.yaml upload.url / 环境变量 ADP_RULE_URL 均未配置）"}

    # 出于安全，上传不再支持账号密码直连登录：必须由用户提供有效 adp-token
    if not (req.upload.token or "").strip():
        return {"success": False, "errMsg": "缺少 token：请提供有效的 adp-token"
                                             "（从主系统登录后获取，或由 iframe URL 参数 ?token= 自动注入）"}

    try:
        st, raw, pre_result, then_result, real_token = upload_payload(
            gen, rule_id, repo, cat, url, req.upload.token, desc=rule_desc,
            path=req.upload.path,
            rule_name=req.upload.ruleName or get("upload", "rule_name", "测试接口"),
            verify=req.upload.verify,
            predefine_content=predefine_content,
            bom_base_info_id=req.upload.bomBaseInfoId or get("upload", "bom_base_info_id", ""),
            predefine_url=req.upload.predefineUrl or None,
            bom_model_id=req.upload.bomModelId or get("upload", "bom_model_id", ""),
        )
    except Exception as e:  # token缺失/网络/预定义/那么否则异常统一返回
        return {"success": False, "errMsg": "上传失败: %s" % e}

    return _finish_upload(st, raw, gen.get("ruleContent"), pre_result,
                          then_result, rule_id, predefine_content, url,
                          real_token, rule_desc)


# ---------------------------------------------------------------------------
# 规则命中测试：填值后的 input → POST {url}/test/ruleSingleTest（与 ADP 前端同口径），
# 并可选用本地求值器（rule_test/rule_evaluator.py）对同一份报文离线求值作对照。
# ---------------------------------------------------------------------------

# ADP ruleSingleTest 返回里 resp.ruleContent/ruleConvertContent 等为 base64
# 执行树（数百 KB），归档/回传前端前必须剔除（只留关键状态字段）
_ADP_BIG_KEYS = ("ruleContent", "ruleConvertContent", "bomBaseInfo",
                 "outInfo", "traceInfo", "executeResult")


def _slim_adp_resp(j):
    if not isinstance(j, dict):
        return {}
    o = dict(j)
    r = o.get("resp")
    if isinstance(r, dict):
        o["resp"] = {k: v for k, v in r.items() if k not in _ADP_BIG_KEYS}
    elif isinstance(r, str) and len(r) > 2000:
        o["resp"] = r[:2000] + "…"
    for k in _ADP_BIG_KEYS:
        if k in o and isinstance(o[k], str) and len(o[k]) > 2000:
            o[k] = o[k][:2000] + "…"
    return o


class TestRuleHitReq(BaseModel):
    ruleId: str                            # 规则编号
    input: dict                            # 填值后的测试报文
    url: str = ""                          # ADP 服务基地址（如 http://192.168.0.205:8902；空取 ADP_RULE_URL）
    token: str = ""                        # adp-token（必填；已移除账号密码直连登录）
    applicationId: str = ""                # 测试应用标识（ruleSingleTest 透传；应用不匹配会 success=true 但 pass=0）
    flag: bool = False                     # ruleSingleTest 的 flag（跟踪/日志开关）
    ruleContent: dict | None = None        # 提供则同时跑本地求值器作对照
    predefineText: str = ""                # 预定义行文本（本地求值 BOM 展开用，可选）


def _rule_test_dir():
    p = os.path.join(project_root(), "rule_test")
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


@router.post("/test_rule_hit")
def test_rule_hit(req: TestRuleHitReq = Body(...)):
    """前端接口：提交填值后的报文做规则命中测试（ADP ruleSingleTest + 本地求值对照）。

    本地求值对照最先执行：不依赖 url/token，接口侧 token 缺失/网络失败时也能给出
    "报文填值是否正确"的离线判断（接口 pass=0 时的关键诊断）。
    """
    if not (req.ruleId or "").strip():
        return {"success": False, "errMsg": "缺少 ruleId"}
    if not isinstance(req.input, dict) or not req.input:
        return {"success": False, "errMsg": "测试报文为空"}

    body = {"flag": req.flag, "applicationId": req.applicationId,
            "ruleId": req.ruleId, "input": req.input}
    out = {"success": False, "pass": None,
           "api": {"url": "", "request": body}}

    # ---- 本地求值对照（离线，最先跑） ----
    if req.ruleContent:
        try:
            _rule_test_dir()
            from rule_evaluator import Evaluator, parse, render
            ok, rep, warns = Evaluator(parse(req.ruleContent), req.input,
                                       req.predefineText).run()
            out["localCheck"] = {"pass": ok, "tree": "\n".join(render(rep)),
                                 "warnings": warns}
        except Exception as e:
            out["localCheckError"] = "本地求值失败: %s" % e

    # ---- ADP ruleSingleTest（真实命中） ----
    raw_url = (req.url or os.environ.get("ADP_RULE_URL", "")).strip()
    if not raw_url:
        out["errMsg"] = "缺少 url（或未配置环境变量 ADP_RULE_URL）；已给出本地求值对照"
        return out

    if not (req.applicationId or "").strip():
        out["errMsg"] = ("缺少 applicationId（规则所属应用标识）；"
                         "应用不匹配/为空时 ADP 会 success=true 但 pass 恒 0")
        return out

    # url 可能填的是保存接口地址（…/decision/rule）或服务根（http://host:port）；
    # ruleSingleTest 挂在服务根路径 /test/ruleSingleTest（与 _single_test.py 验证口径一致），
    # 登录走 origin + /api（与上传 resolve_login_base 同口径）。
    from urllib.parse import urlparse
    _u = urlparse(raw_url if "://" in raw_url else "http://" + raw_url)
    origin = "%s://%s" % (_u.scheme, _u.netloc)
    test_base = origin

    out["api"]["url"] = test_base + "/test/ruleSingleTest"
    # 出于安全，已移除账号密码直连登录：测试接口必须携带有效 adp-token
    token = (req.token or "").strip()
    if not token:
        out["errMsg"] = ("缺少 token：请提供有效的 adp-token"
                         "（已移除账号密码直连登录；主系统 iframe 打开时由 URL 参数 ?token= 自动注入）")
        return out

    import sys as _sys2
    _sys2.path.insert(0, os.path.join(project_root(), "desc_to_rulecontent"))
    from write_rule_to_adp import http_json
    import json as _json
    try:
        st, raw = http_json(test_base, "POST", "/test/ruleSingleTest", body,
                            headers={"adp-token": token}, timeout=60)
    except Exception as e:
        out["errMsg"] = "请求异常: %s" % e
        return out

    try:
        j = _json.loads(raw)
    except Exception:
        out["errMsg"] = "ADP 响应非 JSON: %s" % raw[:300]
        out["api"].update({"httpStatus": st, "raw": raw[:500]})
        return out
    resp = j.get("resp") or {}
    out["api"].update({"httpStatus": st, "errCode": j.get("errCode"),
                       "errMsg": j.get("errMsg"), "response": _slim_adp_resp(j)})
    out["success"] = bool(j.get("success"))
    # ADP 的 pass 是字符串 "1"/"0"（偶有布尔），归一成布尔——前端用
    # data.pass === true/false 严格比较，字符串会误判成"无 pass 字段"（2026-09-10）
    _p = resp.get("pass")
    if _p is None:
        out["pass"] = None
    else:
        out["pass"] = str(_p).strip().lower() in ("1", "true")
    out["errMsg"] = j.get("errMsg") or j.get("msg")
    return out


# ---------------------------------------------------------------------------
# 填空模式命中测试：按"如果"条件文本填值 → 仅本地求值器判命中（不调用 ADP 接口）。
# input 由前端按条件树字段路径组装：普通字段按 fieldPath 嵌套；预定义别名组字段
# 落在 input["规则预定义变量"][别名][叶子…]（求值器 _locate a) 分支直接识别）。
# ---------------------------------------------------------------------------

class TestRuleLocalReq(BaseModel):
    ruleId: str = ""                       # 规则编号（仅回显用）
    ruleContent: dict                      # 上传生成的条件树
    input: dict                            # 填空值组装出的最小报文
    predefineText: str = ""                # 预定义行文本（可选）


@router.post("/test_rule_local")
def test_rule_local(req: TestRuleLocalReq = Body(...)):
    """填空模式：只跑本地求值器，不依赖 url/token，不访问 ADP。"""
    if not isinstance(req.ruleContent, dict) or not req.ruleContent:
        return {"success": False, "errMsg": "缺少 ruleContent（请先上传规则）"}
    if not isinstance(req.input, dict) or not req.input:
        return {"success": False, "errMsg": "请至少填写一个字段值"}
    try:
        _rule_test_dir()
        from rule_evaluator import Evaluator, parse, render
        ok, rep, warns = Evaluator(parse(req.ruleContent), req.input,
                                   req.predefineText).run()
        return {"success": True, "pass": ok,
                "tree": "\n".join(render(rep)), "warnings": warns}
    except Exception as e:
        return {"success": False, "errMsg": "本地求值失败: %s" % e}
