@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  RichIris NVR - Dev Environment Setup
echo ============================================
echo.
echo Downloads all external dependencies into dependencies\
echo Run this once after cloning, or to update dependency versions.
echo.

set "ROOT=%~dp0"
set "DEPS=%ROOT%dependencies"

:: All dependencies from our own GitHub release
set "GITHUB_BASE=https://github.com/richard1912/RichIris/releases/download/dependencies"
set "NSSM_URL=https://nssm.cc/release/nssm-2.24.zip"

:: Create directory structure
mkdir "%DEPS%" 2>nul
mkdir "%DEPS%\go2rtc" 2>nul
mkdir "%DEPS%\models" 2>nul

set "STEP=0"

:: -----------------------------------------------
:: 1. ffmpeg
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\ffmpeg.exe" (
    echo [%STEP%/5] ffmpeg - already present, skipping
    goto :skip_ffmpeg
)
echo [%STEP%/5] Downloading ffmpeg...
curl -L -o "%DEPS%\ffmpeg.exe" "%GITHUB_BASE%/ffmpeg.exe"
if errorlevel 1 (
    echo ERROR: Failed to download ffmpeg
    del "%DEPS%\ffmpeg.exe" 2>nul
    exit /b 1
)
echo      Done.
:skip_ffmpeg
echo.

:: -----------------------------------------------
:: 2. ffprobe
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\ffprobe.exe" (
    echo [%STEP%/5] ffprobe - already present, skipping
    goto :skip_ffprobe
)
echo [%STEP%/5] Downloading ffprobe...
curl -L -o "%DEPS%\ffprobe.exe" "%GITHUB_BASE%/ffprobe.exe"
if errorlevel 1 (
    echo ERROR: Failed to download ffprobe
    del "%DEPS%\ffprobe.exe" 2>nul
    exit /b 1
)
echo      Done.
:skip_ffprobe
echo.

:: -----------------------------------------------
:: 3. go2rtc
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\go2rtc\go2rtc.exe" (
    echo [%STEP%/5] go2rtc - already present, skipping
    goto :skip_go2rtc
)
echo [%STEP%/5] Downloading go2rtc...
curl -L -o "%DEPS%\go2rtc\go2rtc.exe" "%GITHUB_BASE%/go2rtc.exe"
if errorlevel 1 (
    echo ERROR: Failed to download go2rtc
    del "%DEPS%\go2rtc\go2rtc.exe" 2>nul
    exit /b 1
)
echo      Done.
:skip_go2rtc
echo.

:: -----------------------------------------------
:: 4. NSSM
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\nssm.exe" (
    echo [%STEP%/5] NSSM - already present, skipping
    goto :skip_nssm
)
echo [%STEP%/5] Downloading NSSM...
curl -L -o "%DEPS%\nssm.zip" "%NSSM_URL%"
if errorlevel 1 (
    echo ERROR: Failed to download NSSM
    del "%DEPS%\nssm.zip" 2>nul
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Path '%DEPS%\nssm.zip' -DestinationPath '%DEPS%\nssm_tmp' -Force"
copy "%DEPS%\nssm_tmp\nssm-2.24\win64\nssm.exe" "%DEPS%\nssm.exe" >nul
rmdir /s /q "%DEPS%\nssm_tmp" 2>nul
del "%DEPS%\nssm.zip" 2>nul
echo      Done.
:skip_nssm
echo.

:: -----------------------------------------------
:: 5. RT-DETR ONNX model
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\models\rtdetr-l.onnx" (
    echo [%STEP%/5] rtdetr-l.onnx - already present, skipping
    goto :skip_rtdetr
)
echo [%STEP%/5] Downloading RT-DETR model (126 MB)...
curl -L -o "%DEPS%\models\rtdetr-l.onnx" "%GITHUB_BASE%/rtdetr-l.onnx"
if errorlevel 1 (
    echo ERROR: Failed to download RT-DETR model
    del "%DEPS%\models\rtdetr-l.onnx" 2>nul
    exit /b 1
)
echo      Done.
:skip_rtdetr
echo.

:: -----------------------------------------------
:: 6. Python dependencies
:: -----------------------------------------------
set /a STEP+=1
echo [%STEP%/5] Installing Python packages...
pip install -r "%ROOT%backend\requirements.txt" -q
if errorlevel 1 (
    echo ERROR: pip install failed
    exit /b 1
)
echo      Done.
echo.

:: -----------------------------------------------
:: Verify
:: -----------------------------------------------
echo ============================================
echo  Verification
echo ============================================
set "MISSING="
if not exist "%DEPS%\ffmpeg.exe" set "MISSING=!MISSING! ffmpeg.exe"
if not exist "%DEPS%\ffprobe.exe" set "MISSING=!MISSING! ffprobe.exe"
if not exist "%DEPS%\go2rtc\go2rtc.exe" set "MISSING=!MISSING! go2rtc/go2rtc.exe"
if not exist "%DEPS%\nssm.exe" set "MISSING=!MISSING! nssm.exe"
if not exist "%DEPS%\models\rtdetr-l.onnx" set "MISSING=!MISSING! models/rtdetr-l.onnx"
if defined MISSING (
    echo WARNING: Missing dependencies:%MISSING%
    echo.
) else (
    echo All dependencies present:
    echo   dependencies\ffmpeg.exe
    echo   dependencies\ffprobe.exe
    echo   dependencies\go2rtc\go2rtc.exe
    echo   dependencies\nssm.exe
    echo   dependencies\models\rtdetr-l.onnx
)
echo.
echo Setup complete. You can now:
echo   - Run the backend:  cd backend ^& python run.py
echo   - Run the app:      cd app ^& flutter run -d windows
echo   - Build release:    build_release.bat
echo.
pause
