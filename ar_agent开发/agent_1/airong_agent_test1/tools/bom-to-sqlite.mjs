#!/usr/bin/env node
/**
 * 核保规则业务助手 —— BOM 字段树转 SQLite
 *
 * 把 bom/bom.json（业务模型对象树）展平成关系表，保留 label / type / dataType /
 * fieldPath 以及父节点与完整祖先链，供智能体用 SQL/FTS 检索业务字段。
 *
 * 零依赖，使用 Node 内置 node:sqlite（需 Node >= 22.5，推荐 24.x）。
 *
 * 用法：
 *   node tools/bom-to-sqlite.mjs import [--in bom/bom.json] [--out bom/bom.sqlite] [--drop]
 *   node tools/bom-to-sqlite.mjs search 精算     [--limit 20] [--data-type field] [--include-methods] [--json]
 *   node tools/bom-to-sqlite.mjs info  result.apInfos.apInfoProcessed [--json]
 *   node tools/bom-to-sqlite.mjs parents result.apInfos.apInfoProcessed
 *   node tools/bom-to-sqlite.mjs children result.apInfos [--limit 30]
 *   node tools/bom-to-sqlite.mjs stats
 *   node tools/bom-to-sqlite.mjs sql "select count(*) c from fields"
 */

import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const ROOT_DIR = path.resolve(import.meta.dirname, "..");
const DEFAULT_IN = path.join(ROOT_DIR, "bom", "bom.json");
const DEFAULT_OUT = path.join(ROOT_DIR, "bom", "bom.sqlite");

/* ------------------------------------------------------------------ 参数解析 */

function parseArgs(argv) {
  const command = argv[0] ?? "import";
  const opts = {
    in: DEFAULT_IN,
    out: DEFAULT_OUT,
    limit: 20,
    dataType: undefined,
    includeMethods: false,
    json: false,
    drop: false,
    sql: undefined,
    arg: undefined,
  };
  for (let i = 1; i < argv.length; i++) {
    const token = argv[i];
    const next = () => argv[++i];
    switch (token) {
      case "--in": opts.in = next(); break;
      case "--out": opts.out = next(); break;
      case "--limit": opts.limit = Number(next()); break;
      case "--data-type": opts.dataType = next(); break;
      case "--include-methods": opts.includeMethods = true; break;
      case "--json": opts.json = true; break;
      case "--drop": opts.drop = true; break;
      case "--sql": opts.sql = next(); break;
      default:
        if (token.startsWith("-")) throw new Error(`未知参数：${token}`);
        opts.arg = opts.arg === undefined ? token : `${opts.arg} ${token}`;
    }
  }
  return { command, opts };
}

/* ------------------------------------------------------------------------ DDL */

