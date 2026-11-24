<#
.SYNOPSIS
    Install the Agent Memory bridge extension into Pi, idempotently.

.DESCRIPTION
    Pi has no JSON hook config: host integration is done with a TypeScript
    extension. This script copies templates/pi/agent-memory.ts into Pi's user
    extension directory so the memory loop is closed with a thin adapter.

    What the extension does (read-only reminders, never auto-commits):
      tool_result    track memory files written this turn
      agent_settled  memoryctl claim those files, then closeout --dry-run and
                     report MERGE_REQUIRED / ASK_USER / deletion signals

    The formal closeout (with scoped commit) stays an explicit agent action per
    the vault AGENTS.md.

    Why the extension must use the installed Runtime memoryctl: the source repo
    has no config/agent-memory.toml, so closeout falls back to template defaults
    (VAULT_ROOT=templates/vault, REPO_ROOT=source repo) and then compares vault
    history against the source repo HEAD, which always fails with
    "closeout git baseline is not an ancestor of HEAD". Install the Runtime first
    (scripts/install_runtime.py) and point the extension at it — that is the
    default when AGENT_MEMORY_MEMORYCTL / AGENT_MEMORY_CONFIG_ROOT are unset.

.PARAMETER ExtensionDir
    Pi user extension directory. Defaults to ~/.pi/agent/extensions.

.PARAMETER Force
    Overwrite an existing agent-memory.ts. Without it the script refuses and
    prints the diff hint, so a locally tuned copy is never clobbered silently.

.PARAMETER DryRun
    Print what would happen without writing anything.

.EXAMPLE
    pwsh -File install-pi-extension.ps1 -DryRun
.EXAMPLE
    pwsh -File install-pi-extension.ps1
#>
[CmdletBinding()]
param(
    [string]$ExtensionDir = (Join-Path $env:USERPROFILE '.pi\agent\extensions'),
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$source = Join-Path $PSScriptRoot '..\templates\pi\agent-memory.ts'
$source = [System.IO.Path]::GetFullPath($source)
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "template missing: $source"
}

$ExtensionDir = [System.IO.Path]::GetFullPath($ExtensionDir)
$target = Join-Path $ExtensionDir 'agent-memory.ts'

Write-Output "template : $source"
Write-Output "target   : $target"
Write-Output ''

if (-not (Test-Path -LiteralPath $ExtensionDir -PathType Container)) {
    if ($DryRun) {
        Write-Output 'would create the extension directory.'
    } else {
        New-Item -ItemType Directory -Force -Path $ExtensionDir | Out-Null
        Write-Output 'created  : extension directory'
    }
}

$exists = Test-Path -LiteralPath $target -PathType Leaf
if ($exists) {
    $same = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq
            (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    if ($same) {
        Write-Output 'already up to date; nothing written.'
        return
    }
    if (-not $Force) {
        Write-Output 'target exists and differs; refusing to overwrite.'
        Write-Output 'Review the difference, then re-run with -Force to replace it.'
        return
    }
}

if ($DryRun) {
    Write-Output $(if ($exists) { 'would replace existing agent-memory.ts.' } else { 'would install agent-memory.ts.' })
    Write-Output ''
    Write-Output 'dry run: nothing written.'
    return
}

if ($exists) {
    $backup = "$target.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $target -Destination $backup
    Write-Output "backup   : $backup"
}

Copy-Item -LiteralPath $source -Destination $target -Force
Write-Output 'written  : ok'
Write-Output ''
Write-Output 'Restart Pi (or start a new session), then confirm the bridge actually runs:'
Write-Output '  - write to a file under the memory vault in a Pi session'
Write-Output '  - on settle you should see an "agent-memory" notification'
Write-Output '  - cross-check the ledger:'
Write-Output '      memoryctl --actor pi closeout --dry-run'
Write-Output '    A file on disk is not evidence; the dry-run output is.'
