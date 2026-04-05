# RichIris NVR - Dependency Downloader
# Called by the installer to download external dependencies at install time.
# Usage: powershell -ExecutionPolicy Bypass -File download_deps.ps1 -InstallDir "C:\Program Files\RichIris"

param(
    [Parameter(Mandatory=$true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Speeds up Invoke-WebRequest significantly

# Dependency versions
$FFMPEG_VERSION = "7.1.1"
$GO2RTC_VERSION = "1.9.14"

# Download URLs
$FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/$FFMPEG_VERSION/ffmpeg-$FFMPEG_VERSION-essentials_build.zip"
$GO2RTC_URL = "https://github.com/AlexxIT/go2rtc/releases/download/v$GO2RTC_VERSION/go2rtc_win64.zip"
$YOLO_URL   = "https://github.com/richard1912/RichIris/releases/download/models/yolo11x.onnx"

$DepsDir = Join-Path $InstallDir "dependencies"
$TempDir = Join-Path $env:TEMP "richiris_setup"

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
        Invoke-WebRequest -Uri $FFMPEG_URL -OutFile $zipPath -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        foreach ($entry in $zip.Entries) {
            if ($entry.Name -eq "ffmpeg.exe" -or $entry.Name -eq "ffprobe.exe") {
                $dest = Join-Path $DepsDir $entry.Name
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            }
        }
        $zip.Dispose()
        Write-Host "  OK"
    } catch {
        Write-Host "  FAILED: $_"
        $failed += "ffmpeg"
    }
} else {
    Write-Host "ffmpeg - already present, skipping"
}

# --- go2rtc ---
$go2rtcPath = Join-Path $DepsDir "go2rtc\go2rtc.exe"
if (-not (Test-Path $go2rtcPath)) {
    Write-Host "Downloading go2rtc $GO2RTC_VERSION..."
    try {
        $zipPath = Join-Path $TempDir "go2rtc.zip"
        Invoke-WebRequest -Uri $GO2RTC_URL -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath (Join-Path $TempDir "go2rtc_tmp") -Force
        Copy-Item (Join-Path $TempDir "go2rtc_tmp\go2rtc.exe") $go2rtcPath -Force
        Write-Host "  OK"
    } catch {
        Write-Host "  FAILED: $_"
        $failed += "go2rtc"
    }
} else {
    Write-Host "go2rtc - already present, skipping"
}

# --- YOLO ONNX model ---
$yoloPath = Join-Path $DepsDir "models\yolo11x.onnx"
if (-not (Test-Path $yoloPath)) {
    Write-Host "Downloading YOLO model (218 MB, this may take a minute)..."
    try {
        Invoke-WebRequest -Uri $YOLO_URL -OutFile $yoloPath -UseBasicParsing
        Write-Host "  OK"
    } catch {
        Write-Host "  FAILED: $_ (AI detection will not be available)"
        $failed += "yolo"
    }
} else {
    Write-Host "YOLO model - already present, skipping"
}

# Cleanup
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue }

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "WARNING: Failed to download: $($failed -join ', ')"
    Write-Host "You can re-run this script or download them manually."
    exit 1
}

Write-Host "All dependencies downloaded successfully."
exit 0
