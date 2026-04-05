@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  RichIris NVR - Release Build
echo ============================================
echo.

set "ROOT=%~dp0"
set "DIST=%ROOT%dist\richiris"

:: Local dependencies directory
set "DEPS=%ROOT%dependencies"

:: Clean previous build
if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

:: -----------------------------------------------
:: 0. Verify nssm (always bundled)
:: -----------------------------------------------
echo [0/5] Verifying nssm...
if not exist "%DEPS%\nssm.exe" (
    echo ERROR: Missing nssm.exe in dependencies\
    echo Run setup_dev.bat to download all dependencies automatically.
    exit /b 1
)
echo      nssm.exe found.
echo      Other dependencies will be downloaded by the installer at install time.
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
echo     (dependencies downloaded by installer at install time)
echo.
echo To create the installer:
echo   ISCC.exe installer\richiris.iss
echo.
pause
