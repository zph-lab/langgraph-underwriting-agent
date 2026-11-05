/**
 * 核保规则业务助手扩展，提供两个工具：
 *
 * - bom_search：在 bom/bom.sqlite（tools/bom-to-sqlite.mjs 从 bom/bom.json 生成）中按关键词
 *   检索业务对象字段，返回父链拼接的业务名称、type、dataType 与 fieldPath。
 * - rule_search：在 MySQL（ya_car，只读）中按关键词检索已有规则，链路与规模由 config/mysql.json
 *   里的 bomBaseInfoId 圈定，返回「预定义 / 如果」原文（已去富文本标签）。
 *
 * 检索逻辑分别在 tools/bom-search.mjs、tools/rule-search.mjs（均可独立 CLI 验证），本文件只做 pi 工具壳。
 */

import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	DEFAULT_DB_PATH,
	describeError as describeBomError,
	formatResults as formatBomResults,
	searchFields,
} from "../../tools/bom-search.mjs";
import {
	describeError as describeRuleError,
	formatResults as formatRuleResults,
	searchRules,
} from "../../tools/rule-search.mjs";

const bomParameters = Type.Object({
	keyword: Type.String({
		description:
			"业务要素关键词，例如「机构」「车龄」「精算处理」「违章」。可匹配中文 label、英文字段名、fieldPath、Java 类名、码值服务。",
	}),
	limit: Type.Optional(Type.Number({ description: "最多返回条数，默认 10，上限 50。" })),
	includeMethods: Type.Optional(
		Type.Boolean({ description: "是否同时检索方法节点（dataType=method，如「取得结果()」）。默认 false，只返回业务字段。" }),
	),
	dataType: Type.Optional(
		Type.String({ description: "按 dataType 精确过滤，可选 field / object / list / method。一般不需要。" }),
	),
});

const ruleParameters = Type.Object({
	keyword: Type.String({
		description:
			"规则关键词，例如「手续费率」「车龄」「违章」「交强险」。匹配规则名、「如果」原文与「预定义」内容；空格分词为「且」关系（如「车龄 违章」要求两个词都出现）。",
	}),
	limit: Type.Optional(Type.Number({ description: "最多返回条数，默认 10，上限 50。" })),
	bomBaseInfoId: Type.Optional(
		Type.String({
			description:
				"按 BOM 基本信息 ID（BOM_BASE_INFO_ID）圈定规则范围，默认取 config/mysql.json 中的「车险销售费用A3」。一般不需要。",
		}),
	),
});

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "bom_search",
		label: "检索核保字段",
		description:
			"按关键词检索核保业务模型（BOM）字段。父链名称由代码拼好（如「保单的二级机构」，根节点「业务模型对象」已忽略）" ,
		promptSnippet: "按中文业务关键词检索核保 BOM 字段（父链名称 + type + dataType + fieldPath）",
		promptGuidelines: [
			"引用业务字段前先调用 bom_search 拿到 fieldPath，规则文本用返回的父链名称（如「保单的二级机构」），报文/路径用 fieldPath，不要凭记忆拼。",
			"同一关键词常命中多条同名字段（如「二级机构」在保单、关联保单、上年保单下各有一条），必须靠父节点/层级区分，不要随意挑第一条。",
			"如果关键词没有命中可考虑更换近义词或者从现有规则文本内提取关键词"
		],
		parameters: bomParameters,
		async execute(_toolCallId, params): Promise<{ content: { type: "text"; text: string }[]; details: unknown; isError?: boolean }> {
			try {
				const result = await searchFields(params.keyword, {
					limit: params.limit,
					includeMethods: params.includeMethods,
					dataType: params.dataType,
					dbPath: process.env.BOM_SQLITE_PATH ?? DEFAULT_DB_PATH,
				});
				return {
					content: [{ type: "text" as const, text: formatBomResults(result) }],
					details: result,
				};
			} catch (error) {
				// 本地库失败（缺文件/损坏/SQL 异常）不重试，直接返回业务友好文案；技术详情留在 details。
				const info = describeBomError(error);
				return {
					content: [{ type: "text" as const, text: info.friendly }],
					details: { error: true, kind: info.kind, technical: info.technical },
					isError: true,
				};
			}
		},
	});

	pi.registerTool({
		name: "rule_search",
		label: "检索已有规则",
		description:
			"按关键词检索 MySQL 规则库（ya_car，只读）中已有的核保规则，返回最相关的若干条。每条给出 RULE_ID、" +
			"「预定义」文本（rule_defined_predefine.RULE_PREDEFINE_DESC）与" +
			"「如果」原文文本（rule_defined.RULE_DESC，已去除富文本标签）。\n" +
			"「否则/那么」动作文本（输出即为成品：工具代码已把参数值填进方法名的数字占位符，无需再做任何拼装）。" ,
		promptSnippet: "按关键词检索已有核保规则原文（预定义/如果/那么/否则）",
		promptGuidelines: [
			"只要输出给用户的规则，不允许省略，必须完整显示规则内容，不要出现表名、英文路径。" ,
			"检索到的文本，不要省略或增添内容：段名用半角冒号（预定义: / 如果:），一条预定义占一行，不把多行并成一句，也不给「如果」自行断行。",
			"「那么/否则」文本已由代码完成参数填充（占位符已替换为实际参数值），原样完整输出即可，禁止再自行拆解、补参数明细或改动已填好的句子。",
			"如果关键词没有命中可考虑更换近义词或者从现有规则文本内提取关键词"
		],
		parameters: ruleParameters,
		async execute(_toolCallId, params): Promise<{ content: { type: "text"; text: string }[]; details: unknown; isError?: boolean }> {
			try {
				const result = await searchRules(params.keyword, {
					limit: params.limit,
					bomBaseInfoId: params.bomBaseInfoId,
				});
				return {
					content: [{ type: "text" as const, text: formatRuleResults(result) }],
					details: result,
				};
			} catch (error) {
				// searchRules 内部已按 config.retry 重试瞬时错误；走到这里说明是真失败（配置/权限/重试耗尽）。
				const info = describeRuleError(error);
				return {
					content: [{ type: "text" as const, text: info.friendly }],
					details: { error: true, kind: info.kind, technical: info.technical },
					isError: true,
				};
			}
		},
	});
}
