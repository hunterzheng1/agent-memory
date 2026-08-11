# Windows 原生安装与运行

项目支持 Windows 10/11、Python 3.10+、Git、Windows PowerShell 5.1 和 PowerShell 7。Obsidian 是可选界面，不是运行依赖。

## 安装前确认

- 使用普通用户权限运行安装器。
- 安装 Python 3 和 Git，并确保当前 PowerShell 能找到它们。
- `MemoryRoot`、`ConfigRoot` 和 Hook 路径不能经过符号链接、Junction 或其他 Reparse Point。安装器遇到重定向路径时会停止，不会继续写入外部目标。

## 全新安装

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 `
  -MemoryRoot "$HOME\Documents\Agent Memory Vault" `
  -ConfigRoot "$env:LOCALAPPDATA\AgentMemoryVault"
```

`Bypass` 只作用于当前 PowerShell 进程。安装器依次创建私有虚拟环境、发布可校验 Runtime、写入配置、初始化 vault、创建首个 Git 基线、初始化索引，并运行 check 与 doctor。全新 vault 的 `.obsidian/` 已忽略，不会进入 Git 基线。

默认会初始化 Git。已有 vault、已有 Git 仓库或位于其他仓库内的目录不会被安装器擅自 stage 或提交。确实不需要初始化时，传入 `-NoInitGit`。

## 升级

使用相同的 `MemoryRoot` 和 `ConfigRoot` 再次运行安装命令。升级默认逐字节保留已有 `config/agent-memory.toml`，不会把示例值覆盖到私人配置。

只有需要明确重建配置并已经备份原文件时，才使用：

```powershell
.\scripts\install-windows.ps1 `
  -MemoryRoot "$HOME\Documents\Agent Memory Vault" `
  -ConfigRoot "$env:LOCALAPPDATA\AgentMemoryVault" `
  -OverwriteConfig
```

安装器会在每个副作用边界重新验证配置路径与文件身份。并发编辑、替换为链接、内容摘要变化或 Runtime 发布冲突都会使安装停止。

## Runtime 恢复与持久化边界

`runtime-manifest.json` 包含两个可检查字段：

```json
{
  "process_crash_recovery": true,
  "power_loss_durability": "verified"
}
```

- `process_crash_recovery`：发布使用持久事务日志。进程在 staging、脚本发布、manifest 发布或恢复途中终止时，下一次安装会在取得同一 OS 文件锁后继续恢复或完成升级。
- `power_loss_durability`：Runtime 发布路径使用文件与目录持久化屏障；Windows 替换使用 `MoveFileExW` 的 `REPLACE_EXISTING | WRITE_THROUGH`。文件系统不支持所需屏障时，安装器返回 `windows_durability_unsupported` 并停止。

这两个字段描述 Runtime 安装事务，不表示 Markdown vault 的每次人工编辑都自动获得同等断电保证。不要手工删除带恢复证据的 `.runtime-stage-*` 目录；错误输出中的 `recovery_path` 用于人工检查。

验证已安装 Runtime：

```powershell
$runtime = "$env:LOCALAPPDATA\AgentMemoryVault"
& "$runtime\.venv\Scripts\python.exe" `
  "$runtime\scripts\install_runtime.py" `
  --config-root $runtime --verify --json
```

## 日常命令

```powershell
$runtime = "$env:LOCALAPPDATA\AgentMemoryVault"
$python = "$runtime\.venv\Scripts\python.exe"
$memoryctl = "$runtime\scripts\memoryctl"

& $python $memoryctl --actor codex search "项目状态" --limit 5
& $python $memoryctl --actor codex closeout --dry-run
& $python $memoryctl --actor human doctor
& $python $memoryctl --actor human check --skip-state-db
```

Python 直接读取 Runtime TOML。配置文件读取使用逐组件检查、稳定文件描述符和读后身份复核；显式配置缺失或 TOML 无效时不会静默回退到另一套配置。

## Codex Stop Hook

安装 Runtime 时可同时安装 Codex Hook：

```powershell
.\scripts\install-windows.ps1 `
  -MemoryRoot "$HOME\Documents\Agent Memory Vault" `
  -InstallCodexHook `
  -AutoCloseout
```

也可以单独运行：

```powershell
.\scripts\install-codex-hook.ps1 `
  -RuntimeRoot "$env:LOCALAPPDATA\AgentMemoryVault" `
  -AutoCloseout
```

脚本保留 `hooks.json` 中无关条目。同一受管 wrapper 再次安装时会更新命令与超时；切换 `-AutoCloseout` 不会静默保留旧模式。写入采用内容快照、父目录锁和原子替换；路径重定向或并发编辑时停止。

确认 `%USERPROFILE%\.codex\config.toml` 已启用 Hook：

```toml
[features]
hooks = true
```

## Task Scheduler audit

```powershell
.\scripts\audit-task.ps1 install
.\scripts\audit-task.ps1 status
.\scripts\audit-task.ps1 run
.\scripts\audit-task.ps1 uninstall
```

任务固定在 `\AgentMemory\` 路径下，并按精确任务名操作。`status`、`run` 和 `uninstall` 不要求 Python 仍然存在；只有 `install` 解析 Python。先用 `-PlanOnly` 查看安装计划，不会创建或删除任务。

## 权限说明

POSIX 平台会检查并收紧私有目录和数据库 mode。Windows 使用 ACL，但当前实现没有设置或验证专用 ACL，因此状态报告为：

```text
permission_model=windows_acl_unverified
mode_enforced=false
```

不要把该状态解释为已经获得等价的 `0600/0700` 权限隔离。需要严格隔离时，使用 Windows ACL 管理工具另行配置并核验。

## 常见错误

- `running scripts is disabled`：仅对当前进程使用示例中的 `-ExecutionPolicy Bypass`，不要永久设置 `Unrestricted`。
- `installer_preflight=error`：检查目标是否为目录、文件、符号链接或 Junction；修复路径后重新运行。
- `runtime install is already in progress`：另一个安装进程持有同一 OS 锁。等待它结束后重试。
- `windows_durability_unsupported`：当前文件系统或 API 无法提供安装器要求的持久化屏障。停止升级，不要绕过检查。
- `config_validation=error`：显式配置缺失、无效或在读取期间变化。修复原配置，不要让安装器回退到默认路径。
- 中文乱码：使用仓库提供的 PowerShell wrapper。它会为 Python 子进程固定 UTF-8 输入输出。
