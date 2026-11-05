#!/usr/bin/env node
/**
 * 规则检索核心（数据源：MySQL ya_car，只读）
 *
 * 链路（一个 RULE_ID 对应一条规则）：
 *   decision_repository（BOM_BASE_INFO_ID → DECISION_REPOSITORY_ID）
 *     → decision_rep_catalogue（→ DECISION_REP_CATALOGUE_ID）
 *       → catalogue_rule_rel（→ RULE_ID）
 *         → rule_defined（RULE_NAME / RULE_DESC 即「如果」原文，含 HTML）
 *         → rule_defined_predefine（RULE_PREDEFINE_DESC 即「预定义」）
 *
 *         → rule_method_define（SETTING_THEN 1=那么 / 0=否则，method_id，ORDER_BY 执行顺序）
 *           → rule_method（METHOD_NAME 即动作文本，如「设置商业险手续费率为」）
 *           → rule_method_define_param（PARAM_ID / PARAM_VALUE，一个动作可有多个参数）
 *
 * 检索方式：空格分词，词间「且」关系；每个词在 规则名 / 如果原文 / 预定义 任一位置出现即命中。
 * 「那么/否则」不参与关键词检索，命中规则后按 RULE_ID 直接带出（rule_defined.rule_content 里的
 * JSON AST 仅在 --content 时取，作为核对用）。
 *
 * 取 METHOD_NAME 必须防行放大：rule_method 按 (METHOD_ID, BOM_MODEL_ID) 存多版本，实测 146628 行
 * 只有 1592 个 METHOD_ID，裸 JOIN 会把 1 个动作放大成几十行。故先按当前 BOM 范围内的
 * BOM_MODEL_ID 过滤再 GROUP BY METHOD_ID 取 MIN(METHOD_NAME)；范围外或查不到的再兜底查一次。
 *
 * 该文件不依赖 pi，可独立命令行验证：
 *   node tools/rule-search.mjs 手续费率
 *   node tools/rule-search.mjs 车龄 违章 --limit 3
 *   node tools/rule-search.mjs 交强险 --json --content
 *   node tools/rule-search.mjs --ping
 *   node tools/rule-search.mjs --stats
 *
 * 连接信息与默认 bom_base_info_id 在 config/mysql.json，可用环境变量覆盖：
 *   RULE_MYSQL_HOST / RULE_MYSQL_PORT / RULE_MYSQL_USER / RULE_MYSQL_PASSWORD /
 *   RULE_MYSQL_DATABASE / RULE_BOM_BASE_INFO_ID
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = path.resolve(HERE, "..");
export const CONFIG_PATH = path.join(PROJECT_ROOT, "config", "mysql.json");

export const DEFAULT_LIMIT = 6;
export const MAX_LIMIT = 30;
/** 单次从 MySQL 取回的最大行数：先超量取，JS 打分排序后再切 limit，避免 SQL 无法表达相关度。 */
const MAX_FETCH_ROWS = 150;

/**
 * MySQL 侧只读保护：SELECT 级超时 hint，避免慢查询把工具调用吊死。
 * 库本身按只读使用，本文件不发出任何写语句。
 */
const READ_TIMEOUT_MS = 15000;
const HINT = `/*+ MAX_EXECUTION_TIME(${READ_TIMEOUT_MS}) */`;

/**
 * 失败重试默认参数（可被 config/mysql.json 的 ruleSearch.retry 覆盖）。
 * 只对「瞬时/连接类」错误重试；SQL 语法、权限、缺列等确定性错误重试无意义，直接失败。
 * 注意：baseDelayMs 是「两次尝试之间的暂停」，不是查询耗时预算——查询本身慢（本项目主查询约 2s）
 * 不影响它，只有失败时才 sleep。取值按「给数据库恢复留窗口」设计：慢库/重启场景 600ms 起步比 300ms 更实际。
 */
export const DEFAULT_RETRY = { maxAttempts: 3, baseDelayMs: 600, maxDelayMs: 4000 };

/**
 * 值得重试的 MySQL / Node 网络层错误码：连接被拒、超时、握手/掉线、连接数打满、锁等待。
 * 这些都是「稍后再来大概率成功」的抖动；其余错误码（如 ER_NO_SUCH_TABLE、ER_ACCESS_DENIED）不在此列。
 */
