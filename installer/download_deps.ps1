# RichIris NVR - Dependency Downloader
# Called by the installer to download external dependencies at install time.
# Usage: powershell -ExecutionPolicy Bypass -File download_deps.ps1 -InstallDir "C:\Program Files\RichIris"

param(
    [Parameter(Mandatory=$true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "  *** Do NOT click this window - it will pause the download ***" -ForegroundColor Yellow
Write-Host "  *** If it freezes, press Enter or right-click to resume ***" -ForegroundColor Yellow
Write-Host ""

# Enable TLS 1.2 (required for GitHub HTTPS on older Windows/PowerShell)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Log file for debugging
$LogFile = Join-Path $env:TEMP "richiris_deps.log"
"" | Out-File $LogFile -Encoding utf8
function Log {
    param([string]$msg)
    $ts = Get-Date -Format "HH:mm:ss"
    "$ts $msg" | Out-File $LogFile -Append -Encoding utf8
    Write-Host $msg
}

Log "Starting dependency download"
Log "InstallDir: $InstallDir"

# All dependencies hosted on our own GitHub release
$GITHUB_BASE = "https://github.com/richard1912/RichIris/releases/download/dependencies"

$DepsDir = Join-Path $InstallDir "dependencies"
$TempDir = Join-Path $env:TEMP "richiris_setup"

function Download-File {
    param([string]$Url, [string]$OutFile, [string]$Label)
    Log "Downloading $Label ..."

    # Get file size via HEAD request for progress
    $totalBytes = 0
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "HEAD"
        $req.AllowAutoRedirect = $true
        $resp = $req.GetResponse()
        $totalBytes = $resp.ContentLength
        $resp.Close()
    } catch { }
    $totalSize = [math]::Round($totalBytes / 1MB, 1)

    # Download in background job
    $job = Start-Job -ScriptBlock {
        param($u, $o)
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $c = New-Object System.Net.WebClient
        $c.DownloadFile($u, $o)
        $c.Dispose()
    } -ArgumentList $Url, $OutFile

    # Poll file size for progress
    while ($job.State -eq 'Running') {
        Start-Sleep -Milliseconds 500
        if (Test-Path $OutFile) {
            $cur = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
            if ($totalBytes -gt 0) {
                $pct = [math]::Min(100, [math]::Round(($cur / $totalSize) * 100))
                $msg = '  {0} : {1} / {2} ({3} pct)    ' -f $Label, $cur, $totalSize, $pct
                Write-Host ("`r" + $msg) -NoNewline
            } else {
                $msg = '  {0} : {1} downloaded    ' -f $Label, $cur
                Write-Host ("`r" + $msg) -NoNewline
            }
        }
    }
    Write-Host ""

    # Check for errors
    $result = Receive-Job $job -ErrorAction SilentlyContinue
    if ($job.State -eq 'Failed') {
        $errMsg = ($job.ChildJobs[0].JobStateInfo.Reason.Message)
        Remove-Job $job -Force
        Log "  ERROR: $errMsg"
        throw $errMsg
    }
    Remove-Job $job -Force

    $final = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
    Log ('  Downloaded: ' + $final + ' megabytes')
}

# Clean temp
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force }
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

# Create directory structure
New-Item -ItemType Directory -Path (Join-Path $DepsDir "go2rtc") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $DepsDir "models") -Force | Out-Null

$failed = @()

# --- ffmpeg ---
$ffmpegPath = Join-Path $DepsDir "ffmpeg.exe"
if (-not (Test-Path $ffmpegPath)) {
    Write-Host "Downloading ffmpeg..."
    try {
        Download-File -Url "$GITHUB_BASE/ffmpeg.exe" -OutFile $ffmpegPath -Label "ffmpeg"
        Log "ffmpeg: OK"
    } catch {
        Log "ffmpeg: FAILED - $_"
        $failed += "ffmpeg"
    }
} else {
    Log "ffmpeg: already present, skipping"
}

# --- ffprobe ---
$ffprobePath = Join-Path $DepsDir "ffprobe.exe"
if (-not (Test-Path $ffprobePath)) {
    Write-Host "Downloading ffprobe..."
    try {
        Download-File -Url "$GITHUB_BASE/ffprobe.exe" -OutFile $ffprobePath -Label "ffprobe"
        Log "ffprobe: OK"
    } catch {
        Log "ffprobe: FAILED - $_"
        $failed += "ffprobe"
    }
} else {
    Log "ffprobe: already present, skipping"
}

# --- go2rtc ---
$go2rtcPath = Join-Path $DepsDir "go2rtc\go2rtc.exe"
if (-not (Test-Path $go2rtcPath)) {
    Write-Host "Downloading go2rtc..."
    try {
        Download-File -Url "$GITHUB_BASE/go2rtc.exe" -OutFile $go2rtcPath -Label "go2rtc"
        Log "go2rtc: OK"
    } catch {
        Log "go2rtc: FAILED - $_"
        $failed += "go2rtc"
    }
} else {
    Log "go2rtc: already present, skipping"
}

# --- RT-DETR ONNX model ---
$rtdetrPath = Join-Path $DepsDir "models\rtdetr-l.onnx"
if (-not (Test-Path $rtdetrPath)) {
    Write-Host "Downloading RT-DETR model, please wait..."
    try {
        Download-File -Url "$GITHUB_BASE/rtdetr-l.onnx" -OutFile $rtdetrPath -Label "RT-DETR model"
        Log "RT-DETR: OK"
    } catch {
        Log "RT-DETR: FAILED - $_"
        $failed += "rtdetr"
    }
} else {
    Log "RT-DETR: already present, skipping"
}

# Cleanup temp
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue }

# Verify all files exist and have reasonable sizes
Log ""
Log "Verifying downloads..."
$checks = @(
    @{ Path = $ffmpegPath;  Name = "ffmpeg.exe";  MinSize = 50 },
    @{ Path = $ffprobePath; Name = "ffprobe.exe"; MinSize = 50 },
    @{ Path = $go2rtcPath;  Name = "go2rtc.exe";  MinSize = 5 }
)

foreach ($check in $checks) {
    if (Test-Path $check.Path) {
        $s = [math]::Round((Get-Item $check.Path).Length / 1MB, 1)
        if ($s -lt $check.MinSize) {
            Log ('  WARN: ' + $check.Name + ' is only ' + $s + ' megabytes')
            $failed += $check.Name
        } else {
            Log ('  OK: ' + $check.Name + ' (' + $s + ' megabytes)')
        }
    } else {
        Log ('  MISSING: ' + $check.Name)
        $failed += $check.Name
    }
}

# RT-DETR is optional (AI detection only)
if (Test-Path $rtdetrPath) {
    $s = [math]::Round((Get-Item $rtdetrPath).Length / 1MB, 1)
    Log ('  OK: rtdetr-l.onnx (' + $s + ' megabytes)')
} else {
    Log "  MISSING: rtdetr-l.onnx (AI detection disabled)"
}

if ($failed.Count -gt 0) {
    Log ""
    Log "WARNING: Some downloads failed"
    Log "Closing in 10 seconds..."
    Start-Sleep -Seconds 10
    exit 1
}

Log ""
Log "All dependencies downloaded and verified."
Log "Closing in 10 seconds..."
Start-Sleep -Seconds 10
exit 0
