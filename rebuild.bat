@echo off
echo ============================================
echo  RichIris Full Rebuild
echo ============================================
echo.

echo [1/4] Restarting backend service...
nssm restart RichIris
echo.

echo [2/4] Building Windows release...
cd /d C:\01-Self-Hosting\RichIris\app
call flutter build windows --release
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Windows build failed!
    pause
    exit /b 1
)
echo.

echo [3/4] Building Android APK...
call flutter build apk --release
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: APK build failed!
    pause
    exit /b 1
)
echo.

echo [4/4] Done!
echo.
echo Windows: app\build\windows\x64\runner\Release\richiris.exe
echo Android: app\build\app\outputs\flutter-apk\app-release.apk
echo.
pause
