# External watchdog for RichIris NVR.
# Probes /api/health; on failure restarts the service and pings ntfy.
# Run as a Scheduled Task every 60s. Logs to G:\logs\watchdog.log (falls
# back to ProgramData if G: is not mounted). Secrets loaded from a
# sibling watchdog.config.psd1 (gitignored).

$ErrorActionPreference = 'Stop'

$HealthUrl  = 'http://localhost:8700/api/health'
$Service    = 'RichIris'
$TimeoutSec = 5

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $scriptDir 'watchdog.config.psd1'
$config = if (Test-Path $configPath) { Import-PowerShellDataFile $configPath } else { @{} }

$logDir = if (Test-Path 'G:\logs') { 'G:\logs' } else { 'C:\ProgramData\RichIris\logs' }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir 'watchdog.log'

function Write-Log($level, $msg) {
    $ts = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffzzz')
    "$ts [$level] $msg" | Add-Content -Path $logFile -Encoding utf8
}

function Send-Ntfy($title, $body, $priority) {
    if (-not $config.NtfyUrl) { return }
    $headers = @{ Title = $title; Priority = $priority; Tags = 'rotating_light,cctv' }
    if ($config.NtfyUser -and $config.NtfyPassword) {
        $cred = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($config.NtfyUser):$($config.NtfyPassword)"))
        $headers['Authorization'] = "Basic $cred"
    }
    try {
        Invoke-RestMethod -Uri $config.NtfyUrl -Method Post -Body $body -Headers $headers -TimeoutSec 5 | Out-Null
    } catch {
        Write-Log 'WARN' "ntfy send failed: $($_.Exception.Message)"
    }
}

try {
    $resp = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec $TimeoutSec -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Log 'INFO' 'healthy'
        exit 0
    }
    $reason = "status $($resp.StatusCode)"
} catch {
    $reason = $_.Exception.Message
}

Write-Log 'ERROR' "probe failed ($reason) - restarting $Service"
Send-Ntfy 'RichIris NVR down' "Health probe failed: $reason`nRestarting service." 'high'

try {
    Restart-Service -Name $Service -Force
    Write-Log 'INFO' "Restart-Service issued"
} catch {
    $restartErr = $_.Exception.Message
    Write-Log 'ERROR' "Restart-Service threw: $restartErr"
    Send-Ntfy 'RichIris restart FAILED' "Restart-Service threw: $restartErr" 'urgent'
    exit 1
}

Start-Sleep -Seconds 25
try {
    $resp = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec $TimeoutSec -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Log 'INFO' 'recovered after restart'
        Send-Ntfy 'RichIris NVR recovered' 'Service is healthy again after auto-restart.' 'default'
        exit 0
    }
    $code = $resp.StatusCode
    Write-Log 'ERROR' "still unhealthy after restart (status $code)"
    Send-Ntfy 'RichIris still down' "Service restart did not recover health (status $code)." 'urgent'
    exit 1
} catch {
    $err = $_.Exception.Message
    Write-Log 'ERROR' "still unhealthy after restart: $err"
    Send-Ntfy 'RichIris still down' "Service restart did not recover health: $err" 'urgent'
    exit 1
}
