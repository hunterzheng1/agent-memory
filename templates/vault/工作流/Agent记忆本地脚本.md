---
memory_type: workflow
track: workflow
project_id: agent-memory-vault-scripts
app_id: {{APP_ID}}
user_id: {{USER_ID}}
agent_id: {{AGENT_ID}}
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
keywords:
  - scripts
  - sqlite
---

# Agent 记忆本地脚本

## 当前有效摘要

本模板提供以下本地脚本：

- `agent_memory_index.py`：全库 Markdown 索引和搜索。
- `agent_memory_search.py`：统一检索入口，合并 SQLite、可选 Zvec 和手动 rg 结果。
- `agent_memory_closeout.py`：任务结束收尾，负责检查、对账、刷新索引、捎带 audit 和可选 scoped commit。
- `agent_memory_claim.py`：记录当前会话负责的记忆文件，并预览或显式过期异常退出遗留的旧认领。
- `agent_memory_intent.py`：创建、验证、完成或取消受保护写入的内容绑定 intent 与 receipt。
- `agent_memory_safety.py`：按来源类别和知识类型执行写入前安全评估。
- `agent_memory_state.py`：建立安全 SQLite 连接，并报告平台权限模型。
- `agent_memory_decision_outcomes.py`：只读汇总“决策—结果记录”的复盘覆盖率。
- `agent_memory_policy_benchmark.py`：离线运行对账与来源安全策略基准。
- `agent_memory_audit.py`：定期体检，发现过期记忆、重复标题、open-loop 噪声和已过时状态。
- `agent_memory_audit_autorun.py`：自动触发器，只在超过设定间隔时运行内容 audit，并顺带执行只读 Doctor，把基础设施健康报告写入 `latest-doctor.json`。
- `agent_memory_doctor.py`：统一体检 Markdown、SQLite、FTS、INDEX、Zvec、远端备份、会话认领、语义 Python、验证来源和自动化状态。
- `agent_memory_stop_hook.py`：Stop 事件节流提醒；到期 audit 仍由 7 天闸门决定是否执行。
- `agent_memory_evolution.py`：Agent case 和 skill 候选状态统计。
- `agent_memory_check.py`：结构、frontmatter、SQLite、泄密风险检查。
- `agent_memory_zvec_index.py`：可选 Zvec 语义索引和搜索。
- `agent_memory_retrieval_benchmark.py`：对比 SQLite 和向量检索召回效果。

## 环境变量

```bash
AGENT_MEMORY_ROOT=/path/to/your/agent-memory-vault
AGENT_MEMORY_GIT_ROOT=/path/to/git-root-containing-the-vault
AGENT_MEMORY_CONFIG_ROOT=$HOME/.config/agent-memory
AGENT_MEMORY_STATE_DB=$HOME/.config/agent-memory/state.sqlite
AGENT_MEMORY_USER_ID=demo-user
AGENT_MEMORY_AGENT_ID=codex
AGENT_MEMORY_APP_ID=codex
AGENT_MEMORY_AUDIT_DB=$HOME/.config/agent-memory/audit_decisions.sqlite
AGENT_MEMORY_CLOSEOUT_LOG=$HOME/.config/agent-memory/logs/closeout.jsonl
AGENT_MEMORY_PYTHON=python3
AGENT_MEMORY_ZVEC_PYTHON=python3
AGENT_MEMORY_VECTOR_DIR=$HOME/.config/agent-memory/zvec/memory_chunks_embeddinggemma_768
AGENT_MEMORY_EMBEDDING_MODEL=google/embeddinggemma-300m
```

## 常用命令

```bash
python3 scripts/agent_memory_index.py --init --scan --report
python3 scripts/agent_memory_search.py "关键词" --limit 5
python3 scripts/memoryctl --actor codex prewrite "准备写入的记忆摘要" --source-class user_direct --knowledge-kind fact --asserted-by user --evidence-ref "当前对话"
python3 scripts/memoryctl --actor codex claim --file "/absolute/path/to/memory.md"
python3 scripts/memoryctl --actor codex closeout --dry-run
python3 scripts/memoryctl --actor codex closeout
python3 scripts/memoryctl --actor human claims-expire --older-than-hours 24 --json
python3 scripts/memoryctl --actor human decision-outcomes --strict --json
python3 scripts/memoryctl --actor human policy-benchmark --kind all --json
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_audit_autorun.py --reason manual --json
python3 scripts/agent_memory_doctor.py
python3 scripts/agent_memory_evolution.py --init --scan --report
python3 scripts/agent_memory_check.py
python3 scripts/agent_memory_zvec_index.py --init
python3 scripts/agent_memory_zvec_index.py --scan --prune
python3 scripts/agent_memory_zvec_index.py --report
python3 scripts/agent_memory_zvec_index.py --search "只记得大概意思的问题" --limit 5
python3 scripts/agent_memory_retrieval_benchmark.py --limit 5
```

`memoryctl --actor` 支持 `codex`、`claude`、`codebuddy`、`cursor`、`human`、`migration` 和 `test`。普通 Agent 写入必须有稳定会话 ID；Cursor 需要显式设置 `AGENT_MEMORY_SESSION_ID` 或传入 `--session-id`。

## 下次优先看

- 修改目录结构后，先更新字段规范，再跑检查脚本。
