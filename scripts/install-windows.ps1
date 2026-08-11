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
$configExisted = Test-Path -LiteralPath $configPath -PathType Leaf

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
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $temporaryPath = Join-Path (Split-Path -Parent $Path) ('.agent-memory-config-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($temporaryPath, ([string]::Join("`n", $lines)), $utf8)
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

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

New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-Checked $python.Source ($prefix + @('-m', 'venv', $venvRoot)) 'virtual environment creation'
}

$sourceInstaller = Join-Path $repoRoot 'scripts\install_runtime.py'
if (-not (Test-Path -LiteralPath $sourceInstaller -PathType Leaf)) {
    throw "Runtime installer was not found: $sourceInstaller"
}
Invoke-Checked $venvPython @($sourceInstaller, '--config-root', $ConfigRoot) 'runtime installation'
$runtimeScripts = Join-Path $ConfigRoot 'scripts'
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
    Invoke-Checked $venvPython $bootstrapArguments 'vault bootstrap'
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

config = agent_memory_env.load_config()
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
Invoke-Checked $venvPython @(
    '-c', $configValidation, $runtimeScripts, $expectedMemoryRoot
) 'runtime config validation'
if ($configureFreshVault) {
    Invoke-Checked $venvPython @(
        (Join-Path $runtimeScripts 'agent_memory_evolution.py'), '--init', '--scan', '--report'
    ) 'evolution initialization'
    Invoke-Checked $venvPython @(
        (Join-Path $runtimeScripts 'agent_memory_index.py'), '--init', '--scan', '--report'
    ) 'SQLite index initialization'
}
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_check.py')) 'structure check'

if ($InstallCodexHook) {
    $hookArgs = @('-RuntimeRoot', $ConfigRoot)
    if ($CodexHooksPath) { $hookArgs += @('-HooksPath', $CodexHooksPath) }
    if ($AutoCloseout) { $hookArgs += '-AutoCloseout' }
    & (Join-Path $runtimeScripts 'install-codex-hook.ps1') @hookArgs
    if ($LASTEXITCODE -ne 0) { throw "Codex hook installation failed with exit code $LASTEXITCODE" }
}
if ($InstallAuditTask) {
    & (Join-Path $runtimeScripts 'audit-task.ps1') install -RuntimeRoot $ConfigRoot -Python $venvPython
    if ($LASTEXITCODE -ne 0) { throw "audit task installation failed with exit code $LASTEXITCODE" }
}

Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_doctor.py')) 'doctor'
Write-Warning 'windows_acl_unverified: POSIX modes are not a Windows access boundary and this installer has not verified or hardened NTFS ACLs.'
Write-Output '[OK] Windows installation complete'
Write-Output "Vault: $MemoryRoot"
Write-Output "Runtime: $ConfigRoot"
