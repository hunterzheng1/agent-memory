[CmdletBinding()]
param(
    [string]$MemoryRoot = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Agent Memory Vault'),
    [string]$ConfigRoot = (Join-Path $env:LOCALAPPDATA 'AgentMemoryVault'),
    [string]$UserId = 'demo-user',
    [ValidateSet('shared', 'codex', 'claude', 'codebuddy', 'cursor')]
    [string]$AgentId = 'shared',
    [string]$AppId = 'agent-memory',
    [switch]$OverwriteConfig,
    [switch]$NoInitGit,
    [switch]$InstallCodexHook,
    [string]$CodexHooksPath = '',
    [switch]$AutoCloseout,
    [switch]$InstallAuditTask
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$installerTestBarrier = [string]$env:AGENT_MEMORY_INSTALLER_TEST_BARRIER

# Installed settings in the caller's environment must not redirect bootstrap or
# health checks away from the paths selected for this invocation.
Get-ChildItem Env: | Where-Object {
    $_.Name -like 'AGENT_MEMORY_*' -or
    $_.Name -like 'CODEX_MEMORY_*' -or
    $_.Name -eq 'MEMORY_ACTOR'
} | ForEach-Object {
    Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$MemoryRoot = [System.IO.Path]::GetFullPath($MemoryRoot)
$ConfigRoot = [System.IO.Path]::GetFullPath($ConfigRoot)
$venvRoot = Join-Path $ConfigRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$configDir = Join-Path $ConfigRoot 'config'
$configPath = Join-Path $configDir 'agent-memory.toml'
$runtimeScripts = Join-Path $ConfigRoot 'scripts'
$runtimeManifest = Join-Path $configDir 'runtime-manifest.json'
if ($InstallCodexHook) {
    if ([string]::IsNullOrWhiteSpace($CodexHooksPath)) {
        $codexUserProfile = [string]$env:USERPROFILE
        if ([string]::IsNullOrWhiteSpace($codexUserProfile)) {
            $codexUserProfile = [Environment]::GetFolderPath('UserProfile')
        }
        if ([string]::IsNullOrWhiteSpace($codexUserProfile)) {
            throw 'installer_preflight=error reason=missing_user_profile'
        }
        $CodexHooksPath = Join-Path $codexUserProfile '.codex\hooks.json'
    }
    $CodexHooksPath = [System.IO.Path]::GetFullPath($CodexHooksPath)
}

function Get-LexicalPathComponents([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        throw "installer_preflight=error reason=path_not_absolute path=$fullPath"
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

function Assert-InstallerPath(
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
            throw "installer_preflight=error reason=metadata_error path=$component"
        }
        $attributes = [System.IO.FileAttributes]$item.Attributes
        if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "installer_preflight=error reason=reparse_point path=$component"
        }
        if (-not $final -and -not $item.PSIsContainer) {
            throw "installer_preflight=error reason=parent_not_directory path=$component"
        }
        if ($final -and $ExpectedKind -eq 'Directory' -and -not $item.PSIsContainer) {
            throw "installer_preflight=error reason=not_directory path=$component"
        }
        if ($final -and $ExpectedKind -eq 'File') {
            $isDevice = ($attributes -band [System.IO.FileAttributes]::Device) -ne 0
            if ($item.PSIsContainer -or $isDevice -or -not ($item -is [System.IO.FileInfo])) {
                throw "installer_preflight=error reason=not_regular_file path=$component"
            }
        }
    }
}

function Get-InstallerPathState([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return 'missing'
    } catch {
        throw "installer_preflight=error reason=metadata_error path=$Path"
    }
    $attributes = [System.IO.FileAttributes]$item.Attributes
    if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "installer_preflight=error reason=reparse_point path=$Path"
    }
    $state = @(
        'present',
        $item.GetType().FullName,
        [string]$item.PSIsContainer,
        [string][int]$attributes,
        [string]$item.CreationTimeUtc.Ticks
    )
    if ($item -is [System.IO.FileInfo]) {
        $hashStream = $null
        $hasher = $null
        try {
            $hashStream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
            $hasher = [System.Security.Cryptography.SHA256]::Create()
            $hashBytes = $hasher.ComputeHash($hashStream)
            $contentHash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
            $afterHash = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        } catch {
            $errorKind = $_.Exception.GetType().Name
            throw "installer_preflight=error reason=metadata_error detail=$errorKind path=$Path"
        } finally {
            if ($null -ne $hasher) { $hasher.Dispose() }
            if ($null -ne $hashStream) { $hashStream.Dispose() }
        }
        $afterAttributes = [System.IO.FileAttributes]$afterHash.Attributes
        if (
            ($afterAttributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not ($afterHash -is [System.IO.FileInfo]) -or
            $afterHash.CreationTimeUtc.Ticks -ne $item.CreationTimeUtc.Ticks
        ) {
            throw "installer_preflight=error reason=path_changed path=$Path"
        }
        $state += @(
            [string]$afterHash.Length,
            [string]$afterHash.LastWriteTimeUtc.Ticks,
            $contentHash
        )
    }
    return [string]::Join('|', $state)
}

function Get-InstallerPathSnapshot([string[]]$Paths) {
    $snapshot = @{}
    foreach ($path in $Paths) {
        foreach ($component in @(Get-LexicalPathComponents $path)) {
            if (-not $snapshot.ContainsKey($component)) {
                $snapshot[$component] = Get-InstallerPathState $component
            }
        }
    }
    return $snapshot
}

$script:installerSnapshotPaths = @(
    $ConfigRoot,
    $configDir,
    $configPath,
    $venvRoot,
    $venvPython,
    $MemoryRoot,
    $runtimeScripts,
    $runtimeManifest
)
if ($InstallCodexHook -and $CodexHooksPath) {
    $script:installerSnapshotPaths += [System.IO.Path]::GetFullPath($CodexHooksPath)
}
$script:installerPathSnapshot = $null

function Assert-InstallerPaths {
    Assert-InstallerPath -Path $ConfigRoot -ExpectedKind Directory
    Assert-InstallerPath -Path $configDir -ExpectedKind Directory
    Assert-InstallerPath -Path $configPath -ExpectedKind File
    Assert-InstallerPath -Path $venvRoot -ExpectedKind Directory
    Assert-InstallerPath -Path $venvPython -ExpectedKind File
    Assert-InstallerPath -Path $MemoryRoot -ExpectedKind Directory
    Assert-InstallerPath -Path $runtimeScripts -ExpectedKind Directory
    Assert-InstallerPath -Path $runtimeManifest -ExpectedKind File
    if ($InstallCodexHook -and $CodexHooksPath) {
        $hooksFullPath = [System.IO.Path]::GetFullPath($CodexHooksPath)
        Assert-InstallerPath -Path (Split-Path -Parent $hooksFullPath) -ExpectedKind Directory
        Assert-InstallerPath -Path $hooksFullPath -ExpectedKind File
    }
}

function Set-InstallerPathSnapshot {
    Assert-InstallerPaths
    $script:installerPathSnapshot = Get-InstallerPathSnapshot $script:installerSnapshotPaths
}

function Test-InstallerAllowedSnapshotChange([string]$Path, [string[]]$AllowedRoots) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    foreach ($root in $AllowedRoots) {
        $fullRoot = [System.IO.Path]::GetFullPath($root)
        $separatorChars = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $prefix = $fullRoot.TrimEnd($separatorChars) + [System.IO.Path]::DirectorySeparatorChar
        if ($fullPath.Equals($fullRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Assert-InstallerSnapshot([string[]]$AllowedChanges = @()) {
    Assert-InstallerPaths
    if ($null -eq $script:installerPathSnapshot) { return }
    $observed = Get-InstallerPathSnapshot $script:installerSnapshotPaths
    foreach ($path in $script:installerPathSnapshot.Keys) {
        if (-not $observed.ContainsKey($path) -or $observed[$path] -ne $script:installerPathSnapshot[$path]) {
            if (Test-InstallerAllowedSnapshotChange -Path $path -AllowedRoots $AllowedChanges) {
                continue
            }
            throw "installer_preflight=error reason=path_changed path=$path"
        }
    }
}

function Assert-InstallerBoundary {
    Assert-InstallerSnapshot
}

function Invoke-InstallerTestBarrier {
    if ([string]::IsNullOrWhiteSpace($installerTestBarrier)) { return }
    $barrier = [System.IO.Path]::GetFullPath($installerTestBarrier)
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $separatorChars = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $tempPrefix = $tempRoot.TrimEnd($separatorChars) + [System.IO.Path]::DirectorySeparatorChar
    $insideTemp = $barrier.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    $configInsideTemp = $ConfigRoot.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    $testNamed = (Split-Path -Leaf $barrier) -like 'agent-memory-installer-test-*'
    if (-not $insideTemp -or -not $configInsideTemp -or -not $testNamed) {
        throw "installer_preflight=error reason=test_seam_rejected path=$barrier"
    }
    Assert-InstallerPath -Path $barrier -ExpectedKind Directory
    $readyPath = Join-Path $barrier 'ready'
    $continuePath = Join-Path $barrier 'continue'
    [System.IO.File]::WriteAllText($readyPath, "ready`n", $utf8)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not [System.IO.File]::Exists($continuePath)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "installer_preflight=error reason=test_seam_timeout path=$barrier"
        }
        Start-Sleep -Milliseconds 50
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function ConvertTo-TomlString([string]$Value) {
    $builder = New-Object System.Text.StringBuilder
    foreach ($character in $Value.ToCharArray()) {
        $code = [int][char]$character
        $escape = $null
        switch ($code) {
            8 { $escape = '\b' }
            9 { $escape = '\t' }
            10 { $escape = '\n' }
            12 { $escape = '\f' }
            13 { $escape = '\r' }
            34 { $escape = '\"' }
            92 { $escape = '\\' }
        }
        if ($null -ne $escape) {
            [void]$builder.Append($escape)
        } elseif ($code -lt 32 -or $code -eq 127) {
            [void]$builder.Append(('\u{0:X4}' -f $code))
        } else {
            [void]$builder.Append($character)
        }
    }
    return '"' + $builder.ToString() + '"'
}

function ConvertTo-TomlPath([string]$Value) {
    return ConvertTo-TomlString ([System.IO.Path]::GetFullPath($Value).Replace('\', '/'))
}

function Write-AgentMemoryConfig([string]$Path) {
    Assert-InstallerBoundary
    $stateDb = Join-Path $ConfigRoot 'state.sqlite'
    $lines = @(
        ('memory_root = ' + (ConvertTo-TomlPath $MemoryRoot))
        ('git_root = ' + (ConvertTo-TomlPath $MemoryRoot))
        ('config_root = ' + (ConvertTo-TomlPath $ConfigRoot))
        ('state_db = ' + (ConvertTo-TomlPath $stateDb))
        ('audit_db = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'audit_decisions.sqlite')))
        ('closeout_log = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'logs\closeout.jsonl')))
        ('audit_run_log = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'logs\audit_runs.jsonl')))
        ('audit_report = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'reports\latest-audit.json')))
        ('invariants_file = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'config\system-invariants.json')))
        ('python = ' + (ConvertTo-TomlPath $venvPython))
        ''
        ('user_id = ' + (ConvertTo-TomlString $UserId))
        ('agent_id = ' + (ConvertTo-TomlString $AgentId))
        ('app_id = ' + (ConvertTo-TomlString $AppId))
        ''
        '[closeout]'
        'ordinary_memory_candidate_pool = false'
        'ask_before_skill_promotion = true'
        'run_sqlite_index_after_closeout = true'
        'run_audit_after_interval_days = 7'
        'commit_scoped_memory_files = true'
        ''
        '[write_intents]'
        'enabled = false'
        'enforcement = "off"'
        'ttl_hours = 24'
        'max_proposal_bytes = 2097152'
        'max_target_bytes = 8388608'
        'max_snapshot_bytes = 262144'
        'protected_paths = ['
        '  "AGENTS.md",'
        '  "用户记忆/偏好与边界.md",'
        '  "工作流/Agent记忆收尾决策规则.md",'
        '  "工作流/Agent记忆字段规范.md",'
        ']'
        ''
        '[semantic_retrieval]'
        'enabled = false'
        ('vector_dir = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'zvec\memory_chunks_embeddinggemma_768')))
        'embedding_model = "google/embeddinggemma-300m"'
        'embedding_dim = 768'
        'embedding_device = "cpu"'
        ('python = ' + (ConvertTo-TomlPath $venvPython))
        ('lock_path = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'locks\zvec.lock')))
        'require_local_model = false'
        'model_revision = ""'
        ('model_manifest = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'models\embeddinggemma-300m\model-manifest.json')))
        ('dependency_lock = ' + (ConvertTo-TomlPath (Join-Path $ConfigRoot 'requirements-vector.lock')))
        'run_vector_index_after_closeout = false'
        ''
        '[privacy]'
        'include_real_vault = false'
        'include_state_db = false'
        'include_env_file = false'
        'include_vector_db = false'
        'include_model_cache = false'
        ''
    )
    $parentPath = Split-Path -Parent $Path
    Assert-InstallerPath -Path $parentPath -ExpectedKind Directory
    Assert-InstallerPath -Path $Path -ExpectedKind File
    New-Item -ItemType Directory -Force -Path $parentPath | Out-Null
    Assert-InstallerPath -Path $parentPath -ExpectedKind Directory
    Assert-InstallerPath -Path $Path -ExpectedKind File
    $temporaryPath = Join-Path $parentPath ('.agent-memory-config-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    Assert-InstallerPath -Path $temporaryPath -ExpectedKind File
    try {
        [System.IO.File]::WriteAllText($temporaryPath, ([string]::Join("`n", $lines)), $utf8)
        Assert-InstallerBoundary
        Assert-InstallerPath -Path $parentPath -ExpectedKind Directory
        Assert-InstallerPath -Path $Path -ExpectedKind File
        Assert-InstallerPath -Path $temporaryPath -ExpectedKind File
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
        Assert-InstallerSnapshot -AllowedChanges @($configPath)
        Set-InstallerPathSnapshot
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

# Fail closed before creating a venv, installing runtime files, bootstrapping a
# vault, or initializing Git. Existing config is valid only as a regular,
# non-reparse file; missing paths remain eligible for a fresh install.
Assert-InstallerPaths
New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
Assert-InstallerPaths
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
Set-InstallerPathSnapshot
Invoke-InstallerTestBarrier
Assert-InstallerBoundary
$configExisted = Test-Path -LiteralPath $configPath -PathType Leaf

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $git) { throw 'Git was not found in PATH.' }
$python = Get-Command py.exe -ErrorAction SilentlyContinue
$prefix = @('-3')
if (-not $python) {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    $prefix = @()
}
if (-not $python) { throw 'Python 3 was not found in PATH.' }
$version = & $python.Source @prefix -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor); raise SystemExit(sys.version_info < (3, 10))'
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or newer is required; detected version code $version"
}

Assert-InstallerBoundary
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-Checked $python.Source ($prefix + @('-m', 'venv', $venvRoot)) 'virtual environment creation'
    Assert-InstallerSnapshot -AllowedChanges @($venvRoot)
    Set-InstallerPathSnapshot
}

$sourceInstaller = Join-Path $repoRoot 'scripts\install_runtime.py'
if (-not (Test-Path -LiteralPath $sourceInstaller -PathType Leaf)) {
    throw "Runtime installer was not found: $sourceInstaller"
}
Assert-InstallerBoundary
Invoke-Checked $venvPython @($sourceInstaller, '--config-root', $ConfigRoot) 'runtime installation'
Assert-InstallerSnapshot -AllowedChanges @($runtimeScripts, $runtimeManifest)
Set-InstallerPathSnapshot
Assert-InstallerBoundary
Invoke-Checked $venvPython @(
    (Join-Path $runtimeScripts 'install_runtime.py'),
    '--config-root', $ConfigRoot,
    '--verify'
) 'runtime verification'

$configureFreshVault = (-not $configExisted) -or $OverwriteConfig
if ($configureFreshVault) {
    $bootstrapArguments = @(
        (Join-Path $runtimeScripts 'bootstrap.py'),
        '--memory-root', $MemoryRoot,
        '--config-root', $ConfigRoot,
        '--state-db', (Join-Path $ConfigRoot 'state.sqlite'),
        '--git-root', $MemoryRoot,
        '--user-id', $UserId,
        '--agent-id', $AgentId,
        '--app-id', $AppId
    )
    if ($NoInitGit) { $bootstrapArguments += '--no-init-git' }
    else { $bootstrapArguments += '--init-git' }
    Assert-InstallerBoundary
    Invoke-Checked $venvPython $bootstrapArguments 'vault bootstrap'
    Assert-InstallerSnapshot -AllowedChanges @($MemoryRoot)
    Set-InstallerPathSnapshot
    Write-AgentMemoryConfig $configPath
    if ($configExisted) { Write-Output "[OK] config_overwritten explicit=$configPath" }
    else { Write-Output "[OK] config_created path=$configPath" }
} else {
    Write-Output "[OK] existing_config_preserved path=$configPath"
}

$env:AGENT_MEMORY_CONFIG_FILE = $configPath
$configValidation = @'
import sys
sys.path.insert(0, sys.argv[1])
import agent_memory_env

try:
    config = agent_memory_env.load_config()
except agent_memory_env.ConfigPathSecurityError as exc:
    print('config_validation=error reason=' + exc.reason, file=sys.stderr)
    raise SystemExit(2)
required = ('memory_root', 'git_root', 'state_db')
missing = [key for key in required if not str(config.get(key, '')).strip()]
if missing:
    print('config_validation=error missing=' + ','.join(missing), file=sys.stderr)
    raise SystemExit(2)
for key in required:
    agent_memory_env.resolve_config_path(str(config[key]))
expected_memory_root = sys.argv[2]
if expected_memory_root != '-':
    configured = agent_memory_env.resolve_config_path(str(config['memory_root']))
    expected = agent_memory_env.resolve_config_path(expected_memory_root)
    if configured != expected:
        print('config_validation=error memory_root_mismatch', file=sys.stderr)
        raise SystemExit(2)
print('config_validation=ok')
'@
$expectedMemoryRoot = if ($configureFreshVault) { $MemoryRoot } else { '-' }
Assert-InstallerBoundary
Invoke-Checked $venvPython @(
    '-c', $configValidation, $runtimeScripts, $expectedMemoryRoot
) 'runtime config validation'
if ($configureFreshVault) {
    Assert-InstallerBoundary
    Invoke-Checked $venvPython @(
        (Join-Path $runtimeScripts 'agent_memory_evolution.py'), '--init', '--scan', '--report'
    ) 'evolution initialization'
    Assert-InstallerBoundary
    Invoke-Checked $venvPython @(
        (Join-Path $runtimeScripts 'agent_memory_index.py'), '--init', '--scan', '--report'
    ) 'SQLite index initialization'
}
Assert-InstallerBoundary
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_check.py')) 'structure check'

if ($InstallCodexHook) {
    Assert-InstallerBoundary
    $hookAllowedChanges = @($CodexHooksPath)
    $hooksParent = Split-Path -Parent $CodexHooksPath
    foreach ($component in @(Get-LexicalPathComponents $hooksParent)) {
        if (
            $script:installerPathSnapshot.ContainsKey($component) -and
            $script:installerPathSnapshot[$component] -eq 'missing'
        ) {
            $hookAllowedChanges += $component
        }
    }
    $hookParameters = @{
        RuntimeRoot = $ConfigRoot
        HooksPath = $CodexHooksPath
    }
    if ($AutoCloseout) { $hookParameters['AutoCloseout'] = $true }
    & (Join-Path $runtimeScripts 'install-codex-hook.ps1') @hookParameters
    if ($LASTEXITCODE -ne 0) { throw "Codex hook installation failed with exit code $LASTEXITCODE" }
    Assert-InstallerSnapshot -AllowedChanges $hookAllowedChanges
    Set-InstallerPathSnapshot
}
if ($InstallAuditTask) {
    Assert-InstallerBoundary
    & (Join-Path $runtimeScripts 'audit-task.ps1') install -RuntimeRoot $ConfigRoot -Python $venvPython
    if ($LASTEXITCODE -ne 0) { throw "audit task installation failed with exit code $LASTEXITCODE" }
}

Assert-InstallerBoundary
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_doctor.py')) 'doctor'
Write-Warning 'windows_acl_unverified: POSIX modes are not a Windows access boundary and this installer has not verified or hardened NTFS ACLs.'
Write-Output '[OK] Windows installation complete'
Write-Output "Vault: $MemoryRoot"
Write-Output "Runtime: $ConfigRoot"
