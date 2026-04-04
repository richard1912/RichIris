@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  RichIris NVR - Release Build
echo ============================================
echo.

set "ROOT=%~dp0"
set "DIST=%ROOT%dist\richiris"

:: Local dependencies directory (all binaries managed here)
set "DEPS=%ROOT%dependencies"

:: Check for --slim flag (online installer: deps downloaded at install time)
set "SLIM="
if "%1"=="--slim" set "SLIM=1"

:: Clean previous build
if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

:: -----------------------------------------------
:: 0. Verify local dependencies
:: -----------------------------------------------
echo [0/5] Verifying dependencies in dependencies\ ...

:: nssm is always needed (bundled even in slim mode)
if not exist "%DEPS%\nssm.exe" (
    echo ERROR: Missing nssm.exe in dependencies\
    echo Run setup_dev.bat to download all dependencies automatically.
    exit /b 1
)

if not defined SLIM (
    set "DEP_MISSING="
    if not exist "%DEPS%\ffmpeg.exe" set "DEP_MISSING=!DEP_MISSING! ffmpeg.exe"
    if not exist "%DEPS%\ffprobe.exe" set "DEP_MISSING=!DEP_MISSING! ffprobe.exe"
    if not exist "%DEPS%\go2rtc\go2rtc.exe" set "DEP_MISSING=!DEP_MISSING! go2rtc\go2rtc.exe"
    if not exist "%DEPS%\models\yolo11x.onnx" set "DEP_MISSING=!DEP_MISSING! models\yolo11x.onnx"
    if defined DEP_MISSING (
        echo ERROR: Missing dependencies:!DEP_MISSING!
        echo Run setup_dev.bat to download all dependencies automatically.
        exit /b 1
    )
    echo      All dependencies found.
) else (
    echo      Slim mode: dependencies will be downloaded at install time.
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
mkdir "%ROOT%dist" 2>nul
move "%ROOT%backend\dist\richiris" "%DIST%" >nul

:: Copy Flutter app
xcopy "%ROOT%app\build\windows\x64\runner\Release\*" "%DIST%\app\" /s /e /q /y >nul

:: NSSM always bundled (tiny, needed during service install)
copy "%DEPS%\nssm.exe" "%DIST%\" >nul

if not defined SLIM (
    :: Full mode: bundle all dependencies
    mkdir "%DIST%\dependencies" 2>nul
    mkdir "%DIST%\dependencies\go2rtc" 2>nul
    mkdir "%DIST%\dependencies\models" 2>nul
    copy "%DEPS%\ffmpeg.exe" "%DIST%\dependencies\" >nul
    copy "%DEPS%\ffprobe.exe" "%DIST%\dependencies\" >nul
    copy "%DEPS%\go2rtc\go2rtc.exe" "%DIST%\dependencies\go2rtc\" >nul
    copy "%DEPS%\models\yolo11x.onnx" "%DIST%\dependencies\models\" >nul
    echo      Bundled all dependencies ^(full offline installer^)
) else (
    echo      Slim mode: dependencies not bundled ^(downloaded at install time^)
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
if not exist "%DIST%\nssm.exe" set "MISSING=!MISSING! nssm.exe"
if not defined SLIM (
    if not exist "%DIST%\dependencies\ffmpeg.exe" set "MISSING=!MISSING! ffmpeg.exe"
    if not exist "%DIST%\dependencies\ffprobe.exe" set "MISSING=!MISSING! ffprobe.exe"
    if not exist "%DIST%\dependencies\go2rtc\go2rtc.exe" set "MISSING=!MISSING! go2rtc.exe"
    if not exist "%DIST%\dependencies\models\yolo11x.onnx" set "MISSING=!MISSING! yolo11x.onnx"
)
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
if not defined SLIM (
echo     dependencies\         (ffmpeg, ffprobe, go2rtc, YOLO model)
) else (
echo     (dependencies downloaded at install time)
)
echo.
if not defined SLIM (
echo To create a FULL installer (offline, ~300 MB):
echo   ISCC.exe installer\richiris.iss
) else (
echo To create a SLIM installer (online, ~150 MB):
echo   ISCC.exe /DSLIM installer\richiris.iss
)
echo.
echo Both modes:  ISCC.exe installer\richiris.iss           (full)
echo              ISCC.exe /DSLIM installer\richiris.iss    (slim)
pause
