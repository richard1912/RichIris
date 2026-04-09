# RichIris NVR - Dependency Downloader
# Called by the installer to download external dependencies at install time.
# Usage: powershell -ExecutionPolicy Bypass -File download_deps.ps1 -InstallDir "C:\Program Files\RichIris"

param(
    [Parameter(Mandatory=$true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Continue"

# Enable TLS 1.2 (required for GitHub HTTPS on older Windows/PowerShell)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Log file for debugging
$LogFile = Join-Path $env:TEMP "richiris_deps.log"
"" | Out-File $LogFile -Encoding utf8
function Log { param([string]$msg) $ts = Get-Date -Format "HH:mm:ss"; "$ts $msg" | Out-File $LogFile -Append -Encoding utf8; Write-Host $msg }

Log "Starting dependency download"
Log "InstallDir: $InstallDir"
Log "PowerShell: $($PSVersionTable.PSVersion)"
Log "TLS: $([Net.ServicePointManager]::SecurityProtocol)"

# Dependency versions
$FFMPEG_VERSION = "7.1.1"
$GO2RTC_VERSION = "1.9.14"

# Download URLs
$FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/$FFMPEG_VERSION/ffmpeg-$FFMPEG_VERSION-essentials_build.zip"
$GO2RTC_URL = "https://github.com/AlexxIT/go2rtc/releases/download/v$GO2RTC_VERSION/go2rtc_win64.zip"
$YOLO_URL   = "https://github.com/richard1912/RichIris/releases/download/models/yolo11x.onnx"

$DepsDir = Join-Path $InstallDir "dependencies"
$TempDir = Join-Path $env:TEMP "richiris_setup"

# Synchronous download with WebClient (reliable, follows redirects)
function Download-File {
    param([string]$Url, [string]$OutFile, [string]$Label)
    Log "Downloading $Label from $Url"

    # Get file size via HEAD request for progress calculation
    $totalBytes = 0
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "HEAD"
        $req.AllowAutoRedirect = $true
        $resp = $req.GetResponse()
        $totalBytes = $resp.ContentLength
        $resp.Close()
    } catch { }
    $totalMB = [math]::Round($totalBytes / 1MB, 1)

    # Start synchronous download in a background job
    $job = Start-Job -ScriptBlock {
        param($u, $o)
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $c = New-Object System.Net.WebClient
        $c.DownloadFile($u, $o)
        $c.Dispose()
    } -ArgumentList $Url, $OutFile

    # Poll file size for progress while job runs
    while ($job.State -eq 'Running') {
        Start-Sleep -Milliseconds 500
        if (Test-Path $OutFile) {
            $sizeMB = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
            if ($totalBytes -gt 0) {
                $pct = [math]::Min(100, [math]::Round(($sizeMB / $totalMB) * 100))
                Write-Host "`r  $Label : $sizeMB MB / $totalMB MB ($pct%)    " -NoNewline
            } else {
                Write-Host "`r  $Label : $sizeMB MB downloaded    " -NoNewline
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

    $sizeMB = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
    Log "  Downloaded: $sizeMB MB"
}

# Clean temp
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force }
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

# Create directory structure
New-Item -ItemType Directory -Path (Join-Path $DepsDir "go2rtc") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $DepsDir "models") -Force | Out-Null

$failed = @()

# --- ffmpeg + ffprobe ---
$ffmpegPath = Join-Path $DepsDir "ffmpeg.exe"
$ffprobePath = Join-Path $DepsDir "ffprobe.exe"
if (-not (Test-Path $ffmpegPath) -or -not (Test-Path $ffprobePath)) {
    Write-Host "Downloading ffmpeg $FFMPEG_VERSION..."
    try {
        $zipPath = Join-Path $TempDir "ffmpeg.zip"
        Download-File -Url $FFMPEG_URL -OutFile $zipPath -Label "ffmpeg"
        Write-Host "  Extracting..."
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        foreach ($entry in $zip.Entries) {
            if ($entry.Name -eq "ffmpeg.exe" -or $entry.Name -eq "ffprobe.exe") {
                $dest = Join-Path $DepsDir $entry.Name
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            }
        }
        $zip.Dispose()
        Log "ffmpeg: OK"
    } catch {
        Log "ffmpeg: FAILED - $_"
        $failed += "ffmpeg"
    }
} else {
    Log "ffmpeg: already present, skipping"
}

# --- go2rtc ---
$go2rtcPath = Join-Path $DepsDir "go2rtc\go2rtc.exe"
if (-not (Test-Path $go2rtcPath)) {
    Write-Host "Downloading go2rtc $GO2RTC_VERSION..."
    try {
        $zipPath = Join-Path $TempDir "go2rtc.zip"
        Download-File -Url $GO2RTC_URL -OutFile $zipPath -Label "go2rtc"
        Write-Host "  Extracting..."
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        foreach ($entry in $zip.Entries) {
            if ($entry.Name -eq "go2rtc.exe") {
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $go2rtcPath, $true)
            }
        }
        $zip.Dispose()
        Log "go2rtc: OK"
    } catch {
        Log "go2rtc: FAILED - $_"
        $failed += "go2rtc"
    }
} else {
    Log "go2rtc: already present, skipping"
}

# --- YOLO ONNX model ---
$yoloPath = Join-Path $DepsDir "models\yolo11x.onnx"
if (-not (Test-Path $yoloPath)) {
    Write-Host "Downloading YOLO model (218 MB, please wait)..."
    try {
        Download-File -Url $YOLO_URL -OutFile $yoloPath -Label "YOLO model"
        Log "YOLO: OK"
    } catch {
        Log "YOLO: FAILED - $_ (AI detection will not be available)"
        $failed += "yolo"
    }
} else {
    Log "YOLO: already present, skipping"
}

# Cleanup temp
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue }

# Verify all files exist and have reasonable sizes
Log ""
Log "Verifying downloads..."
$checks = @(
    @{ Path = $ffmpegPath;  Name = "ffmpeg.exe";  MinMB = 50 },
    @{ Path = $ffprobePath; Name = "ffprobe.exe"; MinMB = 50 },
    @{ Path = $go2rtcPath;  Name = "go2rtc.exe";  MinMB = 5 }
)

foreach ($check in $checks) {
    if (Test-Path $check.Path) {
        $sizeMB = [math]::Round((Get-Item $check.Path).Length / 1MB, 1)
        if ($sizeMB -lt $check.MinMB) {
            Log "  WARN: $($check.Name) is only $sizeMB MB (expected >$($check.MinMB) MB)"
            $failed += $check.Name
        } else {
            Log "  OK: $($check.Name) ($sizeMB MB)"
        }
    } else {
        Log "  MISSING: $($check.Name)"
        $failed += $check.Name
    }
}

# YOLO is optional (AI detection only)
if (Test-Path $yoloPath) {
    $sizeMB = [math]::Round((Get-Item $yoloPath).Length / 1MB, 1)
    Log "  OK: yolo11x.onnx ($sizeMB MB)"
} else {
    Log "  MISSING: yolo11x.onnx (AI detection disabled)"
}

if ($failed.Count -gt 0) {
    Log ""
    Log "WARNING: Issues with: $($failed -join ', ')"
    Log "Closing in 10 seconds..."
    Start-Sleep -Seconds 10
    exit 1
}

Log ""
Log "All dependencies downloaded and verified."
Log "Closing in 10 seconds..."
Start-Sleep -Seconds 10
exit 0
