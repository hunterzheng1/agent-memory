# CodeBuddy Memory Instructions

这是本地长期记忆库。遇到既有项目、仓库、路径、人物、历史结论、继续上次任务、报告、调研、较长排查时，默认先使用这个记忆库；简单翻译、改一句话、查时间等一次性小任务可以跳过。

本 vault 是项目级长期记忆层，与 CodeBuddy 全局配置（`~/.codebuddy/`）互补不替代：全局配置管 hooks/settings，本 vault 管稳定事实与状态。

> Codex / Claude 用户看 `AGENTS.md`；Cursor 用户看项目 `.cursor/rules/agent-memory.mdc` 或 Cursor User Rule。规则等价，写入时用各自 `agent_id`。

读取顺序：

1. 先读本文件。
2. 再读 `INDEX.md`。
3. 根据任务关键词，只读最相关的 1-3 个文件。

不要默认读取整个记忆库。

## 检索规则

优先使用统一入口：

```bash
python3 scripts/memoryctl --actor codebuddy search "查询词" --limit 5
```

它会先查 SQLite/FTS；启用语义索引时，也可以并行查 Zvec。Zvec 命中只能当作候选线索，最终回答前必须回读 Markdown 原文。

## 写入规则

正式写入前先做对账：

```bash
python3 scripts/memoryctl --actor codebuddy prewrite "准备写入的记忆摘要"
```

对账动作只允许：`ADD`、`UPDATE`、`NOOP`、`MARK_OUTDATED`、`MERGE_REQUIRED`、`ASK_USER`。

每次新建或修改正式记忆后，认领到当前会话：

```bash
python3 scripts/memoryctl --actor codebuddy claim --file "/absolute/path/to/memory.md"
```

CodeBuddy Bash 会原生注入 `CODEBUDDY_SESSION_ID`，**默认不需要** SessionStart 桥接（与 Claude 的 `CLAUDE_ENV_FILE` 不同）。Stop Hook 只处理当前会话认领的文件。

重要任务结束前：

```bash
python3 scripts/memoryctl --actor codebuddy closeout --dry-run
python3 scripts/memoryctl --actor codebuddy closeout
```

会话内 closeout 只提交本会话认领的文件；人工全库收尾才用 `memoryctl ... closeout --global`。若输出 `MERGE_REQUIRED`、`ASK_USER`、删除文件或疑似历史脏变更，先停下让用户确认。

## Audit 规则

```bash
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_doctor.py
```

## 字段要求

```yaml
---
memory_type: project
track: project
project_id: example-app
app_id: {{APP_ID}}
user_id: {{USER_ID}}
agent_id: codebuddy
agent_scope: shared
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
review_after_days: 90
keywords:
  - example
---
```

## 安全边界

- 不要把 API key、token、cookie、密码写入 Markdown。
- 不要把私密原始聊天全文写入公开仓库。
- 不要把 SQLite 数据库提交到 Git。
- 搜索日志只保存查询哈希、长度、来源和耗时，不保存新的查询原文。
- 对外分享前必须脱敏。
- 正式事实以 Markdown vault 为准；不要把 CodeBuddy 本地草稿当唯一真相源。