const RETRYABLE_ERROR_CODES = new Set([
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ENOTFOUND",
  "EAI_AGAIN",
  "ECONNRESET",
  "EPIPE",
  "PROTOCOL_CONNECTION_LOST",
  "PROTOCOL_SEQUENCE_TIMEOUT",
  "ER_CON_COUNT_ERROR",
  "ER_SERVER_SHUTDOWN",
  "LOCK_WAIT_TIMEOUT",
  "ER_LOCK_WAIT_TIMEOUT",
  "ER_LOCK_DEADLOCK",
]);

/** 从连接池错误里取真实错误码：mysql2 有的码在 err.code，有的在 err.sqlMessage 前缀，握手失败还有 err.errno。 */
function extractErrorCode(err) {
  if (!err) return "";
  if (typeof err.code === "string") return err.code;
  if (typeof err.errno === "number" && err.sqlMessage) return err.sqlMessage;
  return "";
}

/** 是否为可重试的瞬时错误。非 Error（字符串/对象）一律按不可重试处理，避免吞掉逻辑 bug。 */
export function isRetryableError(err) {
  return RETRYABLE_ERROR_CODES.has(extractErrorCode(err));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 带指数退避的重试执行器。
 * @template T
 * @param {() => Promise<T>} fn 需幂等的只读操作（本项目全部是 SELECT）
 * @param {{maxAttempts?:number, baseDelayMs?:number, maxDelayMs?:number}} [opts]
 * @returns {Promise<T>} 某次成功的结果
 * @throws 重试耗尽时抛最后一次的原始错误（附带 `__retryAttempts`），由调用方决定措辞
 */
export async function withRetry(fn, opts = {}) {
  const maxAttempts = Math.max(1, Math.floor(opts.maxAttempts ?? DEFAULT_RETRY.maxAttempts));
  const baseDelayMs = Math.max(0, opts.baseDelayMs ?? DEFAULT_RETRY.baseDelayMs);
  const maxDelayMs = Math.max(baseDelayMs, opts.maxDelayMs ?? DEFAULT_RETRY.maxDelayMs);

  let lastErr;
  let attemptsUsed = 0;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    attemptsUsed = attempt;
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      // 确定性错误（语法/权限/缺表）不重试；已到最后一次也不重试。
      if (!isRetryableError(err) || attempt >= maxAttempts) break;
      // 指数退避 + 抖动：base * 2^(attempt-1)，夹在 [baseDelayMs, maxDelayMs]，再叠 0~30% 随机抖动避免惊群。
      const backoff = Math.min(baseDelayMs * 2 ** (attempt - 1), maxDelayMs);
      const jitter = Math.floor(backoff * 0.3 * Math.random());
      await sleep(backoff + jitter);
    }
  }
  // 重试用尽：抛出最后一次的「原始错误」并记录实际尝试次数，交由调用方统一措辞（不在此处二次包装，
  // 否则错误文案被套两层）。非 Error 值兜底包一层，保证调用方 catch 到的始终是 Error。
  if (lastErr && typeof lastErr === "object") lastErr.__retryAttempts = attemptsUsed;
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

let configCache = null;
let poolCache = null;

/** 读取 config/mysql.json 并套用环境变量覆盖。 */
export function loadConfig({ reload = false } = {}) {
  if (configCache && !reload) return configCache;
  if (!fs.existsSync(CONFIG_PATH)) {
    throw new Error(`配置文件不存在：${CONFIG_PATH}\n请创建该文件（字段见 tools/rule-search.mjs 顶部注释）`);
  }
  const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const env = process.env;
  const mysql = {
    host: env.RULE_MYSQL_HOST ?? raw.mysql?.host,
    port: Number(env.RULE_MYSQL_PORT ?? raw.mysql?.port ?? 3306),
    user: env.RULE_MYSQL_USER ?? raw.mysql?.user,
    password: env.RULE_MYSQL_PASSWORD ?? raw.mysql?.password,
    database: env.RULE_MYSQL_DATABASE ?? raw.mysql?.database,
    connectTimeout: Number(raw.mysql?.connectTimeoutMs ?? 10000),
  };
  if (!mysql.host || !mysql.user || !mysql.database) {
    throw new Error(`${CONFIG_PATH} 缺少 mysql.host/user/database 字段`);
  }
  const ruleSearch = {
    bomBaseInfoId: env.RULE_BOM_BASE_INFO_ID ?? raw.ruleSearch?.bomBaseInfoId,
    bomBaseInfoName: raw.ruleSearch?.bomBaseInfoName ?? "未命名 BOM",
    defaultLimit: Number(raw.ruleSearch?.defaultLimit ?? DEFAULT_LIMIT),
    maxLimit: Number(raw.ruleSearch?.maxLimit ?? MAX_LIMIT),
    fetchFactor: Number(raw.ruleSearch?.fetchFactor ?? 5),
    retry: {
      maxAttempts: Number(raw.ruleSearch?.retry?.maxAttempts ?? DEFAULT_RETRY.maxAttempts),
      baseDelayMs: Number(raw.ruleSearch?.retry?.baseDelayMs ?? DEFAULT_RETRY.baseDelayMs),
      maxDelayMs: Number(raw.ruleSearch?.retry?.maxDelayMs ?? DEFAULT_RETRY.maxDelayMs),
    },
  };
  if (!ruleSearch.bomBaseInfoId) {
    throw new Error(`${CONFIG_PATH} 缺少 ruleSearch.bomBaseInfoId 字段`);
  }
  configCache = { mysql, ruleSearch };
  return configCache;
}

