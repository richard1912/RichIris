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

:: Dependency versions (update these when upgrading)
set "FFMPEG_VERSION=7.1.1"
set "GO2RTC_VERSION=1.9.14"
set "RTDETR_MODEL=rtdetr-l"

:: Download URLs
set "FFMPEG_URL=https://github.com/GyanD/codexffmpeg/releases/download/%FFMPEG_VERSION%/ffmpeg-%FFMPEG_VERSION%-essentials_build.zip"
set "GO2RTC_URL=https://github.com/AlexxIT/go2rtc/releases/download/v%GO2RTC_VERSION%/go2rtc_win64.zip"
set "NSSM_URL=https://nssm.cc/release/nssm-2.24.zip"

:: Create directory structure
mkdir "%DEPS%" 2>nul
mkdir "%DEPS%\go2rtc" 2>nul
mkdir "%DEPS%\models" 2>nul

set "STEP=0"

:: -----------------------------------------------
:: 1. ffmpeg + ffprobe
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\ffmpeg.exe" (
    if exist "%DEPS%\ffprobe.exe" (
        echo [%STEP%/5] ffmpeg %FFMPEG_VERSION% - already present, skipping
        goto :skip_ffmpeg
    )
)
echo [%STEP%/5] Downloading ffmpeg %FFMPEG_VERSION%...
curl -L -o "%DEPS%\ffmpeg.zip" "%FFMPEG_URL%"
if errorlevel 1 (
    echo ERROR: Failed to download ffmpeg
    del "%DEPS%\ffmpeg.zip" 2>nul
    exit /b 1
)
echo      Extracting ffmpeg.exe and ffprobe.exe...
powershell -NoProfile -Command ^
    "Add-Type -AssemblyName System.IO.Compression.FileSystem; ^
     $zip = [System.IO.Compression.ZipFile]::OpenRead('%DEPS%\ffmpeg.zip'); ^
     foreach ($e in $zip.Entries) { ^
         if ($e.Name -eq 'ffmpeg.exe' -or $e.Name -eq 'ffprobe.exe') { ^
             [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, '%DEPS%\' + $e.Name, $true) ^
         } ^
     }; ^
     $zip.Dispose()"
del "%DEPS%\ffmpeg.zip" 2>nul
echo      Done.
:skip_ffmpeg
echo.

:: -----------------------------------------------
:: 2. go2rtc
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\go2rtc\go2rtc.exe" (
    echo [%STEP%/5] go2rtc %GO2RTC_VERSION% - already present, skipping
    goto :skip_go2rtc
)
echo [%STEP%/5] Downloading go2rtc %GO2RTC_VERSION%...
curl -L -o "%DEPS%\go2rtc.zip" "%GO2RTC_URL%"
if errorlevel 1 (
    echo ERROR: Failed to download go2rtc
    del "%DEPS%\go2rtc.zip" 2>nul
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Path '%DEPS%\go2rtc.zip' -DestinationPath '%DEPS%\go2rtc_tmp' -Force"
move "%DEPS%\go2rtc_tmp\go2rtc.exe" "%DEPS%\go2rtc\go2rtc.exe" >nul
rmdir /s /q "%DEPS%\go2rtc_tmp" 2>nul
del "%DEPS%\go2rtc.zip" 2>nul
echo      Done.
:skip_go2rtc
echo.

:: -----------------------------------------------
:: 3. NSSM
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
:: 4. RT-DETR ONNX model
:: -----------------------------------------------
set /a STEP+=1
if exist "%DEPS%\models\%RTDETR_MODEL%.onnx" (
    echo [%STEP%/5] %RTDETR_MODEL%.onnx - already present, skipping
    goto :skip_rtdetr
)
echo [%STEP%/5] Downloading %RTDETR_MODEL% and exporting to ONNX...
echo      This requires ultralytics: pip install ultralytics
echo.

:: Check if the .pt file exists (may have been downloaded manually)
if exist "%DEPS%\models\%RTDETR_MODEL%.pt" (
    echo      Found %RTDETR_MODEL%.pt, exporting to ONNX...
    goto :export_onnx
)

:: Download the .pt model via ultralytics (auto-downloads from GitHub)
echo      Exporting %RTDETR_MODEL% to ONNX (will auto-download .pt)...

:export_onnx
:: Export to ONNX using ultralytics (opset 17 required for DirectML performance)
python -c "from ultralytics import RTDETR; model = RTDETR(r'%DEPS%\models\%RTDETR_MODEL%.pt' if __import__('os').path.exists(r'%DEPS%\models\%RTDETR_MODEL%.pt') else '%RTDETR_MODEL%.pt'); model.export(format='onnx', imgsz=640, simplify=True, opset=17)"
if errorlevel 1 (
    echo.
    echo ERROR: ONNX export failed. Make sure ultralytics is installed:
    echo        pip install ultralytics
    echo.
    echo Alternatively, export manually and place %RTDETR_MODEL%.onnx in dependencies\models\
    exit /b 1
)

:: Move the exported .onnx file if it was exported to the current directory
if exist "%RTDETR_MODEL%.onnx" (
    move "%RTDETR_MODEL%.onnx" "%DEPS%\models\%RTDETR_MODEL%.onnx" >nul
)

if exist "%DEPS%\models\%RTDETR_MODEL%.onnx" (
    echo      ONNX export successful.
) else (
    echo ERROR: ONNX file not found after export
    exit /b 1
)

:: Clean up .pt file (not needed at runtime)
del "%DEPS%\models\%RTDETR_MODEL%.pt" 2>nul
del "%RTDETR_MODEL%.pt" 2>nul
echo      Done.
:skip_rtdetr
echo.

:: -----------------------------------------------
:: 5. Python dependencies
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
if not exist "%DEPS%\models\%RTDETR_MODEL%.onnx" set "MISSING=!MISSING! models/%RTDETR_MODEL%.onnx"
if defined MISSING (
    echo WARNING: Missing dependencies:%MISSING%
    echo.
) else (
    echo All dependencies present:
    echo   dependencies\ffmpeg.exe
    echo   dependencies\ffprobe.exe
    echo   dependencies\go2rtc\go2rtc.exe
    echo   dependencies\nssm.exe
    echo   dependencies\models\%RTDETR_MODEL%.onnx
)
echo.
echo Setup complete. You can now:
echo   - Run the backend:  cd backend ^& python run.py
echo   - Run the app:      cd app ^& flutter run -d windows
echo   - Build release:    build_release.bat
echo.
pause
