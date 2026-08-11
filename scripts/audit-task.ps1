[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'status', 'run', 'uninstall')]
    [string]$Action = 'status',
    [string]$TaskName = 'AgentMemoryVaultAudit',
    [string]$Python = '',
    [string]$RuntimeRoot = '',
    [ValidateRange(0, 6)]
    [int]$DayOfWeek = 1,
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '10:30',
    [switch]$PlanOnly
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

if (-not $RuntimeRoot) {
    $RuntimeRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$scriptRoot = Join-Path $RuntimeRoot 'scripts'
$auditScript = Join-Path $scriptRoot 'agent_memory_audit_autorun.py'
$pythonPrefix = ''
if (-not $Python) {
    $candidate = Join-Path $RuntimeRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $Python = $candidate
    } else {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $command) {
            $command = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($command) { $pythonPrefix = '-3 ' }
        }
        if (-not $command) { throw 'Python 3 was not found.' }
        $Python = $command.Source
    }
}
$Python = [System.IO.Path]::GetFullPath($Python)
if ($Python.IndexOf('"') -ge 0 -or $auditScript.IndexOf('"') -ge 0) {
    throw 'Scheduled task executable paths may not contain a double quote.'
}
$taskArguments = $pythonPrefix + ('-X utf8 "{0}" --reason task-scheduler --json' -f $auditScript)

if ($PlanOnly) {
    [pscustomobject]@{
        action = $Action
        task_name = $TaskName
        python = $Python
        arguments = $taskArguments
        working_directory = $RuntimeRoot
        day_of_week = $DayOfWeek
        at = $At
        side_effects = $false
    } | ConvertTo-Json -Compress
    exit 0
}

switch ($Action) {
    'install' {
        if (-not (Test-Path -LiteralPath $auditScript -PathType Leaf)) {
            throw "Audit script was not found: $auditScript"
        }
        $days = @('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
        $taskAction = New-ScheduledTaskAction -Execute $Python -Argument $taskArguments -WorkingDirectory $RuntimeRoot
        $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days[$DayOfWeek] -At $At
        $principal = New-ScheduledTaskPrincipal `
            -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
            -LogonType Interactive `
            -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $taskAction `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Agent Memory Vault weekly audit' `
            -Force | Out-Null
        Write-Output "[OK] installed task=$TaskName"
    }
    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Output "[WARN] task_missing name=$TaskName"
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Output "[OK] task=$TaskName state=$($task.State) last_result=$($info.LastTaskResult) next_run=$($info.NextRunTime)"
    }
    'run' {
        if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
            throw "Scheduled task not found: $TaskName"
        }
        Start-ScheduledTask -TaskName $TaskName
        Write-Output "[OK] started task=$TaskName"
    }
    'uninstall' {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "[OK] uninstalled task=$TaskName"
        } else {
            Write-Output "[OK] task_already_absent name=$TaskName"
        }
    }
}
