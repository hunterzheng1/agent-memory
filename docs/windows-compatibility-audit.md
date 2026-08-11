# Windows 兼容性审计摘要

Windows 适配保持 Markdown 事实源、SQLite 数据模型、项目边界和 closeout 语义不变。平台差异集中在配置读取、进程锁、文件身份、持久化屏障和 PowerShell 入口。

| 范围 | Windows 风险 | 当前处理 |
| --- | --- | --- |
| 命令分发 | 直接执行无扩展名脚本会触发 `WinError 193` | 子脚本统一由已配置的 Python 解释器启动 |
| 配置读取 | Symlink、Junction 或检查后替换可把读取重定向到外部文件 | 使用 lexical 路径、逐组件 Reparse Point 检查、`open`/`fstat` 身份绑定和读后复核；失败时 fail closed |
| 全局锁 | `fcntl` 不可用，长路径与 NTFS 8.3 别名可能指向同一目录 | Windows 使用 OS 文件锁，并通过目录句柄的最终路径生成稳定锁键 |
| Runtime 发布 | 中途异常、硬终止或掉电可能留下混合版本 | 同卷 staging、manifest-last、持久事务日志、逆序恢复、文件与目录屏障、write-through 原子替换 |
| 路径边界 | 目标、父目录或恢复备份可被 Junction 重定向 | 安装、bootstrap、Hook 和恢复均逐组件拒绝 Reparse Point；外部目标保持不变 |
| Git 对象 | CRLF、clean filter、SHA-256 仓库和 tree mode 会影响内容绑定 | 使用带 `--path` 的 Git 哈希、40/64 位对象 ID、精确 tree mode 与 blob 绑定 |
| SQLite 生命周期 | 事务上下文不保证立即关闭连接 | 安全连接上下文退出时显式关闭，并在可写连接前硬化父目录 |
| PowerShell | Windows PowerShell 5.1 的参数、UTF-8 和 JSON 行为不同于 PowerShell 7 | 所有 `.ps1` 同时进行 5.1/7 语法解析；关键安装与 wrapper 流程在 5.1 实际运行 |
| 自动化 | 只有 Unix/macOS 入口不足以覆盖 Windows | 提供 Stop Hook wrapper、Codex Hook 安装器和 Task Scheduler 管理脚本 |

## 安全与兼容边界

- Windows ACL 当前只报告 `windows_acl_unverified`，不伪装为 POSIX `0600/0700` 已强制。
- Runtime 的 `power_loss_durability` 只描述安装事务。普通 Markdown 编辑仍依赖编辑器、文件系统和用户的备份策略。
- Zvec、Torch 和 EmbeddingGemma 是可选旁路。具体 Windows/Python 组合是否存在第三方 wheel，取决于对应项目的发布物。
- Cursor 已纳入 actor 与检索 scope，但当前没有 Cursor Stop Hook 协议；Cursor 会话需要显式 `AGENT_MEMORY_SESSION_ID` 或 `--session-id`。
- 写意图默认关闭。`advisory` 只提示；没有独立可信审批验证器时，`enforce` fail closed，并返回 `TRUSTED_APPROVAL_VERIFIER_REQUIRED`。
- 公共 CLI 提供的 `approved_by`、`approval_ref` 或 `--confirm-user-authorized` 属于 self-attested 信息，不能单独授权受保护写入、删除或提前提交恢复。

## 验证范围

持续集成覆盖 Linux、macOS 和 Windows。Windows 专项还验证 Python 3.10+ 兼容语法、Windows PowerShell 5.1、PowerShell 7、中文与空格路径、配置升级保留、Hook JSON 幂等、NTFS 长短路径互斥、进程硬终止恢复和持久化 API 错误传播。

这些测试证明已覆盖的代码路径满足契约，不代表所有文件系统、杀毒软件、企业组策略或第三方 Python wheel 组合均已认证。
