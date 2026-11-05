/** tools/rule-search.mjs 的类型声明，供 .pi 扩展在编辑器里获得类型提示。 */

export interface MysqlConnectionConfig {
  host: string;
  port: number;
  user: string;
  password: string;
  database: string;
  connectTimeout: number;
}

export interface RuleSearchRetryConfig {
  /** 单次查询最多尝试次数（含首次）。默认 3。 */
  maxAttempts: number;
  /** 指数退避基准毫秒。默认 300。 */
  baseDelayMs: number;
  /** 退避上限毫秒。默认 3000。 */
  maxDelayMs: number;
}

export interface RuleSearchConfig {
  mysql: MysqlConnectionConfig;
  ruleSearch: {
    /** 默认圈定规则范围的 BOM 基本信息 ID（车险销售费用A3）。 */
    bomBaseInfoId: string;
    bomBaseInfoName: string;
    defaultLimit: number;
    maxLimit: number;
    /** 相关度排序需要，SQL 取回条数 = limit × 该系数（上限 150）。 */
    fetchFactor: number;
    /** 只读 SELECT 的瞬时错误重试参数。 */
    retry: RuleSearchRetryConfig;
  };
}

/** describeError 的分类结果：friendly 给模型/业务人员，technical 供日志排查。 */
export interface DescribedError {
  kind: "config" | "auth" | "connection" | "query";
  friendly: string;
  technical: string;
}

/** 动作参数，取自 rule_method_define_param。 */
export interface RuleMethodParam {
  /** paramId，形如 param1 / param2。 */
  paramId: string;
  /** PARAM_TYPE：string / double / boolean / integer / object / date。 */
  paramType: string;
  /** PARAM_VALUE 原文：字段引用是中文业务名，常量带英文引号。 */
  value: string;
}

/** 「那么/否则」的一条执行动作。 */
export interface RuleAction {
  /** 所在 rule_method_define.RULE_METHOD_DEFINE_ID。 */
  defId: string;
  /** rule_method_define.method_id。 */
  methodId: string;
  /** SETTING_THEN：'1' → 那么，'0' → 否则。 */
  branch: "那么" | "否则";
  /** rule_method_define.ORDER_BY（varchar，已转数字）；为空时 null。 */
  step: number | null;
  /** rule_method.METHOD_NAME，即动作文本，如「设置商业险手续费率为」。 */
  methodName: string;
  /** 该 METHOD_ID 在 rule_method 里不同 METHOD_NAME 的个数，>1 说明存在版本歧义。 */
  nameVariants: number;
  params: RuleMethodParam[];
}

export interface RuleSearchItem {
  ruleId: string;
  ruleName: string;
  /** 「预定义」段，取自 rule_defined_predefine.RULE_PREDEFINE_DESC（已去 HTML）；约 3.7% 的规则才有。 */
  predefines: string[];
  /** 「如果」原文，取自 rule_defined.RULE_DESC，已去除 <span class="fields"> 等富文本标签。 */
  ifText: string;
  /** 「那么/否则」动作，按 step 升序；不参与关键词检索，命中规则后直接带出。约 8 成规则只有 1 条，少数规则无任何动作。 */
  actions: RuleAction[];
  ruleTips: string;
  updatedDate: string;
  /** 仅 includeContent=true 时有值：rule_defined.rule_content 原文（JSON AST，含那么/否则）。 */
  contentJson: string | null;
  score: number;
}

export interface RuleSearchOptions {
  limit?: number;
  bomBaseInfoId?: string;
  includeContent?: boolean;
}

export interface RuleSearchResult {
  /** 空格分词后的关键词数组。 */
  words: string[];
  limit: number;
  /** 检索范围中文描述（BOM 名称 + BOM_BASE_INFO_ID）。 */
  scope: string;
  /** 已取回并打分的命中条数。 */
  matchedCount: number;
  fetchLimit: number;
  /** true 表示数据库侧取回达到上限，实际命中可能更多。 */
  overflow: boolean;
  /** 本次查询耗时（毫秒），无全文索引时约 1300～2000。 */
  elapsedMs: number;
  items: RuleSearchItem[];
  /** true 表示命中多于 limit，只展示了前若干条。 */
  truncated: boolean;
  message?: string;
}

export interface RuleScopeStats {
  server: string;
  collation: string;
  bomBaseInfoId: string;
  bomBaseInfoName: string;
  decisionRepos: number;
  rules: number;
}

export declare const PROJECT_ROOT: string;
export declare const CONFIG_PATH: string;
export declare const DEFAULT_LIMIT: number;
export declare const MAX_LIMIT: number;
export declare const DEFAULT_BOM_BASE_INFO_ID: string | null;

export declare function loadConfig(options?: { reload?: boolean }): RuleSearchConfig;
export declare function getPool(): Promise<import("mysql2/promise").Pool>;
export declare function closePool(): Promise<void>;
/** 去除富文本标签并反转义实体，用于 RULE_DESC / RULE_PREDEFINE_DESC。 */
export declare function stripHtml(raw: unknown): string;
export declare function searchRules(keyword: string, options?: RuleSearchOptions): Promise<RuleSearchResult>;
/** 生成给模型阅读的中文结果文本。 */
export declare function formatResults(result: RuleSearchResult): string;
export declare function stats(bomBaseInfoId?: string): Promise<RuleScopeStats>;

/** withRetry 默认参数（config 缺省时的兜底）。 */
export declare const DEFAULT_RETRY: RuleSearchRetryConfig;
/** 错误是否为值得重试的瞬时/连接类错误。 */
export declare function isRetryableError(err: unknown): boolean;
/** 指数退避重试执行器；重试用尽抛原始错误（附 __retryAttempts）。 */
export declare function withRetry<T>(fn: () => Promise<T>, opts?: Partial<RuleSearchRetryConfig>): Promise<T>;
/** 把抛出的错误分类为「给业务人员的友好文案 + 技术详情」。 */
export declare function describeError(err: unknown): DescribedError;
