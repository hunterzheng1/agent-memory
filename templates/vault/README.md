# Agent Memory Vault

这是一个 Claude Code、Codex、CodeBuddy 与 Cursor 可共用的本地记忆库模板。真实使用时，可以把这个目录当作普通 Markdown 文件夹；如果使用 Obsidian，也可以把它作为 Obsidian vault 打开。

建议读取顺序：

1. `AGENTS.md`
2. `INDEX.md`
3. 根据任务关键词读取最相关的 1-3 个文件

普通记忆直接写入正式目录，不设置候选池。Agent 自我进化相关内容单独放在 `agent/`。

所有宿主通过 `memoryctl` 使用同一套 `prewrite → claim → closeout` 协议。Markdown 是事实源，SQLite 和 Zvec 都是可重建的索引或旁路。