const SCHEMA = `
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS fields (
  id                INTEGER PRIMARY KEY,
  field_id          TEXT UNIQUE,                    -- 原 fieldId（全局唯一）
  row_key           TEXT UNIQUE,                    -- fieldPath#重载序号
  model_name        TEXT,                           -- path 第一段：BOM 模型实例名
  field             TEXT,
  key               TEXT,
  field_path        TEXT,                           -- result.apInfos.apInfoProcessed
  label             TEXT,                           -- 精算处理
  type              TEXT,                           -- boolean/string/double/list/object...
  data_type         TEXT,                           -- field/method/object/list
  class_path        TEXT,                           -- com.firsttech.insure.xom.ApInfoXom
  class_name        TEXT,                           -- ApInfoXom
  collection_type   TEXT,
  code_service      TEXT,                           -- 码值服务
  annotation_name   TEXT,
  required_field    INTEGER,
  a_field           INTEGER,
  has_children      INTEGER,
  actual_types      TEXT,                           -- JSON 数组
  actual_type_names TEXT,                           -- JSON 数组
  source_path       TEXT,                           -- 原 path
  overload_index    INTEGER DEFAULT 0,
  depth             INTEGER,
  ordinal           INTEGER,                        -- 同级中的次序
  parent_field_id   TEXT REFERENCES fields(field_id),
  parent_field      TEXT,
  parent_key        TEXT,
  parent_field_path TEXT,
  parent_label      TEXT,                           -- 精算险别信息列表
  parent_class_path TEXT,
  ancestor_labels   TEXT,                           -- 结果 > 精算险别信息列表
  ancestor_paths    TEXT,                           -- result > result.apInfos
  top_label         TEXT,                           -- 第一层祖先 label（结果/动态属性...）
  is_method         INTEGER,
  is_container      INTEGER,                        -- object/list/mapxom
  leaf_count        INTEGER,
  search_text       TEXT
);

CREATE TABLE IF NOT EXISTS params (                                  -- detailsList：方法入参
  id           INTEGER PRIMARY KEY,
  field_id     TEXT REFERENCES fields(field_id),
  param_id     TEXT,                                                -- param1
  ref_id       TEXT,                                                -- MIGRATE_SETSPECIAL_ADJUSTMENT_FACTOR_0
  type         TEXT,
  ordinal      INTEGER
);

CREATE TABLE IF NOT EXISTS classes (                                 -- 按 Java 类聚合
  class_path    TEXT PRIMARY KEY,
  class_name    TEXT,
  node_count    INTEGER,
  field_count   INTEGER,
  method_count  INTEGER,
  sample_labels TEXT
);

CREATE TABLE IF NOT EXISTS code_services (                           -- 按码值服务聚合
  name              TEXT PRIMARY KEY,
  field_count       INTEGER,
  sample_field_paths TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_fields_label        ON fields(label);
CREATE INDEX IF NOT EXISTS idx_fields_field_path   ON fields(field_path);
CREATE INDEX IF NOT EXISTS idx_fields_parent       ON fields(parent_field_id);
CREATE INDEX IF NOT EXISTS idx_fields_class        ON fields(class_path);
CREATE INDEX IF NOT EXISTS idx_fields_data_type    ON fields(data_type);
CREATE INDEX IF NOT EXISTS idx_fields_type         ON fields(type);
CREATE INDEX IF NOT EXISTS idx_params_field        ON params(field_id);

CREATE VIEW IF NOT EXISTS v_business_fields AS
  SELECT label, field, field_path, type, data_type, class_name, collection_type,
         required_field, parent_label, ancestor_labels, top_label
  FROM fields WHERE is_method = 0;

CREATE VIEW IF NOT EXISTS v_field_tree AS
  SELECT depth, ordinal, label, field_path, type, data_type, class_name,
         parent_label, ancestor_labels, leaf_count
  FROM fields ORDER BY id;
`;

const FTS_SQL = `CREATE VIRTUAL TABLE IF NOT EXISTS fts_fields USING fts5(
  label, field, field_path, class_name, code_service, search_text,
  id UNINDEXED, tokenize = 'trigram'
)`;

/* --------------------------------------------------------------------- 导入 */

function shortName(classPath) {
  if (!classPath) return undefined;
  const idx = classPath.lastIndexOf(".");
  return idx === -1 ? classPath : classPath.slice(idx + 1);
}