export const DEFAULT_BOM_BASE_INFO_ID = (() => {
  try {
    return loadConfig().ruleSearch.bomBaseInfoId;
  } catch {
    return null;
  }
})();

/** 懒建并复用连接池（pi 扩展内常驻，省掉每次约 180ms 的握手）。 */
export async function getPool() {
  if (poolCache) return poolCache;
  const { mysql } = loadConfig();
  const mysql2 = await import("mysql2/promise");
  poolCache = mysql2.createPool({
    host: mysql.host,
    port: mysql.port,
    user: mysql.user,
    password: mysql.password,
    database: mysql.database,
    connectTimeout: mysql.connectTimeout,
    waitForConnections: true,
    connectionLimit: 2,
    queueLimit: 0,
    charset: "utf8mb4",
    multipleStatements: false,
    enableKeepAlive: true,
  });
  return poolCache;
}

export async function closePool() {
  if (!poolCache) return;
  const pool = poolCache;
  poolCache = null;
  await pool.end();
}

const escapeLike = (v) => v.replace(/[\\%_]/g, (c) => "\\" + c);
const likeWrap = (v) => `%${escapeLike(v)}%`;

/** 去掉 RULE_DESC 里的富文本标签（实测 68935/70301 条规则含 <span class="fields"> 之类标记）。 */
export function stripHtml(raw) {
  return String(raw ?? "")
    .replace(/<\s*br\s*\/?\s*>/gi, "\n")
    .replace(/<\/\s*(p|div|li|ul|ol|tr)\s*>/gi, "\n")
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&amp;/gi, "&")
    .replace(/[ \t\u3000]+/g, " ")
    .replace(/\n\s*\n\s*\n+/g, "\n\n")
    .trim();
}

/**
 * 原文里可能自带引导词（库里有些 RULE_PREDEFINE_DESC 存的是一整段带「预定义:」的文案），
 * 输出时剥掉行首重复段名，避免出现「预定义: 预定义: xxx」。
 */
const LEAD_LABEL_RE = /^\s*(?:预定义|如果|那么|否则)\s*[:：]\s*/;

/**
 * 把一段原文压成单行。必须压：formatResults 用「行首标签」划分段落，
 * 原文带裸换行（stripHtml 把 <br>/</p> 转成 \n）会让续行失去归属，
 * 模型分不清这行还属于「如果」还是新段落，抄写时就会串行或漏句。
 */
export function flattenLine(raw) {
  return String(raw ?? "")
    .replace(LEAD_LABEL_RE, "")
    .replace(/\s*\n\s*/g, " ")
    .replace(/[ \t\u3000]+/g, " ")
    .trim();
}

/** 三跳链路（只按 BOM_BASE_INFO_ID 圈定范围），与主查询同库无需前缀。 */
const RULE_ID_SCOPE = `rd.RULE_ID IN (
      SELECT crr.RULE_ID
      FROM catalogue_rule_rel crr
      JOIN decision_rep_catalogue dc ON dc.DECISION_REP_CATALOGUE_ID = crr.DECISION_REP_CATALOGUE_ID
      JOIN decision_repository dr ON dr.DECISION_REPOSITORY_ID = dc.DECISION_REPOSITORY_ID
      WHERE dr.BOM_BASE_INFO_ID = ?)`;

