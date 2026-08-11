[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'AgentMemoryVault'),
    [string]$HooksPath = '',
    [switch]$AutoCloseout
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$testBarrier = [string]$env:_AGENT_MEMORY_TEST_HOOK_BARRIER
$afterCasTestBarrier = [string]$env:_AGENT_MEMORY_TEST_HOOK_AFTER_CAS_BARRIER

if (-not ('AgentMemoryHookFileIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class AgentMemoryHookFileIdentity
{
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName,
        uint desiredAccess,
        FileShare shareMode,
        IntPtr securityAttributes,
        FileMode creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle handle,
        out ByHandleFileInformation information);

    public static string ForPath(string path, bool directory)
    {
        const uint FileFlagBackupSemantics = 0x02000000;
        const uint FileFlagOpenReparsePoint = 0x00200000;
        uint flags = FileFlagOpenReparsePoint;
        if (directory) flags |= FileFlagBackupSemantics;
        using (SafeFileHandle handle = CreateFile(
            path,
            0,
            FileShare.ReadWrite | FileShare.Delete,
            IntPtr.Zero,
            FileMode.Open,
            flags,
            IntPtr.Zero))
        {
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(handle, out information))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            ulong index = ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow;
            return information.VolumeSerialNumber.ToString("x8") + ":" + index.ToString("x16");
        }
    }
}
'@
}

