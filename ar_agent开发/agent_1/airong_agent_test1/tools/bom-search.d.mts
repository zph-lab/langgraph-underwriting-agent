/** tools/bom-search.mjs 的类型声明，供 .pi 扩展在编辑器里获得类型提示。 */

export interface BomFieldRow {
  label: string | null;
  field: string | null;
  fieldPath: string | null;
  type: string | null;
  dataType: string | null;
  className: string | null;
  parentLabel: string | null;
  ancestors: string | null;
  topLabel: string | null;
  depth: number;
  requiredField: number | null;
  codeService: string | null;
  collectionType: string | null;
  actualTypeNames: string | null;
  leafCount: number | null;
  isMethod: number;
  fieldId: string | null;
}

export interface BomSearchItem extends BomFieldRow {
  /** 父链拼接的业务名称，如「保单的二级机构」（已忽略根节点「业务模型对象」）。 */
  qualifiedName: string;
}

export interface BomSearchOptions {
  limit?: number;
  includeMethods?: boolean;
  dataType?: string;
  dbPath?: string;
}

export interface BomSearchResult {
  keyword: string;
  limit: number;
  /** 检索范围中文描述，用于结果措辞。 */
  scope?: string;
  items: BomSearchItem[];
  truncated?: boolean;
  message?: string;
}

export declare const PROJECT_ROOT: string;
export declare const DEFAULT_DB_PATH: string;
export declare const DEFAULT_LIMIT: number;
export declare const MAX_LIMIT: number;

export declare function openDatabase(dbPath?: string): Promise<import("node:sqlite").DatabaseSync>;
export declare function closeDatabase(): void;
export declare function qualifiedName(item: BomFieldRow): string;
export declare function searchFields(keyword: string, options?: BomSearchOptions): Promise<BomSearchResult>;
export declare function formatResults(result: BomSearchResult): string;

/** describeError 的分类结果：friendly 给模型/业务人员，technical 供日志排查。 */
export interface BomDescribedError {
  kind: "missing" | "corrupt" | "query";
  friendly: string;
  technical: string;
}
/** 把抛出的错误分类为「给业务人员的友好文案 + 技术详情」。 */
export declare function describeError(err: unknown): BomDescribedError;
