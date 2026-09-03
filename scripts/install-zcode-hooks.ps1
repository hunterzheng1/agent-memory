<#
.SYNOPSIS
    Merge the Agent Memory hooks into the ZCode CLI config, idempotently.

.DESCRIPTION
    ZCode needs three wires for a closed memory loop (it has no SessionEnd
    event, so the Claude fourth wire does not exist here):

      SessionStart      agent_memory_session_context_hook.py  surface session id in context
      UserPromptSubmit  agent_memory_prompt_hook.py           automatic recall (read)
      Stop              agent_memory_stop_hook.py             automatic closeout (write)

    No SessionStart env bridge is needed: ZCode injects CLAUDE_SESSION_ID into
    hook processes directly, and the zcode actor resolves it (see
    agent_memory_host.py). The session-context hook only republishes that id
    into model context so manual claim/closeout calls can pass --session-id.

    Configuration-file hooks are disabled by default in ZCode; this script
    always sets hooks.enabled = true. Re-run after any change to the hook
    scripts' arguments; the merge never touches unrelated config or hooks.

    Note: ZCode's dataBaseDir redirect (ZCODE_DATA_BASE_DIR) moves workspace
    and plugin data, but the CLI config stays at ~/.zcode/cli/config.json.

.PARAMETER ConfigPath
    ZCode CLI config file. Defaults to ~/.zcode/cli/config.json.

.PARAMETER ScriptsRoot
    Directory holding the agent_memory_*.py hooks. Defaults to this script's dir.

.PARAMETER Python
    Executable used as the process hook command. Hooks are type "process"
    (argument vector, no shell), so the space-containing path needs no quoting.

.PARAMETER NoAutoCloseout
    Install Stop in reminder-only mode (no --auto-closeout), so the hook reports
    pending memory instead of committing it.

.PARAMETER DryRun
    Print the resulting hook wiring without writing anything.

.EXAMPLE
    pwsh -File install-zcode-hooks.ps1
.EXAMPLE
    pwsh -File install-zcode-hooks.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.zcode\cli\config.json'),
    [string]$ScriptsRoot = $PSScriptRoot,
    [string]$Python = 'python',
    [switch]$NoAutoCloseout,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$ScriptsRoot = [System.IO.Path]::GetFullPath($ScriptsRoot)
$required = @(
    'agent_memory_session_context_hook.py',
    'agent_memory_prompt_hook.py',
    'agent_memory_stop_hook.py'
)
foreach ($name in $required) {
    $candidate = Join-Path $ScriptsRoot $name
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "missing hook script: $candidate"
    }
}