/** 单个关键词的命中条件：规则名 / 如果原文 / 预定义，任一出现即可。 */
const WORD_HIT = `(rd.RULE_NAME LIKE ? ESCAPE '\\\\'
         OR rd.RULE_DESC LIKE ? ESCAPE '\\\\'
         OR EXISTS (SELECT 1 FROM rule_defined_predefine pm
                    WHERE pm.RULE_ID = rd.RULE_ID AND pm.RULE_PREDEFINE_DESC LIKE ? ESCAPE '\\\\'))`;

/**
 * 按关键词检索规则。
 * @param {string} keyword 空格分词，词间「且」
 * @param {{limit?:number, bomBaseInfoId?:string, includeContent?:boolean}} [options]
 */
export async function searchRules(keyword, options = {}) {
  const cfg = loadConfig();
  const words = String(keyword ?? "").trim().split(/\s+/).filter(Boolean);
  const limit = normalizeLimit(options.limit, cfg.ruleSearch);
  const bomBaseInfoId = options.bomBaseInfoId ?? cfg.ruleSearch.bomBaseInfoId;
  const scope = `${cfg.ruleSearch.bomBaseInfoName}（BOM_BASE_INFO_ID=${bomBaseInfoId}）`;

  if (!words.length) {
    return { words, limit, scope, items: [], message: "关键词为空，请提供规则业务词（如：手续费率、车龄、违章），空格分隔表示同时满足。" };
  }

  const pool = await getPool();
  const started = Date.now();

  const where = words.map(() => WORD_HIT).join("\n      AND ");
  /**
   * SQL 侧取回排序键：按字段加权的命中面（名称 9 > 如果原文 4 > 预定义 2）。
   *
   * WHERE 已是词间「且」，能通过过滤的行必然含全部关键词，所以这里要分辨的不再是
   * 「是否命中」而是「命中落在哪些字段、覆盖几个词」。原先只数名称命中：名称不含词的
   * 候选一律并列 0，取回额度（fetchLimit）被打满时退化成按 RULE_ID 随机截断，
   * 「如果」原文强相关的规则会在打分前就被丢掉——JS 侧 scoreRule 再准也救不回来。
   *
   * 权重比 9:4:2 取自 scoreRule 的 名称45 / 如果20 / 预定义10，让 SQL 粗排与 JS 精排
   * 口径一致；两个 LIKE 字段都用 IFNULL 兜住 NULL，否则布尔值变 NULL 会传染整个
   * 表达式、使这些行在 DESC 下无谓沉底。
   */
  const recallScore = words
    .map(() => `9 * (IFNULL(rd.RULE_NAME, '') LIKE ? ESCAPE '\\\\')
              + 4 * (IFNULL(rd.RULE_DESC, '') LIKE ? ESCAPE '\\\\')
              + 2 * EXISTS (SELECT 1 FROM rule_defined_predefine pm
                            WHERE pm.RULE_ID = rd.RULE_ID AND pm.RULE_PREDEFINE_DESC LIKE ? ESCAPE '\\\\')`)
    .join("\n              + ");
  const params = [bomBaseInfoId];
  for (const w of words) params.push(likeWrap(w), likeWrap(w), likeWrap(w));
  const scoreParams = [];
  for (const w of words) scoreParams.push(likeWrap(w), likeWrap(w), likeWrap(w));
  const fetchLimit = Math.min(MAX_FETCH_ROWS, Math.max(limit * cfg.ruleSearch.fetchFactor, 30));

  const rows = await queryRows(pool, `
    SELECT ${HINT}
           rd.RULE_ID AS ruleId, rd.RULE_NAME AS ruleName, rd.RULE_DESC AS ifHtml,
           rd.RULE_TIPS AS ruleTips, rd.UPDATED_DATE AS updatedDate
           ${options.includeContent ? ", rd.rule_content AS contentJson" : ""}
    FROM rule_defined rd
    WHERE rd.IS_DELETED = 0
      AND ${RULE_ID_SCOPE}
      AND ${where}
    ORDER BY (${recallScore}) DESC, rd.RULE_NAME IS NULL, rd.RULE_ID
    LIMIT ?`, [...params, ...scoreParams, fetchLimit]);

  const ids = [...new Set(rows.map((r) => r.ruleId))];
  const predefinesById = ids.length ? await fetchPredefines(pool, ids) : new Map();

  const items = [];
  const seen = new Set();
  for (const row of rows) {
    if (seen.has(row.ruleId)) continue;
    seen.add(row.ruleId);
    const ifText = stripHtml(row.ifHtml);
    const predefines = (predefinesById.get(row.ruleId) ?? []).map(stripHtml);
    items.push({
      ruleId: row.ruleId,
      ruleName: row.ruleName ?? "(无名规则)",
      predefines,
      ifText,
      /** 那么/否则动作，由 attachActions 在切完 limit 后填充。 */
      actions: [],
      ruleTips: row.ruleTips ?? "",
      updatedDate: row.updatedDate ? new Date(row.updatedDate).toISOString().slice(0, 10) : "",
      contentJson: row.contentJson ?? null,
      score: scoreRule(words, { ruleName: row.ruleName, ifText, predefines }),
    });
  }
  items.sort((a, b) => b.score - a.score || a.ruleName.length - b.ruleName.length || a.ruleId.localeCompare(b.ruleId));

  const shown = items.slice(0, limit);
  await attachActions(pool, shown, bomBaseInfoId);
  return {
    words, limit, scope,
    /** 已取回并打分的命中条数；overflow 为真时说明数据库侧被 fetchLimit 截断，实际命中可能更多。 */
    matchedCount: items.length,
    fetchLimit,
    overflow: items.length >= fetchLimit,
    elapsedMs: Date.now() - started,
    items: shown,
    truncated: shown.length < items.length,
  };
}

