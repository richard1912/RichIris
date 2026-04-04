@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  RichIris NVR - Release Build
echo ============================================
echo.

set "ROOT=%~dp0"
set "DIST=%ROOT%dist\richiris"
set "DL_CACHE=%ROOT%.build-cache"

:: Dependency versions (update these when upgrading)
set "FFMPEG_VERSION=7.1.1"
set "GO2RTC_VERSION=1.9.14"

:: Download URLs
set "FFMPEG_URL=https://github.com/GyanD/codexffmpeg/releases/download/%FFMPEG_VERSION%/ffmpeg-%FFMPEG_VERSION%-essentials_build.zip"
set "GO2RTC_URL=https://github.com/AlexxIT/go2rtc/releases/download/v%GO2RTC_VERSION%/go2rtc_win64.zip"
set "NSSM_URL=https://nssm.cc/release/nssm-2.24.zip"

:: Clean previous build
if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

:: Create download cache (persists between builds, gitignored)
mkdir "%DL_CACHE%" 2>nul

:: -----------------------------------------------
:: 0. Download dependencies if not cached
:: -----------------------------------------------
echo [0/5] Checking dependencies...

:: --- ffmpeg + ffprobe ---
if not exist "%DL_CACHE%\ffmpeg.exe" (
    echo      Downloading ffmpeg %FFMPEG_VERSION%...
    curl -L -o "%DL_CACHE%\ffmpeg.zip" "%FFMPEG_URL%"
    if errorlevel 1 (
        echo ERROR: Failed to download ffmpeg
        exit /b 1
    )
    :: Extract ffmpeg.exe and ffprobe.exe from the bin/ folder inside the zip
    powershell -NoProfile -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::OpenRead('%DL_CACHE%\ffmpeg.zip'); foreach ($e in $zip.Entries) { if ($e.Name -eq 'ffmpeg.exe' -or $e.Name -eq 'ffprobe.exe') { [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, '%DL_CACHE%\' + $e.Name, $true) } }; $zip.Dispose()"
    del "%DL_CACHE%\ffmpeg.zip" 2>nul
    echo      Downloaded ffmpeg + ffprobe
) else (
    echo      ffmpeg cached
)

:: --- go2rtc ---
if not exist "%DL_CACHE%\go2rtc.exe" (
    echo      Downloading go2rtc %GO2RTC_VERSION%...
    curl -L -o "%DL_CACHE%\go2rtc.zip" "%GO2RTC_URL%"
    if errorlevel 1 (
        echo ERROR: Failed to download go2rtc
        exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path '%DL_CACHE%\go2rtc.zip' -DestinationPath '%DL_CACHE%\go2rtc_tmp' -Force"
    move "%DL_CACHE%\go2rtc_tmp\go2rtc.exe" "%DL_CACHE%\go2rtc.exe" >nul
    rmdir /s /q "%DL_CACHE%\go2rtc_tmp" 2>nul
    del "%DL_CACHE%\go2rtc.zip" 2>nul
    echo      Downloaded go2rtc
) else (
    echo      go2rtc cached
)

:: --- nssm ---
if not exist "%DL_CACHE%\nssm.exe" (
    echo      Downloading NSSM...
    curl -L -o "%DL_CACHE%\nssm.zip" "%NSSM_URL%"
    if errorlevel 1 (
        echo ERROR: Failed to download NSSM
        exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path '%DL_CACHE%\nssm.zip' -DestinationPath '%DL_CACHE%\nssm_tmp' -Force"
    copy "%DL_CACHE%\nssm_tmp\nssm-2.24\win64\nssm.exe" "%DL_CACHE%\nssm.exe" >nul
    rmdir /s /q "%DL_CACHE%\nssm_tmp" 2>nul
    del "%DL_CACHE%\nssm.zip" 2>nul
    echo      Downloaded NSSM
) else (
    echo      nssm cached
)
echo.

:: -----------------------------------------------
:: 1. Build backend with PyInstaller
:: -----------------------------------------------
echo [1/5] Building backend with PyInstaller...
cd /d "%ROOT%backend"
pyinstaller richiris.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)
echo      Backend built successfully.
echo.

:: -----------------------------------------------
:: 2. Build Flutter Windows app
:: -----------------------------------------------
echo [2/5] Building Flutter Windows app...
cd /d "%ROOT%app"
call flutter build windows --release
if errorlevel 1 (
    echo ERROR: Flutter build failed
    exit /b 1
)
echo      Flutter app built successfully.
echo.

:: -----------------------------------------------
:: 3. Assemble distribution
:: -----------------------------------------------
echo [3/5] Assembling distribution...

:: Move PyInstaller output to dist root
move "%ROOT%backend\dist\richiris" "%DIST%" >nul

:: Copy Flutter app
xcopy "%ROOT%app\build\windows\x64\runner\Release\*" "%DIST%\app\" /s /e /q /y >nul

:: Copy dependencies from cache
mkdir "%DIST%\dependencies" 2>nul
mkdir "%DIST%\dependencies\go2rtc" 2>nul
copy "%DL_CACHE%\ffmpeg.exe" "%DIST%\dependencies\" >nul
copy "%DL_CACHE%\ffprobe.exe" "%DIST%\dependencies\" >nul
copy "%DL_CACHE%\go2rtc.exe" "%DIST%\dependencies\go2rtc\" >nul
copy "%DL_CACHE%\nssm.exe" "%DIST%\" >nul

:: Copy YOLO ONNX model (for AI object detection)
if exist "%ROOT%data\yolo11x.onnx" (
    mkdir "%DIST%\models" 2>nul
    copy "%ROOT%data\yolo11x.onnx" "%DIST%\models\" >nul
    echo      Bundled YOLO ONNX model
) else (
    echo      WARNING: yolo11x.onnx not found in data\ - AI detection will not work
)

:: Generate default bootstrap.yaml
(
echo data_dir: "C:/ProgramData/RichIris"
echo port: 8700
) > "%DIST%\bootstrap.yaml"

echo      Distribution assembled at: %DIST%
echo.

:: -----------------------------------------------
:: 4. Verify
:: -----------------------------------------------
echo [4/5] Verifying...
set "MISSING="
if not exist "%DIST%\richiris.exe" set "MISSING=!MISSING! richiris.exe"
if not exist "%DIST%\app\richiris.exe" set "MISSING=!MISSING! app\richiris.exe"
if not exist "%DIST%\dependencies\ffmpeg.exe" set "MISSING=!MISSING! ffmpeg.exe"
if not exist "%DIST%\dependencies\ffprobe.exe" set "MISSING=!MISSING! ffprobe.exe"
if not exist "%DIST%\dependencies\go2rtc\go2rtc.exe" set "MISSING=!MISSING! go2rtc.exe"
if not exist "%DIST%\nssm.exe" set "MISSING=!MISSING! nssm.exe"
if defined MISSING (
    echo ERROR: Missing files:%MISSING%
    exit /b 1
)
echo      All files present.
echo.

:: -----------------------------------------------
:: 5. Summary
:: -----------------------------------------------
echo [5/5] Build complete!
echo.
echo Distribution layout:
echo   %DIST%\
echo     richiris.exe          (NVR backend)
echo     nssm.exe              (service manager)
echo     bootstrap.yaml        (minimal config)
echo     app\                  (Flutter desktop app)
echo     dependencies\         (ffmpeg, ffprobe, go2rtc)
echo.
echo To create an installer, run Inno Setup with installer\richiris.iss
pause
