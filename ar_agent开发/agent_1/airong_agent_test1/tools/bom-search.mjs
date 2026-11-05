#!/usr/bin/env node
/**
 * BOM 字段检索核心（核保规则业务助手使用）
 *
 * 数据源：tools/bom-to-sqlite.mjs 生成的 bom/bom.sqlite（当前车险范围；可用 --db 或环境变量覆盖）。
 * 职责：按关键词检索业务对象字段，并拼出「父链的字段名」这种规则里可直接口述的名称。
 *
 * 该文件不依赖 pi，可独立命令行验证：
 *   node tools/bom-search.mjs 机构
 *   node tools/bom-search.mjs 二级机构 --limit 3 --json
 *   node tools/bom-search.mjs 违章 --methods
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = path.resolve(HERE, "..");
// Verified model metadata matches the existing ruleSearch vehicle scope.
export const DEFAULT_DB_PATH = path.join(PROJECT_ROOT, "bom", "bom.sqlite");
export const DEFAULT_LIMIT = 10;
export const MAX_LIMIT = 50;
const ANCESTOR_SEP = " > ";

let cache = null;

// node:sqlite 首次加载会发出一次 ExperimentalWarning，写进 stderr 会打乱 pi 的 TUI 界面。
// 因此延迟加载该模块，并在加载期间吞掉这条特定警告，其它 warning 保持 Node 默认行为。
let sqliteLoading = null;
function loadSqlite() {
  sqliteLoading ??= (async () => {
    const original = process.emitWarning.bind(process);
    process.emitWarning = (warning, ...rest) =>
      String(warning?.message ?? warning ?? "").includes("SQLite is an experimental feature")
        ? undefined
        : original(warning, ...rest);
    try {
      return (await import("node:sqlite")).DatabaseSync;
    } finally {
      process.emitWarning = original;
    }
  })();
  return sqliteLoading;
}

/** 只读打开 BOM 库并缓存连接；库缺失时给出重建指引。 */
export async function openDatabase(dbPath = DEFAULT_DB_PATH) {
  const abs = path.resolve(dbPath);
  if (cache?.path === abs) return cache.db;
  if (!fs.existsSync(abs)) {
    throw new Error(`BOM 数据库不存在：${abs}\n请先执行：node tools/bom-to-sqlite.mjs import`);
  }
  const DatabaseSync = await loadSqlite();
  const db = new DatabaseSync(abs, { readOnly: true });
  cache = { path: abs, db };
  return db;
}

/** 允许调用方（pi 扩展）关闭进程前释放句柄。 */
export function closeDatabase() {
  if (cache) { cache.db.close(); cache = null; }
}

const COLUMNS = `f.label, f.field, f.field_path AS fieldPath, f.type, f.data_type AS dataType,
  f.class_name AS className, f.parent_label AS parentLabel, f.ancestor_labels AS ancestors,
  f.top_label AS topLabel, f.depth, f.required_field AS requiredField, f.code_service AS codeService,
  f.collection_type AS collectionType, f.actual_type_names AS actualTypeNames, f.leaf_count AS leafCount,
  f.is_method AS isMethod, f.field_id AS fieldId`;

const escapeLike = (v) => v.replace(/[\\%_]/g, (c) => "\\" + c);
const likeWrap = (v) => `%${escapeLike(v)}%`;

/** 祖先链（已排除根「业务模型对象」，根节点不入库）+ 自身 label，用「的」连接。 */
export function qualifiedName(item) {
  const chain = String(item.ancestors ?? "").split(ANCESTOR_SEP).map((s) => s.trim()).filter(Boolean);
  const labels = [...chain, item.label].filter(Boolean);
  // 相邻同名去重，避免出现「保单的保单的...」
  const uniq = labels.filter((v, i) => i === 0 || v !== labels[i - 1]);
  return uniq.join("的");
}

/**
 * 关键词检索业务字段。
 * 排序：label 完全相等 > label 前缀 > label 包含 > 英文字段名包含 > 类名包含 > 其他，
 * 同级再按层级浅、label 短、插入序，保证「机构」这类歧义词先给出最直接的字段。
 *
 * @param {string} keyword
 * @param {{limit?: number, includeMethods?: boolean, dataType?: string, dbPath?: string}} [options]
 */