function buildRows(root) {
  const rows = [];
  const seen = new Map();                       // fieldPath -> 重载次数
  const byRowKey = new Map();                   // row_key -> 行对象（用于回填 leaf_count）
  const classAgg = new Map();
  const serviceAgg = new Map();

  const visit = (node, parent, depth, ordinal, ancestorLabels, ancestorPaths, topLabel) => {
    const children = Array.isArray(node.children) ? node.children : [];
    const isRoot = parent === null;
    const fp = String(node.fieldPath ?? "");
    const overload = seen.get(fp) ?? 0;
    seen.set(fp, overload + 1);

    const className = shortName(node.classPath);
    const pathHead = typeof node.path === "string" ? node.path.split(".")[0] : undefined;

    if (!isRoot) {
      const dataType = node.dataType ?? null;
      const row = {
        field_id: node.fieldId ?? `${fp}#${overload}`,
        row_key: `${fp}#${overload}`,
        model_name: pathHead,
        field: node.field ?? null,
        key: node.key ?? null,
        field_path: fp || null,
        label: node.label ?? null,
        type: node.type ?? null,
        data_type: dataType,
        class_path: node.classPath ?? null,
        class_name: className ?? null,
        collection_type: node.collectionType ?? null,
        code_service: node.codeService ?? null,
        annotation_name: node.annotationName ?? null,
        required_field: bool(node.requiredField),
        a_field: bool(node.afield),
        has_children: bool(node.hasChildren),
        actual_types: jsonOrNull(node.actualTypes),
        actual_type_names: jsonOrNull(node.actualTypeNames),
        source_path: node.path ?? null,
        overload_index: overload,
        depth,
        ordinal,
        parent_field_id: parent.fieldId ?? null,
        parent_field: parent.field ?? null,
        parent_key: parent.key ?? null,
        parent_field_path: parent.fieldPath ?? null,
        parent_label: parent.label ?? null,
        parent_class_path: parent.classPath ?? null,
        ancestor_labels: ancestorLabels.join(" > ") || null,
        ancestor_paths: ancestorPaths.join(" > ") || null,
        top_label: topLabel ?? node.label ?? null,
        is_method: dataType === "method" ? 1 : 0,
        is_container: ["object", "list", "mapxom", "map"].includes(node.type) ? 1 : 0,
        leaf_count: 0,
        search_text: null,
        params: (Array.isArray(node.detailsList) ? node.detailsList : []).map((p, i) => ({
          param_id: p?.paramId ?? null,
          ref_id: p?.id ?? null,
          type: p?.type ?? null,
          ordinal: i,
        })),
      };
      row.search_text = [row.label, row.field, row.field_path, row.class_name, row.code_service,
        row.annotation_name, jsonText(node.actualTypeNames), collectionHint(row.collection_type), dataType]
        .filter(Boolean).join(" ");
      rows.push(row);
      byRowKey.set(row.row_key, row);

      if (row.class_path) {
        const agg = classAgg.get(row.class_path) ?? { node_count: 0, field_count: 0, method_count: 0, labels: [] };
        agg.node_count++;
        if (row.is_method) agg.method_count++; else agg.field_count++;
        if (agg.labels.length < 5 && row.label) agg.labels.push(row.label);
        classAgg.set(row.class_path, agg);
      }
      if (row.code_service) {
        const agg = serviceAgg.get(row.code_service) ?? { field_count: 0, paths: [] };
        agg.field_count++;
        if (agg.paths.length < 5 && row.field_path) agg.paths.push(row.field_path);
        serviceAgg.set(row.code_service, agg);
      }
    }

    const nextAncestorLabels = isRoot ? [] : [...ancestorLabels, node.label ?? node.field ?? ""];
    const nextAncestorPaths = isRoot ? [] : [...ancestorPaths, node.fieldPath ?? node.field ?? ""];
    const nextTop = isRoot ? undefined : (topLabel ?? (node.label ?? node.field));

    children.forEach((child, i) => visit(child, node, depth + 1, i, nextAncestorLabels, nextAncestorPaths, nextTop));

    const self = isRoot ? undefined : byRowKey.get(`${fp}#${overload}`);
    if (self) self.leaf_count = countLeaves(node);
  };

  visit(root, null, 0, 0, [], [], undefined);
  return { rows, classAgg, serviceAgg };
}

function countLeaves(node) {
  const children = Array.isArray(node.children) ? node.children : [];
  if (children.length === 0) return 1;
  return children.reduce((sum, c) => sum + countLeaves(c), 0);
}

function collectionHint(t) {
  if (!t) return null;
  return t === "list" ? "列表集合元素" : t === "map" ? "映射集合" : String(t);
}

const bool = (v) => (v === undefined || v === null ? null : v ? 1 : 0);
const jsonOrNull = (v) => (Array.isArray(v) && v.length ? JSON.stringify(v) : null);
const jsonText = (v) => (Array.isArray(v) ? v.join(" ") : null);

