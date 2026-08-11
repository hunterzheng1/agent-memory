[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'AgentMemoryVault'),
    [string]$HooksPath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\hooks.json'),
    [switch]$AutoCloseout
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$wrapper = Join-Path $RuntimeRoot 'scripts\stop-hook.ps1'
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Stop Hook wrapper was not found: $wrapper"
}
$wrapper = [System.IO.Path]::GetFullPath($wrapper)
if ($wrapper.IndexOf('"') -ge 0) { throw 'Stop Hook wrapper path may not contain a double quote.' }

$hooksDirectory = Split-Path -Parent $HooksPath
if (-not $hooksDirectory) { throw "Hooks path must have a parent directory: $HooksPath" }
New-Item -ItemType Directory -Force -Path $hooksDirectory | Out-Null

if (Test-Path -LiteralPath $HooksPath -PathType Leaf) {
    try {
        $root = Get-Content -Raw -LiteralPath $HooksPath -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Invalid Codex hooks JSON: $HooksPath"
    }
    if (
        $null -eq $root -or
        $root -is [System.Array] -or
        $root -is [string] -or
        $root -is [ValueType]
    ) {
        throw "Codex hooks JSON root must be an object: $HooksPath"
    }
} elseif (Test-Path -LiteralPath $HooksPath) {
    throw "Codex hooks path is not a regular file: $HooksPath"
} else {
    $root = [pscustomobject]@{}
}

if (-not $root.PSObject.Properties['hooks']) {
    $root | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
} elseif (
    $null -eq $root.hooks -or
    $root.hooks -is [System.Array] -or
    $root.hooks -is [string] -or
    $root.hooks -is [ValueType]
) {
    throw "Codex hooks property must be an object: $HooksPath"
}
if (-not $root.hooks.PSObject.Properties['Stop']) {
    $root.hooks | Add-Member -NotePropertyName Stop -NotePropertyValue @()
} elseif ($root.hooks.Stop -is [string] -or $root.hooks.Stop -is [ValueType]) {
    throw "Codex Stop hooks must be an array or object: $HooksPath"
}

$mode = if ($AutoCloseout) { ' -AutoCloseout' } else { '' }
$command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Actor codex -Protocol codex -Event stop-hook{1}' -f $wrapper, $mode
$alreadyInstalled = $false
foreach ($group in @($root.hooks.Stop)) {
    if ($null -eq $group) { continue }
    foreach ($hook in @($group.hooks)) {
        if ($null -eq $hook) { continue }
        $commandText = [string]$hook.command
        if ($commandText.IndexOf($wrapper, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $alreadyInstalled = $true
        }
    }
}

if (-not $alreadyInstalled) {
    $entry = [pscustomobject]@{
        hooks = @(
            [pscustomobject]@{
                type = 'command'
                command = $command
                timeout = $(if ($AutoCloseout) { 320 } else { 20 })
            }
        )
    }
    $root.hooks.Stop = @($root.hooks.Stop) + @($entry)
}

$json = $root | ConvertTo-Json -Depth 32
$temporaryPath = Join-Path $hooksDirectory ('.agent-memory-hooks-' + [Guid]::NewGuid().ToString('N') + '.tmp')
try {
    [System.IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $utf8)
    Move-Item -LiteralPath $temporaryPath -Destination $HooksPath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

if ($alreadyInstalled) {
    Write-Output "[OK] Codex Stop Hook already installed: $HooksPath"
} else {
    Write-Output "[OK] Codex Stop Hook installed: $HooksPath"
}
Write-Output '[WARN] Confirm that [features] hooks = true is enabled in ~/.codex/config.toml.'