/** 批量取预定义原文（一步 SQL，命中行 ≤ fetchLimit，走 RULE_ID 索引）。 */
async function fetchPredefines(pool, ids) {
  const placeholders = ids.map(() => "?").join(",");
  const rows = await queryRows(pool, `
    SELECT ${HINT}
           RULE_ID AS ruleId, RULE_PREDEFINE_DESC AS descText
    FROM rule_defined_predefine
    WHERE RULE_ID IN (${placeholders})
    ORDER BY RULE_ID, CREATED_DATE`, ids);
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.ruleId)) map.set(r.ruleId, []);
    const text = String(r.descText ?? "").trim();
    if (text) map.get(r.ruleId).push(text);
  }
  return map;
}

/**
 * 取规则的执行动作（那么/否则）。不参与关键词检索，按命中规则的 RULE_ID 直接带出。
 * SETTING_THEN：'1'=那么、'0'=否则（实测 33336:80）；ORDER_BY 是 varchar，需数值排序且可能为 NULL。
 */
async function fetchActions(pool, ruleIds) {
  const ph = ruleIds.map(() => "?").join(",");
  const rows = await queryRows(pool, `
    SELECT ${HINT}
           RULE_ID AS ruleId, RULE_METHOD_DEFINE_ID AS defId, method_id AS methodId,
           SETTING_THEN AS branch, ORDER_BY AS step
    FROM rule_method_define
    WHERE RULE_ID IN (${ph})
    ORDER BY RULE_ID, (SETTING_THEN = '0'), ORDER_BY IS NULL, CAST(ORDER_BY AS SIGNED), RULE_METHOD_DEFINE_ID`, ruleIds);
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.ruleId)) map.set(r.ruleId, []);
    const step = Number(r.step);
    map.get(r.ruleId).push({
      defId: r.defId,
      methodId: String(r.methodId ?? ""),
      branch: r.branch === "0" ? "否则" : "那么",
      step: Number.isFinite(step) ? step : null,
      methodName: "",
      nameVariants: 0,
      params: [],
    });
  }
  return map;
}

/**
 * 动作文本 METHOD_NAME（rule_method_define.METHOD_NAME 实测全空，只能回表取）。
 * 同一 METHOD_ID 在 rule_method 里按 BOM_MODEL_ID 存多版本，故先把范围限在当前 BOM 的模型集合，
 * 再 GROUP BY METHOD_ID 取一个名称；范围内查不到的整体兜底一次。
 */
async function fetchMethodNames(pool, methodIds, bomBaseInfoId) {
  const ph = methodIds.map(() => "?").join(",");
  const rows = await queryRows(pool, `
    SELECT ${HINT}
           m.METHOD_ID AS methodId, MIN(m.METHOD_NAME) AS name,
           COUNT(DISTINCT m.METHOD_NAME) AS nameCount
    FROM rule_method m
    WHERE m.METHOD_ID IN (${ph})
      AND m.BOM_MODEL_ID IN (SELECT BOM_MODEL_ID FROM decision_repository WHERE BOM_BASE_INFO_ID = ?)
    GROUP BY m.METHOD_ID`, [...methodIds, bomBaseInfoId]);
  const map = new Map(rows.map((r) => [r.methodId, r]));
  const missing = methodIds.filter((id) => !map.has(id));
  if (!missing.length) return map;
  const ph2 = missing.map(() => "?").join(",");
  const fallback = await queryRows(pool, `
    SELECT ${HINT}
           METHOD_ID AS methodId, MIN(METHOD_NAME) AS name,
           COUNT(DISTINCT METHOD_NAME) AS nameCount
    FROM rule_method WHERE METHOD_ID IN (${ph2}) GROUP BY METHOD_ID`, missing);
  for (const r of fallback) map.set(r.methodId, r);
  return map;
}

