---
memory_type: workflow
track: workflow
project_id: agent-memory-vault-closeout
app_id: {{APP_ID}}
user_id: {{USER_ID}}
agent_id: {{AGENT_ID}}
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
keywords:
  - closeout
  - memory
---

# Agent 记忆收尾决策规则

## 当前有效摘要

普通记忆不设候选池。每次重要任务结束时，由 Agent 判断是否有稳定事实需要直接写入正式目录；写入前先对账，写入后用 closeout 自动整理。

## 写入前对账

写入前先运行：

```bash
python3 scripts/memoryctl --actor codex prewrite "准备写入的记忆摘要" \
  --source-class user_direct \
  --knowledge-kind fact \
  --asserted-by user \
  --evidence-ref "当前对话中的明确指令"
```

其中，`source_class` 表示来源类别，`knowledge_kind` 表示内容类型。安全评估先于检索和对账；外部不可信规则、来源不明事实和 Agent 推断的权威偏好不能直接进入正式记忆。

受保护文件需要内容绑定记录时，在 vault 外准备完整提案，再为同一目标创建 intent：

```bash
python3 scripts/memoryctl --actor codex prewrite "准备修改受保护文件" \
  --create-intent \
  --target-file "/absolute/path/to/protected-memory.md" \
  --proposal-file "/outside/vault/proposal.md" \
  --source-class user_direct --knowledge-kind rule \
  --asserted-by user --evidence-ref "当前对话中的明确指令"
```

编辑后使用 `claim --intent-id <intent-id> --file <path>`。`advisory` 只报告和保存审计状态，不能把 self-attested 元数据提升为可信授权。

根据结果选择：

- `ADD`：没有相近旧记忆，可以新建。
- `UPDATE`：已有同主题文件，优先更新旧文件。
- `NOOP`：没有长期价值，不写。
- `MARK_OUTDATED`：旧事实过时，在旧文件中标注过时。
- `MERGE_REQUIRED`：疑似重复或冲突，先让用户确认。
- `ASK_USER`：涉及敏感、删除、账号、凭证、费用或高不确定性时先问用户。

## 写入哪里

- 用户稳定偏好或边界：`用户记忆/`
- 某个项目的当前状态：`项目/`
- 可复用的方法流程：`工作流/`
- 明确取舍和原因：`决策/`
- Agent 解决某类问题的经验：`agent/cases/`
- 可能值得升级成 skill 的流程：`agent/skill-candidates/`

## 不写入什么

- API key、token、cookie、密码。
- 一次性闲聊。
- 未验证猜测。
- 已经写过且没有新增信息的重复内容。

## Agent case 规则

满足下面条件时，可以写入 `agent/cases/`：

- 任务已经完成或失败原因清楚。
- 过程对未来有复用价值。
- 能说清楚触发条件、操作步骤、风险和验证方式。

## Skill 候选规则

满足下面条件时，可以写入 `agent/skill-candidates/`：

- 同类 case 至少复用 3 次。
- 至少有 2 条有效证据。
- 步骤稳定，不依赖某个临时项目。
- 风险可控。

正式升级为 skill 前，需要询问用户。

## 收尾动作

写完记忆后，先把修改文件认领到当前会话，再运行统一 closeout：

```bash
python3 scripts/memoryctl --actor codex claim --file "/absolute/path/to/memory.md"
python3 scripts/memoryctl --actor codex closeout --dry-run
python3 scripts/memoryctl --actor codex closeout
```

它会自动完成：

- 只处理当前会话认领的变更文件。
- 检查结构、frontmatter、泄密和变更文件膨胀。
- 对新文件做写入后查重。
- 刷新 SQLite 索引。
- 可选刷新 Zvec 语义索引。
- 必要时刷新 Agent evolution。
- audit 超过间隔时自动捎带运行。
- 写入 closeout 日志。
- 只提交本轮处理过的记忆文件。

如果输出 `MERGE_REQUIRED`、`ASK_USER`、删除文件状态或疑似历史脏变更，不要强行提交。

`[write_intents]` 默认关闭。启用 `advisory` 后只报告受保护路径的意图问题；`enforce` 在没有独立可信审批验证器时稳定返回 `TRUSTED_APPROVAL_VERIFIER_REQUIRED`，不能把本地 CLI 的自报批准当作用户授权。

## Audit 体检

audit 负责发现需要复核、合并或忽略的记忆，不直接修改 Markdown。

```bash
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_audit.py --ignore FINDING_ID --note "保留原因"
python3 scripts/agent_memory_audit_autorun.py --reason closeout --min-interval-days 7
```
