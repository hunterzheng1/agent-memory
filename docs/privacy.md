# 隐私检查清单

这个模板不是你的真实记忆库。真实信息应该留在本地私有 vault 里。

Claude Code、Codex、CodeBuddy 与 Cursor 可以读取同一个私有配置源，但真实 Cookie、token 和 API key 仍然不能进入 Markdown vault、宿主规则、auto-memory、搜索日志或公开仓库。推荐把私有值放在 Git 之外的结构化文件或系统凭据存储中，并通过 runner 只向目标子进程注入所需变量；不要让 Agent 打印整个 secrets 文件。

POSIX 平台会检查私有目录和数据库 mode。Windows 权限状态为 `windows_acl_unverified`，表示当前实现没有设置或验证专用 Windows ACL；不要把它解释为已经获得等价的 `0600/0700` 隔离。

## 永远不要放进模板

- `.env`
- SQLite 数据库：`*.sqlite`、`*.db`
- audit 裁决库：`audit_decisions.sqlite`
- closeout/audit 运行日志：`logs/*.jsonl`
- 写意图、安全评估和恢复审计状态库
- API key、token、cookie、密码
- Hugging Face token、模型缓存
- Zvec / LanceDB / Qdrant 等派生向量库
- 真实聊天记录
- 私有项目名和客户名
- 合同、报价、账号、手机号、邮箱、身份证、银行卡
- 真实 Obsidian vault 全量内容

## 推荐做法

- 模板只放 `templates/`、`scripts/`、`docs/`、假示例。
- 本地真实记忆库放在另一个不公开的位置。
- `.env.example` 只放变量名和占位符。
- 文档里的路径使用 `/path/to/...` 或 `$HOME/...`。
- 示例项目统一使用 `example-app`、`demo-user` 这类假名。
- closeout/audit 只能公开脚本，不能公开本地运行产物。
- 搜索日志不得保存查询原文或命中文件绝对路径。新记录只保存查询哈希、长度、来源、路径摘要和计数；检查会清理遗留明文字段。
- `asserted_by`、`approved_by` 和证据引用只保存受限标签或 SHA-256 摘要。它们仍属于 self-attested 元数据，不是数字签名。

## 本地检查命令

```bash
find . -name "*.sqlite" -o -name "*.db" -o -name ".env" -o -name "*.key" -o -name "*.pem" -o -name "zvec" -o -path "*/logs/*.jsonl"
python3 scripts/agent_memory_check.py
```

如果检查结果出现真实 key 或真实路径，先从模板里移除。