/** 动作参数（一个动作可 0..8 个），按 param1 / param2 … 自然序排列。 */
async function fetchActionParams(pool, defIds) {
  const ph = defIds.map(() => "?").join(",");
  const rows = await queryRows(pool, `
    SELECT ${HINT}
           RULE_METHOD_DEFINE_ID AS defId, PARAM_ID AS paramId,
           PARAM_TYPE AS paramType, PARAM_VALUE AS paramValue
    FROM rule_method_define_param
    WHERE RULE_METHOD_DEFINE_ID IN (${ph})`, defIds);
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.defId)) map.set(r.defId, []);
    map.get(r.defId).push({
      paramId: String(r.paramId ?? ""),
      paramType: String(r.paramType ?? ""),
      value: String(r.paramValue ?? ""),
    });
  }
  for (const list of map.values()) {
    list.sort((a, b) => a.paramId.localeCompare(b.paramId, "en", { numeric: true }));
  }
  return map;
}

/** 只为真要展示的那几条规则补动作，避免为被 limit 砍掉的规则白查。 */
async function attachActions(pool, items, bomBaseInfoId) {
  if (!items.length) return;
  const actionsById = await fetchActions(pool, items.map((it) => it.ruleId));
  const acts = [...actionsById.values()].flat();
  const methodIds = [...new Set(acts.map((a) => a.methodId).filter(Boolean))];
  const names = methodIds.length ? await fetchMethodNames(pool, methodIds, bomBaseInfoId) : new Map();
  const paramsById = acts.length ? await fetchActionParams(pool, [...new Set(acts.map((a) => a.defId))]) : new Map();
  for (const it of items) {
    const list = actionsById.get(it.ruleId) ?? [];
    for (const a of list) {
      const n = names.get(a.methodId);
      a.methodName = String(n?.name ?? "").trim();
      a.nameVariants = Number(n?.nameCount ?? 0);
      a.params = paramsById.get(a.defId) ?? [];
    }
    it.actions = list;
  }
}

async function queryRows(pool, sql, params) {
  const retry = loadConfig().ruleSearch.retry;
  try {
    // 每次都是只读 SELECT，幂等，可安全重试；连接池首次真正建连也发生在这里（getPool 只 createPool）。
    const [rows] = await withRetry(() => pool.query(sql, params), retry);
    return rows;
  } catch (err) {
    const attempts = err?.__retryAttempts ?? 1;
    const code = err?.code ?? err?.sqlMessage ?? err?.message ?? "未知错误";
    const retried = attempts > 1 ? `（已自动重试 ${attempts} 次仍失败）` : "";
    throw new Error(`查询 ya_car 失败${retried}：${code}\n`
      + `请检查 config/mysql.json 的连接信息、账号是否已授权当前来源 IP（当前按 root@本机 授权）、以及 192.168.0.6:3306 是否可达。`, { cause: err });
  }
}

/** 相关度：规则名 > 如果原文 > 预定义；词在名称中越短越靠前。 */
function scoreRule(words, { ruleName, ifText, predefines }) {
  const name = String(ruleName ?? "");
  const pre = predefines.join(" ");
  let score = 0;
  for (const w of words) {
    if (name === w) score += 100;
    else if (name.startsWith(w) || name.endsWith(w)) score += 60;
    else if (name.includes(w)) score += 45;
    if (ifText.includes(w)) score += 20;
    if (pre.includes(w)) score += 10;
  }
  return score;
}

function normalizeLimit(raw, ruleSearch) {
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n) || n <= 0) return Math.min(ruleSearch.defaultLimit, ruleSearch.maxLimit);
  return Math.min(ruleSearch.maxLimit, n);
}

/**
 * 结果文本按段落固定行式：RULE_ID / 预定义(0..n 行) / 如果(1 行) / 那么 / 否则。
 * 「那么」「否则」各只出现一次段名：单条动作跟在段名后，多条则段名独占一行、动作逐条缩进编号。
 * 规则名、更新时间、提示、动作JSON 一律不进文本（数据仍在 structuredData 里）。
 */
