<#
.SYNOPSIS
    Merge the Agent Memory hooks into Claude Code settings.json, idempotently.

.DESCRIPTION
    Claude needs three wires for a closed memory loop:

      SessionStart      agent_memory_session_hook.py   bridge session_id
      UserPromptSubmit  agent_memory_prompt_hook.py    automatic recall (read)
      Stop / SessionEnd agent_memory_stop_hook.py      automatic closeout (write)

    Installing only some of them silently breaks the rest: a Stop hook without
    the SessionStart bridge cannot attribute changes and fails every run with
    MISSING_HOST_SESSION_ID. This script always writes the complete set.

    Provider switchers and settings managers regenerate ~/.claude/settings.json
    and drop hooks (see docs/automation.md "Claude Settings Managers"). Re-run
    this script after such an event; it merges into whatever is there now and
    never touches unrelated hooks.

.PARAMETER SettingsPath
    Claude settings file. Defaults to ~/.claude/settings.json.

.PARAMETER ScriptsRoot
    Directory holding the agent_memory_*.py hooks. Defaults to this script's dir.

.PARAMETER Python
    Python executable used in the generated hook commands.

.PARAMETER NoAutoCloseout
    Install Stop in reminder-only mode (no --auto-closeout), so the hook reports
    pending memory instead of committing it.

.PARAMETER DryRun
    Print the resulting hook wiring without writing anything.

.EXAMPLE
    pwsh -File install-claude-hooks.ps1
.EXAMPLE
    pwsh -File install-claude-hooks.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$ScriptsRoot = $PSScriptRoot,
    [string]$Python = 'python',
    [switch]$NoAutoCloseout,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$ScriptsRoot = [System.IO.Path]::GetFullPath($ScriptsRoot)
$required = @(
    'agent_memory_session_hook.py',
    'agent_memory_prompt_hook.py',
    'agent_memory_stop_hook.py'
)
foreach ($name in $required) {
    $candidate = Join-Path $ScriptsRoot $name
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "missing hook script: $candidate"
    }
}

# Forward slashes keep the command valid under Git Bash, which is where Claude
# runs hook commands on Windows. Quote it: the path routinely contains spaces.
function Get-HookCommand([string]$FileName, [string]$Arguments) {
    $full = (Join-Path $ScriptsRoot $FileName).Replace('\', '/')
    return "$Python `"$full`" $Arguments"
}

$stopArgs = '--actor claude --protocol claude --timeout 300'
if (-not $NoAutoCloseout) {
    $stopArgs = '--actor claude --protocol claude --auto-closeout --timeout 300'
}

# Each entry: event -> the single hook command this installer owns.
$managed = [ordered]@{
    'SessionStart'     = @{ Command = (Get-HookCommand 'agent_memory_session_hook.py' '--actor claude'); Timeout = 15 }
    'UserPromptSubmit' = @{ Command = (Get-HookCommand 'agent_memory_prompt_hook.py'  '--actor claude'); Timeout = 15 }
    'Stop'             = @{ Command = (Get-HookCommand 'agent_memory_stop_hook.py'    $stopArgs);        Timeout = 320 }
    'SessionEnd'       = @{ Command = (Get-HookCommand 'agent_memory_stop_hook.py'    '--actor claude --protocol claude --timeout 30'); Timeout = 40 }
}

if (Test-Path -LiteralPath $SettingsPath) {
    $raw = [System.IO.File]::ReadAllText($SettingsPath, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { $raw = '{}' }
    $settings = $raw | ConvertFrom-Json
} else {
    $settings = [pscustomobject]@{}
}

if ($null -eq $settings.PSObject.Properties['hooks']) {
    $settings | Add-Member -NotePropertyName 'hooks' -NotePropertyValue ([pscustomobject]@{})
}
$hooks = $settings.hooks

$added = @(); $updated = @(); $unchanged = @()

foreach ($event in $managed.Keys) {
    $command = $managed[$event].Command
    $timeout = $managed[$event].Timeout

    if ($null -eq $hooks.PSObject.Properties[$event]) {
        $hooks | Add-Member -NotePropertyName $event -NotePropertyValue @()
    }
    # ConvertFrom-Json yields a scalar for single-element arrays; normalize.
    $groups = @($hooks.$event)

    # An entry is "ours" when it invokes the same hook script — regardless of
    # its current flags — so re-running updates in place instead of duplicating.
    $scriptName = if ($command -match 'agent_memory_session_hook') { 'agent_memory_session_hook' }
                  elseif ($command -match 'agent_memory_prompt_hook') { 'agent_memory_prompt_hook' }
                  else { 'agent_memory_stop_hook' }

    $mine = @($groups | Where-Object {
        $_.hooks -and (@($_.hooks) | Where-Object { $_.command -match $scriptName })
    })

    if ($mine.Count -gt 0) {
        $existing = @($mine[0].hooks | Where-Object { $_.command -match $scriptName })[0]
        if ($existing.command -eq $command -and $existing.timeout -eq $timeout) {
            $unchanged += $event
        } else {
            $existing.command = $command
            if ($null -eq $existing.PSObject.Properties['timeout']) {
                $existing | Add-Member -NotePropertyName 'timeout' -NotePropertyValue $timeout
            } else {
                $existing.timeout = $timeout
            }
            $updated += $event
        }
        # Drop any extra duplicates this installer created in earlier versions.
        if ($mine.Count -gt 1) {
            $keep = $mine[0]
            $groups = @($groups | Where-Object { $_ -eq $keep -or -not ($_.hooks -and (@($_.hooks) | Where-Object { $_.command -match $scriptName })) })
            $hooks.$event = $groups
        }
    } else {
        $group = [pscustomobject]@{
            hooks = @([pscustomobject]@{ type = 'command'; command = $command; timeout = $timeout })
        }
        # Prepend: the memory hook must read stdin before any hook that drains it.
        $hooks.$event = @($group) + $groups
        $added += $event
    }
}

Write-Output "settings : $SettingsPath"
Write-Output "scripts  : $ScriptsRoot"
Write-Output "closeout : $(if ($NoAutoCloseout) { 'reminder-only' } else { 'automatic' })"
Write-Output ''
foreach ($event in $managed.Keys) {
    $state = if ($added -contains $event) { 'added' }
             elseif ($updated -contains $event) { 'updated' }
             else { 'unchanged' }
    "{0,-18} {1,-10} {2}" -f $event, $state, $managed[$event].Command | Write-Output
}

if ($DryRun) {
    Write-Output ''
    Write-Output 'dry run: nothing written.'
    return
}

if ($added.Count -eq 0 -and $updated.Count -eq 0) {
    Write-Output ''
    Write-Output 'already up to date; settings not rewritten.'
    return
}

if (Test-Path -LiteralPath $SettingsPath) {
    $backup = "$SettingsPath.bak-agent-memory-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $SettingsPath -Destination $backup
    Write-Output ''
    Write-Output "backup   : $backup"
}

$json = $settings | ConvertTo-Json -Depth 32
[System.IO.File]::WriteAllText($SettingsPath, $json + "`n", $utf8NoBom)

# Fail loudly rather than leave a settings file Claude cannot parse.
$null = [System.IO.File]::ReadAllText($SettingsPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
Write-Output "written  : ok (JSON validated)"
Write-Output ''
Write-Output 'Restart Claude Code, then confirm the hooks are actually loaded'
Write-Output '(a file on disk is not evidence) - see docs/automation.md.'