function Get-HookArgs([string]$FileName, [string[]]$Arguments) {
    $full = (Join-Path $ScriptsRoot $FileName).Replace('\', '/')
    return @($full) + $Arguments
}

$stopArgs = @('--actor', 'zcode', '--protocol', 'claude', '--timeout', '300')
if (-not $NoAutoCloseout) {
    $stopArgs = @('--actor', 'zcode', '--protocol', 'claude', '--auto-closeout', '--timeout', '300')
}

# Each entry: event -> the single process hook this installer owns.
# timeoutMs is per-hook and overrides the runner default of 60000; closeout
# commits to the vault git repo and can legitimately take minutes.
$managed = [ordered]@{
    'SessionStart'     = @{
        Args     = [string[]](Get-HookArgs 'agent_memory_session_context_hook.py' @())
        TimeoutMs = 15000
        Status   = '记忆库会话桥接…'
    }
    'UserPromptSubmit' = @{
        # Measured search subprocess cost is ~6s on this machine (SQLite+Zvec
        # warm-up); the hook's 8s default would time out under load and
        # silently skip recall, so widen the internal cap accordingly.
        Args     = [string[]](Get-HookArgs 'agent_memory_prompt_hook.py' @('--actor', 'zcode', '--timeout', '20'))
        TimeoutMs = 25000
        Status   = '记忆库召回…'
    }
    'Stop'             = @{
        Args     = [string[]](Get-HookArgs 'agent_memory_stop_hook.py' $stopArgs)
        TimeoutMs = 320000
        Status   = '记忆库收尾…'
    }
}

if (Test-Path -LiteralPath $ConfigPath) {
    $raw = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { $raw = '{}' }
    $config = $raw | ConvertFrom-Json
} else {
    $config = [pscustomobject]@{}
}

if ($null -eq $config.PSObject.Properties['hooks']) {
    $config | Add-Member -NotePropertyName 'hooks' -NotePropertyValue ([pscustomobject]@{})
}
$hooksRoot = $config.hooks

# Configuration-file hooks are disabled by default; without this flag the
# runner never starts and every hook below is silently skipped.
if ($null -eq $hooksRoot.PSObject.Properties['enabled']) {
    $hooksRoot | Add-Member -NotePropertyName 'enabled' -NotePropertyValue $true
} elseif ($hooksRoot.enabled -ne $true) {
    $hooksRoot.enabled = $true
}

if ($null -eq $hooksRoot.PSObject.Properties['events']) {
    $hooksRoot | Add-Member -NotePropertyName 'events' -NotePropertyValue ([pscustomobject]@{})
}
$events = $hooksRoot.events

$added = @(); $updated = @(); $unchanged = @()

foreach ($event in $managed.Keys) {
    # [string[]] matters: Get-HookArgs' single-element output unrolls to a
    # scalar through the hashtable, which ConvertTo-Json then writes as a
    # bare string instead of the array ZCode's schema requires.
    $hookArgs = [string[]]$managed[$event].Args
    $timeoutMs = $managed[$event].TimeoutMs
    $status = $managed[$event].Status

    if ($null -eq $events.PSObject.Properties[$event]) {
        $events | Add-Member -NotePropertyName $event -NotePropertyValue @()
    }
    # ConvertFrom-Json yields a scalar for single-element arrays; normalize.
    $groups = @($events.$event)

    # A group is "ours" when one of its hooks invokes one of our hook scripts
    # — regardless of current flags — so re-running updates in place instead
    # of duplicating.
    $scriptName = ($hookArgs[0] -split '/')[-1]

    $mine = @($groups | Where-Object {
        $_.hooks -and (@($_.hooks) | Where-Object { ($_.args -join ' ') -like "*$scriptName*" })
    })

    if ($mine.Count -gt 0) {
        $existing = @($mine[0].hooks | Where-Object { ($_.args -join ' ') -like "*$scriptName*" })[0]
        # A scalar args (earlier single-element collapse) is invalid for the
        # process schema - force the update path so it is rewritten as an array.
        $argsIsArray = ($existing.args -is [array])
        if ($argsIsArray -and ($existing.args -join "`0") -eq ($hookArgs -join "`0") -and $existing.timeoutMs -eq $timeoutMs) {
            $unchanged += $event
        } else {
            $existing.args = $hookArgs
            foreach ($pair in @(@('timeoutMs', $timeoutMs), @('statusMessage', $status))) {
                if ($null -eq $existing.PSObject.Properties[$pair[0]]) {
                    $existing | Add-Member -NotePropertyName $pair[0] -NotePropertyValue $pair[1]
                } else {
                    $existing.$($pair[0]) = $pair[1]
                }
            }
            if ($existing.type -ne 'process') { $existing.type = 'process' }
            $updated += $event
        }
        # Drop any extra duplicates this installer created in earlier versions.
        if ($mine.Count -gt 1) {
            $keep = $mine[0]
            $groups = @($groups | Where-Object { $_ -eq $keep -or -not ($_.hooks -and (@($_.hooks) | Where-Object { ($_.args -join ' ') -like "*$scriptName*" })) })
            $events.$event = $groups
        }
    } else {
        $group = [pscustomobject]@{
            hooks = @([pscustomobject]@{
                type = 'process'
                command = $Python
                args = $hookArgs
                timeoutMs = $timeoutMs
                statusMessage = $status
            })
        }
        # Prepend: the memory hook must read stdin before any hook that drains it.
        $events.$event = @($group) + $groups
        $added += $event
    }
}

Write-Output "config   : $ConfigPath"
Write-Output "scripts  : $ScriptsRoot"
Write-Output "closeout : $(if ($NoAutoCloseout) { 'reminder-only' } else { 'automatic' })"
Write-Output ''
foreach ($event in $managed.Keys) {
    $state = if ($added -contains $event) { 'added' }
             elseif ($updated -contains $event) { 'updated' }
             else { 'unchanged' }
    "{0,-18} {1,-10} {2}" -f $event, $state, ($managed[$event].Args -join ' ') | Write-Output
}

if ($DryRun) {
    Write-Output ''
    Write-Output 'dry run: nothing written.'
    return
}

if ($added.Count -eq 0 -and $updated.Count -eq 0 -and $unchanged.Count -eq $managed.Keys.Count) {
    Write-Output ''
    Write-Output 'already up to date; config not rewritten.'
    return
}

if (Test-Path -LiteralPath $ConfigPath) {
    $backup = "$ConfigPath.bak-agent-memory-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $ConfigPath -Destination $backup
    Write-Output ''
    Write-Output "backup   : $backup"
}

$json = $config | ConvertTo-Json -Depth 32
[System.IO.File]::WriteAllText($ConfigPath, $json + "`n", $utf8NoBom)

# Fail loudly rather than leave a config file ZCode cannot parse.
$null = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
Write-Output "written  : ok (JSON validated)"
Write-Output ''
Write-Output 'Restart ZCode (or start a new session), then confirm the hooks actually'
Write-Output 'run: check ~/.config/agent-memory/logs/session-context-hook.jsonl and'
Write-Output 'prompt-hook.jsonl - a file on disk is not evidence.'
