<#
.SYNOPSIS
    dock-winstatus-ssh - the ONLY thing docker-updater's SSH key may run on a Windows machine.

.DESCRIPTION
    This is the key's forced command (authorized_keys: command="powershell ... -File <this script>").
    Whatever docker-updater sends arrives in SSH_ORIGINAL_COMMAND, and exactly two words mean anything:

        refresh   start the "Docker Updater - Windows status" scheduled task now, wait for it to finish,
                  then print the file it wrote (so "Check System Now" gets a fresh answer)
        status    just print the file

    Anything else is treated like "status". Nothing from the connection is ever run, passed to a command
    or used as a path: the task name and the file are fixed here, and the task itself only SEARCHES
    Windows Update (see dock-winstatus.ps1) - it installs and restarts nothing.

    Only the JSON goes to stdout. Install it next to dock-winstatus.ps1, in a folder only administrators
    can write to.

.PARAMETER StatusFile / TaskName / TimeoutSeconds
    Test hooks. The forced command cannot pass parameters, so on a real connection these are the defaults.
#>
param(
    [string]$StatusFile = (Join-Path $env:ProgramData 'dock-winstatus\status.json'),
    [string]$TaskName = 'Docker Updater - Windows status',
    [int]$TimeoutSeconds = 150
)

$verb = ([string]$env:SSH_ORIGINAL_COMMAND).Trim()

if ($verb -eq 'refresh') {
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
        $started = Get-Date
        do {
            Start-Sleep -Seconds 2
            $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
        } while ($state -eq 'Running' -and ((Get-Date) - $started).TotalSeconds -lt $TimeoutSeconds)
    }
    catch {
        # Could not start or watch the task: fall through and print whatever is there. The age of the
        # file is in it, so docker-updater still warns when it is stale.
    }
}

if (Test-Path -LiteralPath $StatusFile) {
    [Console]::Out.Write([IO.File]::ReadAllText($StatusFile))
    exit 0
}
exit 1
