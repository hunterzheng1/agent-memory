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
$taskPath = '\AgentMemory\'

function Assert-SafeTaskName([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw 'TaskName must not be empty.'
    }
    if ($Name.Length -gt 128) {
        throw 'TaskName must not exceed 128 characters.'
    }
    if ($Name.IndexOfAny([char[]]@('*', '?', '[', ']', '/', '\')) -ge 0) {
        throw 'TaskName must not contain wildcards or path separators.'
    }
    foreach ($character in $Name.ToCharArray()) {
        $code = [int][char]$character
        if ($code -lt 32 -or $code -eq 127) {
            throw 'TaskName must not contain control characters.'
        }
    }
}

Assert-SafeTaskName $TaskName

if (-not $RuntimeRoot) {
    $RuntimeRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$scriptRoot = Join-Path $RuntimeRoot 'scripts'
$auditScript = Join-Path $scriptRoot 'agent_memory_audit_autorun.py'
$pythonPrefix = ''
$taskArguments = $null

function Resolve-InstallPython {
    if (-not $script:Python) {
        $candidate = Join-Path $RuntimeRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $script:Python = $candidate
        } else {
            $command = Get-Command python.exe -ErrorAction SilentlyContinue
            if (-not $command) {
                $command = Get-Command py.exe -ErrorAction SilentlyContinue
                if ($command) { $script:pythonPrefix = '-3 ' }
            }
            if (-not $command) { throw 'Python 3 was not found.' }
            $script:Python = $command.Source
        }
    }
    $script:Python = [System.IO.Path]::GetFullPath($script:Python)
    if ($script:Python.IndexOf('"') -ge 0 -or $auditScript.IndexOf('"') -ge 0) {
        throw 'Scheduled task executable paths may not contain a double quote.'
    }
    $script:taskArguments = $script:pythonPrefix + ('-X utf8 "{0}" --reason task-scheduler --json' -f $auditScript)
}

function Get-ExactAuditTask {
    $candidates = @(
        Get-ScheduledTask `
            -TaskPath $taskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
    )
    $exact = @(
        $candidates | Where-Object {
            $null -ne $_ -and
            ([string]$_.TaskName).Equals($TaskName, [System.StringComparison]::OrdinalIgnoreCase) -and
            ([string]$_.TaskPath).Equals($taskPath, [System.StringComparison]::OrdinalIgnoreCase)
        }
    )
    if ($exact.Count -gt 1) {
        throw "Multiple exact scheduled tasks were returned for path=$taskPath name=$TaskName"
    }
    if ($exact.Count -eq 1) { return $exact[0] }
    return $null
}

if ($Action -eq 'install') {
    Resolve-InstallPython
}

if ($PlanOnly) {
    [pscustomobject]@{
        action = $Action
        task_path = $taskPath
        task_name = $TaskName
        python = $(if ($Action -eq 'install') { $Python } else { $null })
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
            -TaskPath $taskPath `
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
        $task = Get-ExactAuditTask
        if (-not $task) {
            Write-Output "[WARN] task_missing path=$taskPath name=$TaskName"
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $TaskName
        Write-Output "[OK] task_path=$taskPath task=$TaskName state=$($task.State) last_result=$($info.LastTaskResult) next_run=$($info.NextRunTime)"
    }
    'run' {
        if (-not (Get-ExactAuditTask)) {
            throw "Scheduled task not found: $TaskName"
        }
        Start-ScheduledTask -TaskPath $taskPath -TaskName $TaskName
        Write-Output "[OK] started task=$TaskName"
    }
    'uninstall' {
        if (Get-ExactAuditTask) {
            Unregister-ScheduledTask -TaskPath $taskPath -TaskName $TaskName -Confirm:$false
            Write-Output "[OK] uninstalled task=$TaskName"
        } else {
            Write-Output "[OK] task_already_absent name=$TaskName"
        }
    }
}