/** 名称/参数值压成一行，并把半角逗号换成全角，避免与行内分隔符相撞。 */
const flattenField = (raw) => flattenLine(raw).replace(/,/g, "，");

/**
 * 方法名里的数字占位符：独立的单个数字（前后不接字母/数字/下划线），
 * 可连同紧跟的「，<提示>」「,<提示>」「、<提示>」「<提示>」（数字与尖括号间的逗号/空格一并吃掉）。
 * 多位数字串（如 063013、_112、915）不是占位符——前后瞻保证整串不匹配。
 */
const PLACEHOLDER_RE = /(?<![\w])(\d)(?![\w])(?:\s*[，,、]?\s*<[^<>]*>)?/g;

/**
 * 组装「那么/否则」单条动作文本：代码直接把参数值填进占位符，不再依赖模型自行拼装。
 * 规则（与产品口径一致）：
 *  - 占位符 N（或 N<提示>）→ 用第 N+1 个参数值替换（参数按 paramId 自然序）；
 *  - 数字 ≥ 参数个数 → 视为方法名自带数字，原样保留；
 *  - 数字填充后仍有未用参数 → 方法名里的空括号对「（）/()」按序填成「（值）」
 *    （如「设置险别列表中险别编号为()的费率%为()」两个括号就是参数槽位）；
 *  - 没有空括号可填 → 无数字占位符时参数值直接接在方法名后面（多值全角逗号分隔），
 *    有数字占位符但仍有漏网参数时末尾以「，值」补齐，避免丢信息。
 * 例：「向this增加核保结果0， <核保结果>，规则名称: 1， <规则名称>，规则描述: 2， <规则描述>」
 * 填参后 →「向this增加核保结果动态域集的核保结果的转人工分公司初级，规则名称: 动态域集的帮助的当前规则的名称，规则描述: "…"」。
 */
export function assembleActionText(action) {
  const name = flattenField(action.methodName) || action.methodId || "（未知方法）";
  const paramTexts = (action.params ?? []).map((p) => flattenField(p.value));
  if (!paramTexts.length) return name;

  const used = paramTexts.map(() => false);
  const filled = name.replace(PLACEHOLDER_RE, (match, digit) => {
    const n = Number(digit);
    if (n >= paramTexts.length) return match;
    used[n] = true;
    return paramTexts[n];
  });

  const rest = paramTexts.filter((v, i) => !used[i] && v !== "");
  if (!rest.length) return filled;
  const hadDigit = used.some(Boolean);
  // 空括号对（半角/全角）按序当槽位填值；括号多于剩余参数时多余括号保持原样。
  const withParens = filled.replace(/[（(]\s*[）)]/g, () => (rest.length ? `(${rest.shift()})` : "()"));
  if (!rest.length) return withParens;
  return hadDigit ? `${withParens}，${rest.join("，")}` : `${withParens}${rest.join("，")}`;
}

export function formatResults(result) {
  if (result.message) return result.message;
  const { words, items, scope } = result;
  const kw = words.join(" ");
  if (!items.length) {
    return `关键词「${kw}」在${scope}下未命中规则，可换更短的业务词或减少限定词再试。`;
  }
  const notes = [];
  if (result.overflow) notes.push(`取回已达上限 ${result.fetchLimit} 条，可能不全`);
  else if (result.truncated) notes.push(`按相关度只取前 ${items.length} 条`);
  const head = `命中 ${result.matchedCount ?? items.length} 条${notes.length ? `（${notes.join("；")}）` : ""}`;

  const blocks = items.map((it) => {
    const lines = [`RULE_ID=${it.ruleId}`];
    for (const p of it.predefines) {
      const text = flattenLine(p);
      if (text) lines.push(`预定义: ${text}`);
    }
    lines.push(`如果: ${flattenLine(it.ifText) || "（空）"}`);
    // 同一分支（那么/否则）有多个动作时，段名只出现一次，动作按 SQL 的 ORDER_BY 顺序逐条编号换行。
    for (const branch of ["那么", "否则"]) {
      const acts = (it.actions ?? []).filter((a) => a.branch === branch);
      if (!acts.length) continue;
      const texts = acts.map((a) => assembleActionText(a));
      if (texts.length === 1) lines.push(`${branch}: ${texts[0]}`);
      else lines.push(`${branch}:`, ...texts.map((t, i) => `    ${i + 1}. ${t}`));
    }
    return lines.join("\n");
  });
  return [head, ...blocks].join("\n\n");
}