export async function searchFields(keyword, options = {}) {
  const kw = String(keyword ?? "").trim();
  const limit = normalizeLimit(options.limit);
  if (!kw) {
    return { keyword: kw, limit, scope: "业务字段", items: [], message: "关键词为空，请提供业务要素名称（如：机构、车龄、精算处理）" };
  }

  const db = await openDatabase(options.dbPath);
  const modelInfo = Object.fromEntries(db.prepare(
    "SELECT key, value FROM meta WHERE key IN ('model_name', 'model_names', 'imported_at')"
  ).all().map(row => [row.key, row.value]));
  const like = likeWrap(kw);
  const filters = [];
  const tailParams = [];
  if (!options.includeMethods) filters.push("f.is_method = 0");
  if (options.dataType) { filters.push("f.data_type = ?"); tailParams.push(options.dataType); }

  const sql = `SELECT ${COLUMNS} FROM fields f
    WHERE (f.label LIKE ? ESCAPE '\\' OR f.field LIKE ? ESCAPE '\\' OR f.field_path LIKE ? ESCAPE '\\'
           OR f.class_name LIKE ? ESCAPE '\\' OR f.code_service LIKE ? ESCAPE '\\' OR f.search_text LIKE ? ESCAPE '\\')
          ${filters.length ? `AND ${filters.join(" AND ")}` : ""}
    ORDER BY CASE
        WHEN f.label = ? THEN 0
        WHEN f.label LIKE ? ESCAPE '\\' THEN 1
        WHEN f.label LIKE ? ESCAPE '\\' THEN 2
        WHEN f.field LIKE ? ESCAPE '\\' THEN 3
        WHEN f.class_name LIKE ? ESCAPE '\\' THEN 4
        ELSE 5 END,
      f.depth, length(f.label), f.id
    LIMIT ?`;
  const params = [
    like, like, like, like, like, like,
    ...tailParams,
    kw, `${escapeLike(kw)}%`, like, like, like,
    limit,
  ];

  const rows = db.prepare(sql).all(...params);
  const seen = new Set();
  const items = [];

  const paramsFor = db.prepare(`SELECT param_id AS paramId, type, ordinal
    FROM params WHERE field_id = ? ORDER BY ordinal`);
  const parentIdFor = db.prepare('SELECT parent_field_id FROM fields WHERE field_id = ?');
  const parentFor = db.prepare(`SELECT field_id AS fieldId, parent_field_id AS parentFieldId,
    label, field_path AS fieldPath, type, data_type AS dataType,
    ancestor_labels AS ancestors, is_method AS isMethod
    FROM fields WHERE field_id = ?`);
  for (const row of rows) {
    const dedupeKey = `${row.fieldPath}|${row.dataType}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    // A leaf may live below a parameterized object method (e.g. coverage(code)).
    // Names alone cannot establish its binding: expose the actual parent signature.
    const ancestorMethods = [];
    const visited = new Set([row.fieldId]);
    let parentId = parentIdFor.get(row.fieldId)?.parent_field_id;
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = parentFor.get(parentId);
      if (!parent) break;
      if (parent.isMethod) ancestorMethods.unshift({
        fieldId: parent.fieldId, fieldPath: parent.fieldPath,
        qualifiedName: qualifiedName(parent), type: parent.type, dataType: parent.dataType,
        params: paramsFor.all(parent.fieldId),
      });
      parentId = parent.parentFieldId;
    }
    const fullName = qualifiedName(row);
    // User-confirmed business-system grammar: fill the method's trailing (),
    // then retain the descendant suffix, without repeating the parent chain.
    // Expose an unfilled template only; do not invent business arguments.
    const objectAccessTemplates = ancestorMethods
      .filter(method => ancestorMethods.length === 1 && method.type === 'object' && method.params.length > 0
        && method.qualifiedName.endsWith('()') && fullName.startsWith(method.qualifiedName + '的'))
      .map(method => ({ methodFieldPath: method.fieldPath,
        template: `${method.qualifiedName.replace(/\(\)$/, '(' + method.params.map(p => `<${p.paramId}:${p.type}>`).join(', ') + ')')}${fullName.slice(method.qualifiedName.length)}`,
        params: method.params }));
    items.push({ ...row, qualifiedName: fullName,
      params: paramsFor.all(row.fieldId), ancestorMethods, objectAccessTemplates });
  }
  const scope = options.includeMethods ? "业务字段与方法节点" : "业务字段";
  return { keyword: kw, limit, scope, modelInfo, items, truncated: rows.length >= limit };
}

function normalizeLimit(raw) {
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_LIMIT;
  return Math.min(MAX_LIMIT, n);
}

/** 生成给模型阅读的中文结果文本。 */
export function formatResults(result) {
  const { keyword, limit, items, truncated } = result;
  if (result.message) return result.message;
  const scope = result.scope ?? "业务字段";
  if (!items.length) {
    return `关键词「${keyword}」未检索到${scope}。可尝试：换用更短或更通用的关键词（如用「机构」代替「二级机构代码」）、`
      + `设 includeMethods=true 一并检索方法节点，或用 tools/bom-to-sqlite.mjs 的 sql 子命令直接查询 fields 表。`;
  }
  const head = `关键词「${keyword}」命中 ${items.length} 个${scope}${truncated ? `（已截断，limit=${limit}）` : ""}：`;
  const lines = items.map((it, i) => {
    const facts = [
      `type=${it.type ?? "-"}`,
      `dataType=${it.dataType ?? "-"}`,
      it.className ? `class=${it.className}` : null,
      it.collectionType ? `collection=${it.collectionType}` : null,
      Number(it.requiredField) === 1 ? "必填" : null,
      it.codeService ? `码值服务=${it.codeService}` : null,
    ].filter(Boolean).join("  ");
    return [
      `${i + 1}. ${it.qualifiedName}`,
      `   path: ${it.fieldPath}`,
      `   ${facts}`,
      `   父节点: ${it.ancestors ? it.parentLabel ?? "（未命名）" : "（顶层，直接挂在业务模型对象下）"}   层级: ${it.depth}${it.isMethod ? "   [方法节点]" : ""}`,
    ].join("\n");
  });
  return [head, ...lines].join("\n");
}

/** 面向业务人员的失败文案（本地库无网络抖动，不做重试；仅把技术错误翻译成中文提示）。 */
const FRIENDLY_MESSAGES = {
  missing: "核保业务模型（BOM）数据尚未就绪，暂时无法检索字段。请联系管理员确认数据文件已就位后重试。",
  corrupt: "核保业务模型（BOM）数据文件异常，暂时无法检索字段。请联系管理员重新生成数据后重试。",
  query: "字段检索暂时失败。请稍后重试，或换用更短、更通用的业务关键词。",
};

/**
 * 把 searchFields / openDatabase 抛出的错误分类为「友好文案 + 技术详情」。
 * @param {unknown} err
 * @returns {{kind: "missing"|"corrupt"|"query", friendly: string, technical: string}}
 */
export function describeError(err) {
  const message = String(err?.message ?? err ?? "未知错误");
  let kind = "query";
  if (/数据库不存在|no such table|unable to open|does not exist|SQLITE_CANTOPEN/i.test(message)) kind = "missing";
  else if (/file is not a database|SQLITE_CORRUPT|SQLITE_NOTDATABASE/i.test(message)) kind = "corrupt";
  return { kind, friendly: FRIENDLY_MESSAGES[kind], technical: message };
}

/* ------------------------------------------------------------------- CLI 自检 */

const invokedDirectly = /bom-search\.mjs$/.test(process.argv[1] ?? "");
if (invokedDirectly) {
  const positional = [];
  const opts = { limit: DEFAULT_LIMIT, dbPath: DEFAULT_DB_PATH };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === "--limit") opts.limit = Number(argv[++i]);
    else if (token === "--db") opts.dbPath = argv[++i];
    else if (token === "--data-type") opts.dataType = argv[++i];
    else if (token === "--methods") opts.includeMethods = true;
    else if (token === "--json") opts.json = true;
    else if (token.startsWith("-")) throw new Error(`未知参数：${token}`);
    else positional.push(token);
  }
  const result = await searchFields(positional.join(" "), opts);
  process.stdout.write(opts.json ? JSON.stringify(result, null, 2) + "\n" : formatResults(result) + "\n");
  closeDatabase();
}
