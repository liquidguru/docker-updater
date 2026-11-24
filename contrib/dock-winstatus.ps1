<#
.SYNOPSIS
    dock-winstatus - the Windows half of Docker Updater's optional "System updates" feature (read-only).

.DESCRIPTION
    Run it on a schedule (Task Scheduler, as SYSTEM). It ASKS Windows Update, read-only, which updates
    are waiting, checks whether a reboot is pending, and writes the answers to ONE small JSON file that
    Docker Updater reads over an SSH key locked to printing that file.

    It only SEARCHES. It never downloads, installs, hides, uninstalls or restarts anything, and uses no
    other Windows Update call. Updates are installed in Windows Update as usual.

    Microsoft Defender definition updates ("Security Intelligence Update ...") are left out by default:
    they arrive several times a day and install themselves, so they would only be noise. Use
    -IncludeDefinitions to list them.

.PARAMETER Out
    Where to write the file. Default: C:\ProgramData\dock-winstatus\status.json (readable by every user,
    writable by SYSTEM and administrators only).

.PARAMETER SkipSearch
    Test hook: do not contact Windows Update (the update list is left empty).
#>
param(
    [string]$Out = (Join-Path $env:ProgramData 'dock-winstatus\status.json'),
    [switch]$IncludeDefinitions,
    [switch]$SkipSearch
)

$ErrorActionPreference = 'Stop'

function Get-RebootReasons {
    # The two places Windows itself records "restart needed". PendingFileRenameOperations is left out on
    # purpose: installers set it constantly and it is a famous source of false alarms.
    $reasons = @()
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
        $reasons += 'Windows Update'
    }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
        $reasons += 'Component servicing'
    }
    return , $reasons
}

function Get-PendingUpdates {
    param([bool]$Definitions)
    $session = New-Object -ComObject Microsoft.Update.Session
    $result = $session.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
    $list = @()
    foreach ($u in $result.Updates) {
        $cats = @($u.Categories | ForEach-Object { $_.Name })
        $isDefinition = ($cats -contains 'Definition Updates') -or ($u.Title -like 'Security Intelligence Update*')
        if ($isDefinition -and -not $Definitions) { continue }
        $list += [ordered]@{
            title       = [string]$u.Title
            kb          = (@($u.KBArticleIDs | ForEach-Object { "KB$_" }) -join ', ')
            categories  = $cats
            security    = [bool](($cats -contains 'Security Updates') -or $u.MsrcSeverity)
            reboot_hint = [bool]($u.InstallationBehavior.RebootBehavior -ne 0)
            size_mb     = [int]($u.MaxDownloadSize / 1MB)
            downloaded  = [bool]$u.IsDownloaded
        }
    }
    return , $list
}

function Write-Atomic {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = Join-Path $dir ('.status-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($tmp, $Text, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

$status = [ordered]@{
    schema   = 1
    kind     = 'windows'
    time     = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    hostname = $env:COMPUTERNAME
}
$code = 0
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $ubr = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR
    $reasons = Get-RebootReasons
    $status.os = '{0} {1}.{2}' -f $os.Caption, $os.Version, $ubr
    $status.reboot_required = [bool]($reasons.Count -gt 0)
    $status.reboot_reasons = $reasons
    if ($SkipSearch) { $status.updates = @() }
    else { $status.updates = Get-PendingUpdates -Definitions ([bool]$IncludeDefinitions) }
}
catch {
    # Leave an honest error file rather than a stale good one.
    $status = [ordered]@{
        schema = 1; kind = 'windows'; hostname = $env:COMPUTERNAME
        time   = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        error  = $_.Exception.Message
    }
    $code = 1
}

Write-Atomic -Path $Out -Text (ConvertTo-Json -InputObject $status -Depth 6 -Compress)
Write-Output (('wrote ' + $Out) + $(if ($code) { ' (ERROR file)' } else { '' }))
exit $code
