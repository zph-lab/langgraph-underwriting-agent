/**
 * 中文检索的文本预处理：建库侧与查询侧必须共用本模块，否则两侧切法一旦分叉就会静默漏召回。
 *
 * 背景：SQLite 自带的 trigram 分词器要求匹配串 ≥3 个字符，「车牌」「车龄」「机构」这类
 * 两字业务词走 trigram 恒为 0 命中；而 unicode61 会把一整段中文当成一个词，同样不可用。
 * 因此对 CJK 另建一张 2-gram 表（fts5 + unicode61），把「连续子串」语义翻译成「相邻 gram 短语」：
 *   文本「车辆损失」 → 索引词 [车辆, 辆损, 损失]
 *   查询「辆损失」   → 短语 `"辆损 损失"`（要求两个 gram 在文中相邻）——等价于子串包含。
 */

/** CJK 统一表意文字 + 扩展 A + 兼容表意文字（业务文本够用，不含日文假名）。 */
const CJK_CHAR = "\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uF900-\\uFAFF";
const CJK_RUN_RE = new RegExp(`[${CJK_CHAR}]+`, "g");
const CJK_TEST_RE = new RegExp(`[${CJK_CHAR}]`);
/** ASCII 字母数字与 %_\ 一律视为「非纯中文」，交给 trigram 表处理。 */
const ASCII_RE = /[A-Za-z0-9_%\\]/;

/** 取出文本里的连续 CJK 片段，按出现顺序返回。 */
export function cjkRuns(text) {
  return String(text ?? "").match(CJK_RUN_RE) ?? [];
}

/** 一个 CJK 片段切成 gram：长度 1 原样保留，否则取滑动 2-gram。 */
export function gramsOfRun(run) {
  if (run.length < 2) return [run];
  const out = [];
  for (let i = 0; i + 1 < run.length; i++) out.push(run.slice(i, i + 2));
  return out;
}

/** 建库侧：把一列文本转成 gram 串（空格分隔）；无 CJK 时返回 null（该行该列不进索引）。 */
export function gramText(text) {
  const out = [];
  for (const run of cjkRuns(text)) out.push(...gramsOfRun(run));
  return out.length ? out.join(" ") : null;
}

/** FTS5 短语转义：整串用双引号包住，内部双引号成对翻倍。 */
export function quotePhrase(term) {
  return `"${String(term).replaceAll('"', '""')}"`;
}

/**
 * 查询侧：关键词 → fts_cjk 的 MATCH 串。
 * 每个 CJK 片段各自构成一个短语（片段内 gram 必须相邻），片段之间取「且」。
 * 无 CJK 片段返回 null（该关键词不该走中文表）。
 */
export function cjkMatchQuery(keyword) {
  const runs = cjkRuns(keyword);
  if (!runs.length) return null;
  return runs.map((r) => quotePhrase(gramsOfRun(r).join(" "))).join(" AND ");
}

/**
 * 中文表能否单独代表 LIKE 语义：
 * 纯中文（含空格/中文标点）时，gram 命中是「整串 LIKE 命中」的必要条件，
 * 索引 0 命中即可判定 LIKE 也 0 命中，无需再全表扫；混入 ASCII 则不作此保证。
 */
export function isCjkOnly(keyword) {
  return CJK_TEST_RE.test(keyword) && !ASCII_RE.test(keyword);
}

/** trigram 表的可查询下限：不足 3 字符无法构造 gram，必须另走途径。 */
export function trigramUsable(keyword) {
  return String(keyword ?? "").length >= 3;
}
