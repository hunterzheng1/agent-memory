# Cursor User Rule Template

Use this as a Cursor User Rule when you want Cursor to use the same Agent Memory vault across projects. You can paste it into Cursor Settings, or save it as a file-backed rule at `~/.cursor/rules/agent-memory.mdc`. Replace paths for your machine before saving.

```text
You have access to a shared long-term Agent Memory vault.

Vault root:
- Prefer AGENT_MEMORY_ROOT.
- If unset, use: <absolute path to your Agent Memory vault>

Memory tooling repository:
- Prefer the current repo if it contains scripts/agent_memory_search.py.
- Otherwise use: <absolute path to the agent-memory repo>

When a task involves an existing project, previous conclusions, local paths, user preferences, reports, research, continuing prior work, or substantial debugging, read memory first:

1. Read <vault root>/AGENTS.md.
2. Read <vault root>/INDEX.md.
3. Search with:
   python "<agent-memory repo>/scripts/agent_memory_search.py" "keywords" --limit 5
4. Read only the most relevant 1-3 Markdown files before answering.

Do not read the entire vault by default.

Cursor may write durable memory when the user asks to remember something or when an important task produces stable facts, decisions, workflows, or reusable agent experience. Before writing, reconcile with:

python "<agent-memory repo>/scripts/agent_memory_closeout.py" --prewrite "summary to write"

When writing memory, use agent_id: cursor. After writing, run:

python "<agent-memory repo>/scripts/agent_memory_closeout.py" --commit

If AGENT_MEMORY_AUTO_COMMIT=1 is enabled, a non-dry-run closeout may commit processed memory files by default. --dry-run never commits. Stop and ask the user on MERGE_REQUIRED, ASK_USER, deleted files, or warnings.

Never write API keys, tokens, cookies, passwords, private raw chat transcripts, SQLite databases, vector stores, or model caches into Markdown or a public repository.
```

Cursor's current rules documentation lives under the official Cursor docs: <https://cursor.com/docs>.
