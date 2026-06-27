---
memory_type: routing
track: routing
project_id: demo-app
app_id: demo
user_id: demo-user
agent_id: claude
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
keywords:
  - example
  - claude
---

# Claude Code Memory Instructions（示例）

demo-vault 示例规则文件，展示 Claude Code 会话启动时如何被引导读取记忆库。真实 vault 的完整规则见 `templates/vault/CLAUDE.md`。

本 vault 是项目级长期记忆，与 Claude 全局记忆（`~/.claude/`）互补不替代。

## 安全边界

- 不写 API key、token、cookie、密码。
- 不提交 SQLite 数据库到 Git。
- 对外分享前脱敏。
