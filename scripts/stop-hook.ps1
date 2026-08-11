[CmdletBinding()]
param(
    [ValidateSet('codex', 'claude', 'codebuddy')]
    [string]$Actor = 'codex',
    [ValidateSet('', 'codex', 'claude')]
    [string]$Protocol = '',
    [ValidateSet('stop-hook', 'session-end')]
    [string]$Event = 'stop-hook',
    [switch]$NonBlocking,
    [switch]$AutoCloseout,
    [ValidateRange(1, 86400)]
    [int]$Timeout = 300,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$hookScript = Join-Path $scriptRoot 'agent_memory_stop_hook.py'

if (-not (Test-Path -LiteralPath $hookScript -PathType Leaf)) {
    Write-Error "Stop Hook implementation was not found: $hookScript"
    exit 2
}

$pythonPrefix = @()
if (-not $Python) {
    $venvPython = Join-Path (Split-Path -Parent $scriptRoot) '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $Python = $venvPython
    } else {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $command) {
            $command = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($command) { $pythonPrefix = @('-3') }
        }
        if (-not $command) {
            Write-Error 'Python 3 was not found. Run scripts\install-windows.ps1 first.'
            exit 2
        }
        $Python = $command.Source
    }
}

$arguments = @($hookScript, '--actor', $Actor)
if ($Protocol) { $arguments += @('--protocol', $Protocol) }
$arguments += @('--event', $Event)
if ($NonBlocking) { $arguments += '--non-blocking' }
$arguments += @('--timeout', [string]$Timeout)
if ($AutoCloseout) { $arguments += '--auto-closeout' }
$nativeArguments = @($pythonPrefix) + $arguments

function ConvertTo-NativeArgument([string]$Value) {
    if ($Value.IndexOf('"') -ge 0) {
        throw 'Native adapter arguments may not contain a double quote.'
    }
    if ($Value.Length -gt 0 -and $Value -notmatch '\s') { return $Value }
    return '"' + $Value + '"'
}

try {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Python
    $startInfo.Arguments = (($nativeArguments | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw 'Python process did not start.' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $payload = [Console]::In.ReadToEnd()
    if ($payload.Length -gt 0) { $process.StandardInput.Write($payload) }
    $process.StandardInput.Close()
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    if ($stdout) { [Console]::Out.Write($stdout) }
    if ($stderr) { [Console]::Error.Write($stderr) }
    exit $process.ExitCode
} catch {
    [Console]::Error.WriteLine("Agent Memory Stop Hook failed: $($_.Exception.Message)")
    exit 2
}