function importBom(opts) {
  const inFile = path.resolve(opts.in);
  const outFile = path.resolve(opts.out);
  if (!fs.existsSync(inFile)) throw new Error(`找不到输入文件：${inFile}`);
  if (opts.drop && fs.existsSync(outFile)) {
    for (const suffix of ["", "-wal", "-shm"]) fs.rmSync(outFile + suffix, { force: true });
  }

  process.stderr.write(`读取 ${inFile} ... `);
  const raw = fs.readFileSync(inFile, "utf8");
  const root = JSON.parse(raw);
  process.stderr.write(`${(raw.length / 1024 / 1024).toFixed(1)} MB\n`);

  const { rows, classAgg, serviceAgg } = buildRows(root);
  const db = new DatabaseSync(outFile);
  db.exec(SCHEMA);

  let ftsReady = true;
  try { db.exec(FTS_SQL); } catch (e) { ftsReady = false; process.stderr.write(`FTS5 trigram 不可用，退回 LIKE 检索：${e.message}\n`); }

  db.exec("BEGIN");
  db.exec("DELETE FROM fields; DELETE FROM params; DELETE FROM classes; DELETE FROM code_services; DELETE FROM meta;");
  if (ftsReady) db.exec("DELETE FROM fts_fields;");

  const insertField = db.prepare(`INSERT INTO fields (
      field_id, row_key, model_name, field, key, field_path, label, type, data_type, class_path, class_name,
      collection_type, code_service, annotation_name, required_field, a_field, has_children, actual_types,
      actual_type_names, source_path, overload_index, depth, ordinal, parent_field_id, parent_field, parent_key,
      parent_field_path, parent_label, parent_class_path, ancestor_labels, ancestor_paths, top_label,
      is_method, is_container, leaf_count, search_text
    ) VALUES (${Array(36).fill("?").join(",")})`);
  const insertFts = ftsReady ? db.prepare("INSERT INTO fts_fields VALUES (?,?,?,?,?,?,?)") : null;
  const insertParam = db.prepare("INSERT INTO params (field_id, param_id, ref_id, type, ordinal) VALUES (?,?,?,?,?)");
  const insertClass = db.prepare("INSERT INTO classes VALUES (?,?,?,?,?,?)");
  const insertService = db.prepare("INSERT INTO code_services VALUES (?,?,?)");
  const insertMeta = db.prepare("INSERT OR REPLACE INTO meta VALUES (?,?)");

  const cols = ["field_id", "row_key", "model_name", "field", "key", "field_path", "label", "type", "data_type",
    "class_path", "class_name", "collection_type", "code_service", "annotation_name", "required_field", "a_field",
    "has_children", "actual_types", "actual_type_names", "source_path", "overload_index", "depth", "ordinal",
    "parent_field_id", "parent_field", "parent_key", "parent_field_path", "parent_label", "parent_class_path",
    "ancestor_labels", "ancestor_paths", "top_label", "is_method", "is_container", "leaf_count", "search_text"];

  for (const row of rows) {
    insertField.run(...cols.map((c) => row[c] ?? null));
    const id = Number(insertField.lastInsertRowid);
    if (insertFts) insertFts.run(row.label ?? "", row.field ?? "", row.field_path ?? "", row.class_name ?? "",
      row.code_service ?? "", row.search_text ?? "", id);
    for (const p of row.params) insertParam.run(row.field_id, p.param_id, p.ref_id, p.type, p.ordinal);
  }
  for (const [classPath, agg] of classAgg) {
    insertClass.run(classPath, shortName(classPath), agg.node_count, agg.field_count, agg.method_count, agg.labels.join("、"));
  }
  for (const [name, agg] of serviceAgg) {
    insertService.run(name, agg.field_count, agg.paths.join(" | "));
  }

  const models = [...new Set(rows.map((r) => r.model_name).filter(Boolean))];
  insertMeta.run("source_file", inFile);
  insertMeta.run("model_name", models[0] ?? "");
  insertMeta.run("model_names", models.join(", "));
  insertMeta.run("root_label", root.label ?? "");
  insertMeta.run("node_count", String(rows.length));
  insertMeta.run("field_count", String(rows.filter((r) => r.is_method === 0).length));
  insertMeta.run("method_count", String(rows.filter((r) => r.is_method === 1).length));
  insertMeta.run("max_depth", String(Math.max(0, ...rows.map((r) => r.depth))));
  insertMeta.run("imported_at", new Date().toISOString());
  insertMeta.run("fts_trigram", ftsReady ? "1" : "0");
  db.exec("COMMIT");
  db.exec("ANALYZE");
  db.close();

  process.stderr.write(`已写入 ${outFile}\n`);
  printStats(outFile);
}

/* --------------------------------------------------------------------- 查询 */

function openDb(opts) {
  const outFile = path.resolve(opts.out);
  if (!fs.existsSync(outFile)) throw new Error(`数据库不存在，请先执行 import：${outFile}`);
  return new DatabaseSync(outFile, { readOnly: true });
}