function Get-LexicalPathComponents([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        throw "Codex hook path is not absolute: $fullPath"
    }
    $components = New-Object System.Collections.Generic.List[string]
    [void]$components.Add($pathRoot)
    $cursor = $pathRoot
    $relative = $fullPath.Substring($pathRoot.Length)
    $separators = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    foreach ($part in $relative.Split($separators, [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $part
        [void]$components.Add($cursor)
    }
    return $components.ToArray()
}

function Assert-HookPath(
    [string]$Path,
    [ValidateSet('File', 'Directory', 'Any')]
    [string]$ExpectedKind = 'Any'
) {
    $components = @(Get-LexicalPathComponents $Path)
    for ($index = 0; $index -lt $components.Count; $index++) {
        $component = $components[$index]
        $final = $index -eq ($components.Count - 1)
        try {
            $item = Get-Item -LiteralPath $component -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            return
        } catch {
            throw "Codex hook path metadata failed: $component"
        }
        $attributes = [System.IO.FileAttributes]$item.Attributes
        if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Codex hook path must not contain a symlink or reparse point: $component"
        }
        if (-not $final -and -not $item.PSIsContainer) {
            throw "Codex hook path parent is not a directory: $component"
        }
        if ($final -and $ExpectedKind -eq 'Directory' -and -not $item.PSIsContainer) {
            throw "Codex hook path is not a directory: $component"
        }
        if ($final -and $ExpectedKind -eq 'File') {
            $isDevice = ($attributes -band [System.IO.FileAttributes]::Device) -ne 0
            if ($item.PSIsContainer -or $isDevice -or -not ($item -is [System.IO.FileInfo])) {
                throw "Codex hook path is not a regular file: $component"
            }
        }
    }
}

function Ensure-HookDirectory([string]$Path) {
    foreach ($component in @(Get-LexicalPathComponents $Path)) {
        try {
            $item = Get-Item -LiteralPath $component -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            [void][System.IO.Directory]::CreateDirectory($component)
            $item = Get-Item -LiteralPath $component -Force -ErrorAction Stop
        }
        $attributes = [System.IO.FileAttributes]$item.Attributes
        if (
            ($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not $item.PSIsContainer
        ) {
            throw "Codex hook directory must not be redirected: $component"
        }
    }
}

function Get-HookPathState([string]$Path) {
    Assert-HookPath -Path $Path -ExpectedKind Any
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return 'missing'
    }
    $attributes = [System.IO.FileAttributes]$item.Attributes
    try {
        $identity = [AgentMemoryHookFileIdentity]::ForPath($Path, [bool]$item.PSIsContainer)
    } catch {
        throw "Codex hook path identity failed: $Path"
    }
    $state = @(
        'present',
        $item.GetType().FullName,
        [string]$item.PSIsContainer,
        [string][int]$attributes,
        [string]$item.CreationTimeUtc.Ticks,
        $identity
    )
    if ($item -is [System.IO.FileInfo]) {
        $stream = $null
        $hasher = $null
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
            $hasher = [System.Security.Cryptography.SHA256]::Create()
            $hashBytes = $hasher.ComputeHash($stream)
            $contentHash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
            $after = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
            $afterIdentity = [AgentMemoryHookFileIdentity]::ForPath($Path, $false)
        } finally {
            if ($null -ne $hasher) { $hasher.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
        }
        $afterAttributes = [System.IO.FileAttributes]$after.Attributes
        if (
            ($afterAttributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not ($after -is [System.IO.FileInfo]) -or
            $afterIdentity -ne $identity -or
            $after.CreationTimeUtc.Ticks -ne $item.CreationTimeUtc.Ticks -or
            $after.Length -ne $item.Length -or
            $after.LastWriteTimeUtc.Ticks -ne $item.LastWriteTimeUtc.Ticks
        ) {
            throw "Codex hooks file changed while hashing: $Path"
        }
        $state += @(
            [string]$after.Length,
            [string]$after.LastWriteTimeUtc.Ticks,
            $contentHash
        )
    }
    return [string]::Join('|', $state)
}

function Get-HookSnapshot([string]$Path) {
    $snapshot = @{}
    foreach ($component in @(Get-LexicalPathComponents $Path)) {
        $snapshot[$component] = Get-HookPathState $component
    }
    return $snapshot
}

function Assert-HookSnapshot([hashtable]$Expected, [string]$Path) {
    Assert-HookPath -Path (Split-Path -Parent $Path) -ExpectedKind Directory
    Assert-HookPath -Path $Path -ExpectedKind File
    $observed = Get-HookSnapshot $Path
    foreach ($component in $Expected.Keys) {
        if (-not $observed.ContainsKey($component) -or $observed[$component] -ne $Expected[$component]) {
            throw "Codex hooks concurrent modification detected: $component"
        }
    }
}

function Test-JsonObject([object]$Value) {
    return (
        $null -ne $Value -and
        -not ($Value -is [System.Array]) -and
        -not ($Value -is [string]) -and
        -not ($Value -is [ValueType])
    )
}

function Assert-HookSchema([object]$Root, [string]$Path) {
    if (-not (Test-JsonObject $Root)) {
        throw "Codex hooks schema error: root must be an object: $Path"
    }
    if ($Root.PSObject.Properties['hooks']) {
        if (-not (Test-JsonObject $Root.hooks)) {
            throw "Codex hooks schema error: hooks must be an object: $Path"
        }
    } else {
        $Root | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
    }
    if (-not $Root.hooks.PSObject.Properties['Stop']) {
        $Root.hooks | Add-Member -NotePropertyName Stop -NotePropertyValue @()
        return
    }
    if ($Root.hooks.Stop -isnot [System.Array]) {
        throw "Codex hooks schema error: Stop must be an array: $Path"
    }
    foreach ($group in @($Root.hooks.Stop)) {
        if (-not (Test-JsonObject $group) -or -not $group.PSObject.Properties['hooks']) {
            throw "Codex hooks schema error: each Stop group must contain a hooks array: $Path"
        }
        if ($group.hooks -isnot [System.Array]) {
            throw "Codex hooks schema error: group hooks must be an array: $Path"
        }
        foreach ($hook in @($group.hooks)) {
            if (
                -not (Test-JsonObject $hook) -or
                -not $hook.PSObject.Properties['type'] -or
                -not $hook.PSObject.Properties['command'] -or
                $hook.type -isnot [string] -or
                $hook.command -isnot [string] -or
                $hook.type -ne 'command'
            ) {
                throw "Codex hooks schema error: each hook must be a command object: $Path"
            }
            if ($hook.PSObject.Properties['timeout']) {
                try { $timeout = [int]$hook.timeout } catch {
                    throw "Codex hooks schema error: timeout must be a positive integer: $Path"
                }
                if ($timeout -le 0) {
                    throw "Codex hooks schema error: timeout must be a positive integer: $Path"
                }
            }
        }
    }
}

function Get-CommandWrapperPath([string]$CommandText) {
    $match = [System.Text.RegularExpressions.Regex]::Match(
        $CommandText,
        '(?i)(?:^|\s)-File\s+(?:"(?<quoted>[^"]+)"|''(?<single>[^'']+)''|(?<bare>[^\s]+))'
    )
    if (-not $match.Success) { return $null }
    $candidate = $match.Groups['quoted'].Value
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $match.Groups['single'].Value
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $match.Groups['bare'].Value
    }
    try { return [System.IO.Path]::GetFullPath($candidate) } catch { return $null }
}

function Test-ManagedHook([object]$Hook, [string]$WrapperPath) {
    $candidate = Get-CommandWrapperPath ([string]$Hook.command)
    return (
        $null -ne $candidate -and
        $candidate.Equals($WrapperPath, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-ByteArraysEqual([byte[]]$Left, [byte[]]$Right) {
    if ($null -eq $Left -or $null -eq $Right -or $Left.Length -ne $Right.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Invoke-HookTestBarrier(
    [string]$Path,
    [string]$BarrierValue,
    [string]$Phase
) {
    if ([string]::IsNullOrWhiteSpace($BarrierValue)) { return }
    $barrier = [System.IO.Path]::GetFullPath($BarrierValue)
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $hooksFullPath = [System.IO.Path]::GetFullPath($Path)
    $separatorChars = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $tempPrefix = $tempRoot.TrimEnd($separatorChars) + [System.IO.Path]::DirectorySeparatorChar
    $insideTemp = $barrier.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    $hooksInsideTemp = $hooksFullPath.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    $testNamed = (Split-Path -Leaf $barrier) -like 'agent-memory-hook-test-*'
    if (-not $insideTemp -or -not $hooksInsideTemp -or -not $testNamed) {
        throw "Codex hook $Phase test seam rejected: $barrier"
    }
    Assert-HookPath -Path $barrier -ExpectedKind Directory
    [System.IO.File]::WriteAllText((Join-Path $barrier 'ready'), "ready`n", $utf8)
    $continuePath = Join-Path $barrier 'continue'
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not [System.IO.File]::Exists($continuePath)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Codex hook $Phase test seam timed out: $barrier"
        }
        Start-Sleep -Milliseconds 50
    }
}

if ([string]::IsNullOrWhiteSpace($HooksPath)) {
    $profileRoot = [string]$env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($profileRoot)) {
        $profileRoot = [Environment]::GetFolderPath('UserProfile')
    }
    if ([string]::IsNullOrWhiteSpace($profileRoot)) {
        throw 'Codex hooks user profile is unavailable.'
    }
    $HooksPath = Join-Path $profileRoot '.codex\hooks.json'
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$HooksPath = [System.IO.Path]::GetFullPath($HooksPath)
$wrapper = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'scripts\stop-hook.ps1'))
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Stop Hook wrapper was not found: $wrapper"
}
if ($wrapper.IndexOf('"') -ge 0) {
    throw 'Stop Hook wrapper path may not contain a double quote.'
}

$hooksDirectory = Split-Path -Parent $HooksPath
if ([string]::IsNullOrWhiteSpace($hooksDirectory)) {
    throw "Hooks path must have a parent directory: $HooksPath"
}
Assert-HookPath -Path $hooksDirectory -ExpectedKind Directory
Assert-HookPath -Path $HooksPath -ExpectedKind File
Ensure-HookDirectory $hooksDirectory
Assert-HookPath -Path $hooksDirectory -ExpectedKind Directory
Assert-HookPath -Path $HooksPath -ExpectedKind File
$lockPath = Join-Path $hooksDirectory '.agent-memory-hooks.lock'
Assert-HookPath -Path $lockPath -ExpectedKind File
$lockCreated = -not [System.IO.File]::Exists($lockPath)
$lockStream = $null
try {
$lockStream = [System.IO.File]::Open(
    $lockPath,
    [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
)
Assert-HookPath -Path $lockPath -ExpectedKind File
$initialSnapshot = Get-HookSnapshot $HooksPath

$initialBytes = $null
if ([System.IO.File]::Exists($HooksPath)) {
    $initialBytes = [System.IO.File]::ReadAllBytes($HooksPath)
    Assert-HookSnapshot -Expected $initialSnapshot -Path $HooksPath
    try {
        $jsonText = $utf8.GetString($initialBytes).TrimStart([char]0xFEFF)
        $root = $jsonText | ConvertFrom-Json
    } catch {
        throw "Invalid Codex hooks JSON: $HooksPath"
    }
} else {
    $root = [pscustomobject]@{}
}
Assert-HookSchema -Root $root -Path $HooksPath

$mode = if ($AutoCloseout) { ' -AutoCloseout' } else { '' }
$timeout = if ($AutoCloseout) { 320 } else { 20 }
$command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Actor codex -Protocol codex -Event stop-hook{1}' -f $wrapper, $mode
$managedSeen = $false
$changed = $false
$stopGroups = New-Object System.Collections.Generic.List[object]
foreach ($group in @($root.hooks.Stop)) {
    $newHooks = New-Object System.Collections.Generic.List[object]
    $groupHadManagedHook = $false
    foreach ($hook in @($group.hooks)) {
        if (Test-ManagedHook -Hook $hook -WrapperPath $wrapper) {
            $groupHadManagedHook = $true
            if (-not $managedSeen) {
                if (
                    [string]$hook.command -ne $command -or
                    -not $hook.PSObject.Properties['timeout'] -or
                    [int]$hook.timeout -ne $timeout
                ) {
                    $changed = $true
                }
                $hook.type = 'command'
                $hook.command = $command
                if ($hook.PSObject.Properties['timeout']) {
                    $hook.timeout = $timeout
                } else {
                    $hook | Add-Member -NotePropertyName timeout -NotePropertyValue $timeout
                }
                [void]$newHooks.Add($hook)
                $managedSeen = $true
            } else {
                $changed = $true
            }
        } else {
            [void]$newHooks.Add($hook)
        }
    }
    if ($newHooks.Count -gt 0 -or -not $groupHadManagedHook) {
        $group.hooks = @($newHooks.ToArray())
        [void]$stopGroups.Add($group)
    }
}

if (-not $managedSeen) {
    $entry = [pscustomobject]@{
        hooks = @(
            [pscustomobject]@{
                type = 'command'
                command = $command
                timeout = $timeout
            }
        )
    }
    [void]$stopGroups.Add($entry)
    $changed = $true
}
$root.hooks.Stop = @($stopGroups.ToArray())

$json = $root | ConvertTo-Json -Depth 32
$desiredBytes = $utf8.GetBytes($json + [Environment]::NewLine)
$writeRequired = -not (Test-ByteArraysEqual -Left $initialBytes -Right $desiredBytes)
if ($writeRequired) {
    $temporaryPath = Join-Path $hooksDirectory ('.agent-memory-hooks-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = Join-Path $hooksDirectory ('.agent-memory-hooks-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $discardPath = Join-Path $hooksDirectory ('.agent-memory-hooks-discard-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupCaptured = $false
    $preserveBackup = $false
    try {
        [System.IO.File]::WriteAllBytes($temporaryPath, $desiredBytes)
        Invoke-HookTestBarrier -Path $HooksPath -BarrierValue $testBarrier -Phase 'pre-CAS'
        Assert-HookSnapshot -Expected $initialSnapshot -Path $HooksPath
        Invoke-HookTestBarrier -Path $HooksPath -BarrierValue $afterCasTestBarrier -Phase 'after-CAS'
        $parentState = Get-HookPathState $hooksDirectory
        if ($parentState -ne $initialSnapshot[$hooksDirectory]) {
            throw "Codex hooks parent changed after CAS: $hooksDirectory"
        }
        $currentFileState = Get-HookPathState $HooksPath
        $expectedFileState = [string]$initialSnapshot[$HooksPath]
        if ($currentFileState -ne 'missing') {
            [System.IO.File]::Replace($temporaryPath, $HooksPath, $backupPath)
            $backupCaptured = $true
            $actualFileState = Get-HookPathState $backupPath
            if ($actualFileState -ne $expectedFileState) {
                try {
                    [System.IO.File]::Replace($backupPath, $HooksPath, $discardPath)
                    $backupCaptured = $false
                    Remove-Item -LiteralPath $discardPath -Force
                } catch {
                    $preserveBackup = $true
                    throw "Codex hooks restore failed; recovery_path=$backupPath"
                }
                throw "Codex hooks actual hooks object changed after CAS: $HooksPath"
            }
        } else {
            if ($expectedFileState -ne 'missing') {
                throw "Codex hooks file was deleted after CAS: $HooksPath"
            }
            [System.IO.File]::Move($temporaryPath, $HooksPath)
        }
        Assert-HookPath -Path $HooksPath -ExpectedKind File
        if ((Get-HookPathState $hooksDirectory) -ne $initialSnapshot[$hooksDirectory]) {
            if ($backupCaptured) {
                [System.IO.File]::Replace($backupPath, $HooksPath, $discardPath)
                $backupCaptured = $false
                Remove-Item -LiteralPath $discardPath -Force
            }
            throw "Codex hooks parent changed during replace: $hooksDirectory"
        }
        $installedBytes = [System.IO.File]::ReadAllBytes($HooksPath)
        if (-not (Test-ByteArraysEqual -Left $installedBytes -Right $desiredBytes)) {
            if ($backupCaptured) {
                [System.IO.File]::Replace($backupPath, $HooksPath, $discardPath)
                $backupCaptured = $false
                Remove-Item -LiteralPath $discardPath -Force
            }
            throw "Codex hooks file verification failed after replace: $HooksPath"
        }
        if ($backupCaptured) {
            Remove-Item -LiteralPath $backupPath -Force
            $backupCaptured = $false
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
        if ((Test-Path -LiteralPath $backupPath) -and -not $preserveBackup) {
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $discardPath) {
            Remove-Item -LiteralPath $discardPath -Force -ErrorAction SilentlyContinue
        }
    }
}

} finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    if ($lockCreated -and (Test-Path -LiteralPath $lockPath)) {
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not $managedSeen) {
    Write-Output "[OK] Codex Stop Hook installed: $HooksPath"
} elseif ($changed) {
    Write-Output "[OK] Codex Stop Hook updated: $HooksPath"
} else {
    Write-Output "[OK] Codex Stop Hook already installed: $HooksPath"
}
Write-Output '[WARN] Confirm that [features] hooks = true is enabled in ~/.codex/config.toml.'
