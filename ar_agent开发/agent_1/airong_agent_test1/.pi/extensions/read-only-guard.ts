/**
 * 只读护栏（仅业务工具版）。
 *
 * 本项目面向核保业务人员，模型唯一可用的工具是业务查询工具 bom_search 与 rule_search。
 * 内置工具 read / grep / find / ls / bash 等全部停用——不只是禁写，连「读项目目录之外的文件」
 * 也不再可能，杜绝提示词注入套取本机文件的入口。
 *
 * 管控分两层，缺一不可：
 * 1. 可见层：每轮 before_agent_start 用 setActiveTools 把活跃工具收敛到业务工具，
 *    read/bash 等不再声明给模型——模型根本看不到，省掉「先调用再被拦」的无效回合。
 * 2. tool_call 兜底：白名单之外的调用一律 block。即便有人用 `pi --tools +bash` 这类
 *    命令行参数或 settings.json 重新启用内置工具，这一层仍然拦得住。
 *    codemode 脚本发起的嵌套调用同样会触发本事件（event.parentToolCallId 有值），一样被拦。
 *
 * 另有两条辅助路径：
 * 3. user_bash —— 拦下用户在输入框用 `!` / `!!` 前缀直接执行的命令。这条路径不经过 LLM，
 *    也不触发 tool_call，是最容易被漏掉的口子。返回 result 即为「完整替换」，命令根本不会执行。
 * 4. capability_limits —— 把能力边界写进本轮系统提示，让模型一开始就回答「我没有这个能力」。
 *
 * 注意：新增查询类扩展工具后，要同步加进 ACTIVE_TOOLS（可见）与 ALLOWED_TOOLS（兜底放行），
 * 否则要么模型看不到，要么会被这里拦掉。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** 业务查询工具：模型唯一允许使用的工具。 */
const ACTIVE_TOOLS = ["bom_search", "rule_search"];

/** tool_call 兜底白名单，与 ACTIVE_TOOLS 保持一致。 */
const ALLOWED_TOOLS = new Set(ACTIVE_TOOLS);

/** 明确列出写/执行类工具，只为给用户和模型一句更准确的拦截理由。 */
const WRITE_TOOLS = new Set(["bash", "powershell", "edit", "write", "apply_patch", "codemode", "tool_search"]);

/** 本轮起停用的内置只读工具，同样只用于给出更明确的拦截理由。 */
const DISABLED_BUILTIN_TOOLS = new Set(["read", "grep", "find", "ls"]);

const BLOCK_REASON =
	"本智能体是面向业务人员的只读助手，只允许使用 bom_search 与 rule_search 两个业务查询工具，" +
	"不具备读取文件、修改本机或执行命令的权限，该调用已被系统拦截。" +
	"请直接向用户说明这一限制，并把需要交付的内容（规则 JSON、SQL、结论文本）写在回复里，不要尝试换一种方式绕开。";

export default function (pi: ExtensionAPI) {
	// 1. 模型发起的工具调用：白名单之外全部拦下（兜底）
	pi.on("tool_call", (event) => {
		if (!ALLOWED_TOOLS.has(event.toolName)) {
			let why: string;
			if (WRITE_TOOLS.has(event.toolName)) {
				why = `工具 ${event.toolName} 属于写/执行类操作。`;
			} else if (DISABLED_BUILTIN_TOOLS.has(event.toolName)) {
				why = `内置工具 ${event.toolName} 已在本项目中停用。`;
			} else {
				why = `工具 ${event.toolName} 不在业务查询工具的允许清单内。`;
			}
			return { block: true, reason: `${why}${BLOCK_REASON}` };
		}

		return undefined;
	});

	// 2. 用户用 ! / !! 直接执行的 shell 命令：返回 result 即完整替换，命令不会真的执行
	pi.on("user_bash", (event) => {
		const preview = event.command.length > 120 ? `${event.command.slice(0, 120)}…` : event.command;
		return {
			result: {
				output: `命令未执行。本智能体是面向业务人员的只读助手，禁止在本机执行任何命令。\n被拒绝的输入：${preview}`,
				exitCode: 1,
				cancelled: false,
				truncated: false,
			},
		};
	});

	// 3. 每轮开始前：收敛活跃工具（让内置工具对模型不可见）+ 写入能力边界
	pi.on("before_agent_start", (event, ctx) => {
		pi.setActiveTools(ACTIVE_TOOLS);

		event.systemPromptOptions.sections.capability_limits = [
			"你是面向保险核保业务人员的只读查询助手。",
			"你唯一可用的工具是 bom_search（检索核保字段）和 rule_search（检索已有规则）。除此之外没有任何工具。",
			"你不能：读取或浏览文件、创建、修改、删除、移动任何文件、执行 shell 命令、安装依赖或改动数据库。内置工具 read/grep/find/ls/bash 已全部停用。这不是风格建议，而是硬性限制——这些工具不存在，调用会被拦截。",
			"用户提出与核保业务无关的诉求时，直接说明本助手没有相应能力，把产物以文本形式回复，或告知需由管理员在系统外完成；不要道歉式反复重试，也不要绕道尝试。",
			`当前工作目录：${ctx.cwd.replace(/\\/g, "/")}`,
		].join("\n");
	});
}