const OUTPUT_COLS = `f.label, f.type, f.data_type AS dataType, f.field_path AS fieldPath,
  f.class_name AS className, f.parent_label AS parentLabel, f.ancestor_labels AS ancestors,
  f.required_field AS required, f.code_service AS codeService, f.field_id AS fieldId`;

function search(db, keyword, opts) {
  const clauses = [];
  const params = [];
  if (!opts.includeMethods) clauses.push("f.is_method = 0");
  if (opts.dataType) { clauses.push("f.data_type = ?"); params.push(opts.dataType); }
  const where = clauses.length ? ` AND ${clauses.join(" AND ")}` : "";
  const like = `%${keyword.replace(/[%_]/g, (c) => "\\" + c)}%`;

  let rows;
  const hasFts = db.prepare("SELECT 1 FROM sqlite_master WHERE name = 'fts_fields'").get();
  if (hasFts && keyword.length >= 3) {
    // 用 rowid 而非 fts 的 id 列做 JOIN：老版本 import 建的库 id 列全为 NULL，
    // 而两表按同一顺序插入，rowid 与 fields.id 恒等（新版 import 填了 id 后此写法依然成立）
    rows = db.prepare(`SELECT ${OUTPUT_COLS} FROM fts_fields
      JOIN fields f ON f.id = fts_fields.rowid
      WHERE fts_fields MATCH ?${where}
      ORDER BY bm25(fts_fields, 5.0, 3.0, 2.0, 1.0, 1.0, 1.0) LIMIT ?`)
      .all(quoteMatch(keyword), ...params, opts.limit);
  }
  if (!rows || rows.length === 0) {
    rows = db.prepare(`SELECT ${OUTPUT_COLS} FROM fields f
      WHERE (f.label LIKE ? ESCAPE '\\' OR f.field LIKE ? ESCAPE '\\' OR f.field_path LIKE ? ESCAPE '\\'
             OR f.class_name LIKE ? ESCAPE '\\' OR f.search_text LIKE ? ESCAPE '\\')${where}
      ORDER BY f.is_method, f.depth LIMIT ?`)
      .all(like, like, like, like, like, ...params, opts.limit);
  }
  return rows;
}

// trigram 索引要求短语加引号，避免被解析成 FTS 查询语法
function quoteMatch(keyword) { return `"${keyword.replaceAll('"', '""')}"`; }

function nodeInfo(db, fieldPath) {
  const row = db.prepare("SELECT * FROM fields WHERE field_path = ? ORDER BY is_method, overload_index LIMIT 1").get(fieldPath);
  if (!row) throw new Error(`未找到字段：${fieldPath}`);
  return row;
}

function parents(db, fieldPath) {
  const chain = [];
  const select = db.prepare("SELECT * FROM fields WHERE field_id = ?");
  let cur = nodeInfo(db, fieldPath);
  while (cur.parent_field_id) {
    cur = select.get(cur.parent_field_id);
    if (!cur) break;
    chain.push(pick(cur));
  }
  return chain;
}

function children(db, fieldPath, limit) {
  const parent = nodeInfo(db, fieldPath);
  return db.prepare(`SELECT ${OUTPUT_COLS}, f.leaf_count AS leafCount FROM fields f
    WHERE f.parent_field_id = ? ORDER BY f.is_method, f.ordinal LIMIT ?`).all(parent.field_id, limit);
}

const pick = (r) => ({
  label: r.label, fieldPath: r.field_path, type: r.type, dataType: r.data_type, className: r.class_name,
  parentLabel: r.parent_label, ancestors: r.ancestor_labels, depth: r.depth,
});

// 去掉 null/undefined，让 info 输出紧凑可读
const compact = (row) => Object.fromEntries(Object.entries(row).filter(([, v]) => v !== null && v !== undefined));
const compactAll = (rows) => rows.map(compact);