/** 连通性与范围自检：库版本、该 BOM_BASE_INFO_ID 下的决策库/目录/规则规模。 */
export async function stats(bomBaseInfoId = loadConfig().ruleSearch.bomBaseInfoId) {
  const pool = await getPool();
  const server = await queryRows(pool, `SELECT ${HINT} VERSION() AS v, @@collation_database AS coll`);
  const repos = await queryRows(pool, `SELECT ${HINT} COUNT(*) AS n FROM decision_repository WHERE BOM_BASE_INFO_ID = ?`, [bomBaseInfoId]);
  const rules = await queryRows(pool, `
    SELECT ${HINT} COUNT(DISTINCT crr.RULE_ID) AS n
    FROM catalogue_rule_rel crr
    JOIN decision_rep_catalogue dc ON dc.DECISION_REP_CATALOGUE_ID = crr.DECISION_REP_CATALOGUE_ID
    JOIN decision_repository dr ON dr.DECISION_REPOSITORY_ID = dc.DECISION_REPOSITORY_ID
    WHERE dr.BOM_BASE_INFO_ID = ?`, [bomBaseInfoId]);
  const cfg = loadConfig();
  return {
    server: server[0].v,
    collation: server[0].coll,
    bomBaseInfoId,
    bomBaseInfoName: cfg.ruleSearch.bomBaseInfoName,
    decisionRepos: Number(repos[0].n),
    rules: Number(rules[0].n),
  };
}

/** 面向业务人员的失败文案（不含表名 / IP / 英文字段等程序员向信息）。 */
const FRIENDLY_MESSAGES = {
  config: "规则库连接配置尚未就绪，暂时无法检索已有规则。请联系管理员完成配置后重试。",
  auth: "规则库账号没有访问权限，请联系管理员核对数据库授权后重试。",
  connection: "规则库暂时连接不上（已自动重试仍未成功）。请稍后重新提问；若持续失败，请联系管理员确认数据库服务是否在线。",
  query: "规则检索暂时失败。请稍后重试，或换用更短、更通用的业务关键词；若持续失败请联系管理员。",
};

/**
 * 把 searchRules / stats 抛出的错误分类为「给业务人员的友好文案 + 技术详情」。
 * 工具壳用 friendly 作为模型可读的 content，用 technical 落到 details 供日志与排查。
 * @param {unknown} err
 * @returns {{kind: "config"|"auth"|"connection"|"query", friendly: string, technical: string}}
 */
export function describeError(err) {
  const message = String(err?.message ?? err ?? "未知错误");
  const code = String(err?.code ?? err?.cause?.code ?? "");
  let kind = "query";
  if (/配置文件不存在|缺少 .*字段|JSON\.parse|Unexpected token/.test(message)) kind = "config";
  else if (/Access denied|ER_ACCESS_DENIED|ERR_dbAccessDenied/i.test(message + code)) kind = "auth";
  else if (isRetryableError(err?.cause ?? err) || /ECONNREFUSED|ETIMEDOUT|PROTOCOL_CONNECTION_LOST|已自动重试|可达/.test(message)) kind = "connection";
  return { kind, friendly: FRIENDLY_MESSAGES[kind], technical: message };
}

/* ------------------------------------------------------------------- CLI 自检 */

const invokedDirectly = /rule-search\.mjs$/.test(process.argv[1] ?? "");
if (invokedDirectly) {
  const positional = [];
  const opts = { limit: undefined, bomBaseInfoId: undefined };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === "--limit") opts.limit = Number(argv[++i]);
    else if (token === "--bom") opts.bomBaseInfoId = argv[++i];
    else if (token === "--content") opts.includeContent = true;
    else if (token === "--json") opts.json = true;
    else if (token === "--ping" || token === "--stats") {
      const s = await stats(opts.bomBaseInfoId);
      process.stdout.write(JSON.stringify(s, null, 2) + "\n");
      await closePool();
      process.exit(0);
    } else if (token.startsWith("-")) throw new Error(`未知参数：${token}`);
    else positional.push(token);
  }
  const result = await searchRules(positional.join(" "), opts);
  process.stdout.write((opts.json ? JSON.stringify(result, null, 2) : formatResults(result)) + "\n");
  await closePool();
}
