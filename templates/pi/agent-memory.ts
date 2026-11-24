/**
 * Agent Memory Bridge for Pi (E:\Agent Memory)
 *
 * Pi 与共享长期记忆库的薄适配层。Pi 没有 JSON hook 配置，宿主接入靠扩展，
 * 所以这里用 `pi.on(...)` 而不是 stop-hook 脚本。
 *
 * 职责（只读提醒，不自动提交）：
 * 1. tool_result：追踪本轮写入记忆库的文件；
 * 2. agent_settled：认领这些文件（memoryctl claim），再跑 closeout --dry-run，
 *    把需要人工确认的信号（MERGE_REQUIRED / ASK_USER / 删除）报给用户。
 *
 * 正式 closeout（含 scoped commit）仍由 agent 按 vault AGENTS.md 显式执行。
 *
 * ## 入口：必须用已安装 Runtime 的 memoryctl
 *
 * 不能用源码仓库里的那份。源码仓库没有 config/agent-memory.toml，closeout 会退回
 * 模板默认值——VAULT_ROOT 指向 templates/vault、REPO_ROOT 指向源码仓库自身，于是
 * git 基线比较拿 vault 的历史提交去比源码仓库的 HEAD，必然报
 * "closeout git baseline is not an ancestor of HEAD"，exit 1 被误报成失败。
 * README 也明确「需要让多个 Agent 从固定本机入口调用时，把仓库安装为 Runtime」。
 *
 * 解析顺序（全部可配置，无个人硬编码路径）：
 *   AGENT_MEMORY_MEMORYCTL  直接指定 memoryctl 可执行文件
 *   AGENT_MEMORY_CONFIG_ROOT 指定 Runtime 根（默认 ~/.config/agent-memory）
 *   AGENT_MEMORY_PYTHON      指定解释器（默认 python）
 *
 * ## 退出码语义
 *
 * scripts/agent_memory_closeout.py：0=ok，1=warning（有告警但不阻塞，如 audit 到期、
 * 未推送提交），2=error/blocked。warning 不是失败——只提示需要关注。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { homedir } from "node:os";
import { join } from "node:path";

/** 记忆库根目录：用正斜杠 + 小写做前缀比较，兼容 Windows 反斜杠与大小写差异。 */
const VAULT_PREFIX = "e:/agent memory/";

/** 已安装 Runtime 的 memoryctl（README 推荐的固定本机入口）。 */
function resolveMemoryctl(): string {
	const explicit = process.env.AGENT_MEMORY_MEMORYCTL?.trim();
	if (explicit) return explicit;
	const configRoot = process.env.AGENT_MEMORY_CONFIG_ROOT?.trim();
	if (configRoot) return join(configRoot, "scripts", "memoryctl");
	return join(homedir(), ".config", "agent-memory", "scripts", "memoryctl");
}

const MEMORYCTL = resolveMemoryctl();
const PYTHON = process.env.AGENT_MEMORY_PYTHON?.trim() || "python";
const CLAIM_TIMEOUT_MS = 30_000;
const CLOSEOUT_TIMEOUT_MS = 180_000;

/** closeout 退出码：0=ok，1=warning，2=error/blocked。 */
const EXIT_OK = 0;
const EXIT_WARNING = 1;

function normalizePath(p: unknown): string {
	return typeof p === "string" ? p.replace(/\\/g, "/").toLowerCase() : "";
}

function vaultPathFromInput(input: unknown): string | null {
	if (!input || typeof input !== "object") return null;
	const raw = (input as Record<string, unknown>).path;
	if (typeof raw !== "string") return null;
	const norm = normalizePath(raw);
	return norm.startsWith(VAULT_PREFIX) ? raw : null;
}

export default function (pi: ExtensionAPI) {
	let touched = new Set<string>();

	pi.on("session_start", () => {
		touched = new Set<string>();
	});

	pi.on("tool_result", (event) => {
		if (event.toolName !== "write" && event.toolName !== "edit") return;
		if (event.isError) return;
		const file = vaultPathFromInput(event.input);
		if (file) touched.add(file);
	});

	pi.on("agent_settled", async (_event, ctx) => {
		if (touched.size === 0) return;
		const files = [...touched];
		touched = new Set<string>();

		// 1. 认领本轮写入的记忆文件（幂等）
		const claimFailures: string[] = [];
		for (const file of files) {
			const res = await pi.exec(
				PYTHON,
				[MEMORYCTL, "--actor", "pi", "claim", "--file", file],
				{ timeout: CLAIM_TIMEOUT_MS },
			);
			if (res.code !== 0) claimFailures.push(file);
		}

		// 2. closeout dry-run，只报告需要人工确认的信号
		const dry = await pi.exec(
			PYTHON,
			[MEMORYCTL, "--actor", "pi", "closeout", "--dry-run"],
			{ timeout: CLOSEOUT_TIMEOUT_MS },
		);
		if (!ctx.hasUI) return;

		const out = `${dry.stdout}\n${dry.stderr}`;
		// 2=error/blocked 才是真失败；1=warning 表示有告警但不阻塞。
		if (dry.code !== EXIT_OK && dry.code !== EXIT_WARNING) {
			ctx.ui.notify(
				`agent-memory closeout --dry-run 失败（code=${dry.code}），请手动检查`,
				"warning",
			);
			return;
		}
		if (/MERGE_REQUIRED|ASK_USER|\bdeleted\b|脏变更/i.test(out)) {
			ctx.ui.notify(
				"agent-memory dry-run 检出需人工确认项（MERGE_REQUIRED/ASK_USER/删除），正式 closeout 前请先处理",
				"warning",
			);
			return;
		}
		if (claimFailures.length > 0) {
			ctx.ui.notify(`agent-memory claim 失败 ${claimFailures.length} 个文件，closeout 可能漏提交`, "warning");
			return;
		}
		if (dry.code === EXIT_WARNING) {
			// warning 不阻塞：如实说明有告警（通常是无害的 audit 到期/未推送）
			ctx.ui.notify(
				`agent-memory：已认领 ${files.length} 个文件，dry-run 有告警但不阻塞（code=1），可执行正式 closeout`,
				"info",
			);
			return;
		}
		ctx.ui.notify(`agent-memory：已认领 ${files.length} 个文件，dry-run 干净，可执行正式 closeout`, "info");
	});
}