function printStats(outFile) {
  const db = openDb({ out: outFile });
  const q = (sql) => db.prepare(sql).all();
  const one = (sql) => db.prepare(sql).get();
  const meta = Object.fromEntries(q("SELECT key, value FROM meta").map((r) => [r.key, r.value]));
  const lines = [];
  lines.push(`数据库 ${outFile}`);
  lines.push(`模型 ${meta.model_name}   根节点 ${meta.root_label}   生成时间 ${meta.imported_at}`);
  lines.push(`节点总数 ${meta.node_count}（业务字段 ${meta.field_count} / 方法 ${meta.method_count}） 最大层级 ${meta.max_depth}`);
  lines.push("按 dataType：" + q("SELECT data_type k, count(*) c FROM fields GROUP BY 1 ORDER BY c DESC").map((r) => `${r.k}=${r.c}`).join(" "));
  lines.push("按 type：" + q("SELECT type k, count(*) c FROM fields GROUP BY 1 ORDER BY c DESC LIMIT 12").map((r) => `${r.k}=${r.c}`).join(" "));
  lines.push(`Java 类 ${one("SELECT count(*) c FROM classes").c} 个，码值服务 ${one("SELECT count(*) c FROM code_services").c} 个，方法入参 ${one("SELECT count(*) c FROM params").c} 条，FTS 中文检索 ${meta.fts_trigram === "1" ? "可用" : "不可用"}`);
  lines.push("顶层分组：" + q("SELECT top_label k, count(*) c FROM fields WHERE depth = 1 GROUP BY 1 ORDER BY c DESC LIMIT 10").map((r) => `${r.k ?? "-"}(${r.c})`).join("  "));
  lines.push("一级容器（父节点示例）：" + q("SELECT label, field_path FROM fields WHERE depth = 1 ORDER BY ordinal LIMIT 6").map((r) => `${r.label}[${r.field_path}]`).join("  "));
  db.close();
  process.stdout.write(lines.join("\n") + "\n");
}

function render(rows, json) {
  if (json) { process.stdout.write(JSON.stringify(rows, null, 2) + "\n"); return; }
  if (!rows.length) { process.stdout.write("（无匹配）\n"); return; }
  for (const r of rows) {
    const extras = [r.required === 1 ? "必填" : null, r.codeService ? `码值:${r.codeService}` : null].filter(Boolean).join(",");
    process.stdout.write(
      `${r.label}\n  path: ${r.fieldPath}\n  type: ${r.type}  dataType: ${r.dataType}  class: ${r.className ?? "-"}` +
      `${extras ? "  " + extras : ""}\n  parent: ${r.parentLabel ?? "-"}   ancestors: ${r.ancestors ?? "-"}\n\n`);
  }
}

/* ------------------------------------------------------------------------ 主 */

function main() {
  const { command, opts } = parseArgs(process.argv.slice(2));
  switch (command) {
    case "import": importBom(opts); break;
    case "stats": printStats(path.resolve(opts.out)); break;
    case "sql": {
      const db = openDb(opts);
      const sql = opts.arg ?? opts.sql;
      if (!/^\s*select/i.test(sql ?? "")) throw new Error("sql 只接受 SELECT 语句");
      process.stdout.write(JSON.stringify(compactAll(db.prepare(sql).all()), null, 2) + "\n");
      db.close();
      break;
    }
    case "search": {
      if (!opts.arg) throw new Error("search 需要关键词，例如：node tools/bom-to-sqlite.mjs search 精算");
      const db = openDb(opts);
      render(search(db, opts.arg, opts), opts.json);
      db.close();
      break;
    }
    case "info": {
      const db = openDb(opts);
      const row = compact(nodeInfo(db, opts.arg));
      const kids = db.prepare("SELECT label, field_path, type, data_type FROM fields WHERE parent_field_id = ? ORDER BY is_method, ordinal LIMIT 30").all(row.field_id);
      process.stdout.write(JSON.stringify({ ...row, children: compactAll(kids) }, null, 2) + "\n");
      db.close();
      break;
    }
    case "parents": {
      const db = openDb(opts);
      const chain = [...parents(db, opts.arg).reverse(), pick(nodeInfo(db, opts.arg))];
      process.stdout.write(chain.map((c) => `${"  ".repeat(c.depth ?? 0)}${c.label}  [${c.fieldPath}]`).join("\n") + "\n");
      db.close();
      break;
    }
    case "children": {
      const db = openDb(opts);
      render(children(db, opts.arg, opts.limit), opts.json);
      db.close();
      break;
    }
    default:
      throw new Error(`未知命令：${command}\n可用命令：import / stats / search / info / parents / children / sql`);
  }
}

try { main(); } catch (err) { process.stderr.write(`错误：${err.message}\n`); process.exitCode = 1; }
