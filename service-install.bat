@echo off
:: Must run as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Run this script as Administrator
    pause
    exit /b 1
)

echo === Installing RichIris via NSSM ===

:: Remove existing service if present
nssm status RichIris >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Removing existing service...
    nssm stop RichIris >nul 2>&1
    nssm remove RichIris confirm
)

:: Create logs directory
if not exist "C:\01-Self-Hosting\RichIris\data\logs" mkdir "C:\01-Self-Hosting\RichIris\data\logs"

:: Install service
nssm install RichIris "C:\Users\Richard\AppData\Local\Programs\Python\Python313\python.exe" "run.py"
nssm set RichIris AppDirectory "C:\01-Self-Hosting\RichIris\backend"
nssm set RichIris DisplayName "RichIris NVR"
nssm set RichIris Description "RichIris Network Video Recorder"
nssm set RichIris Start SERVICE_AUTO_START
nssm set RichIris AppStdout "C:\01-Self-Hosting\RichIris\data\logs\service-stdout.log"
nssm set RichIris AppStderr "C:\01-Self-Hosting\RichIris\data\logs\service-stderr.log"
nssm set RichIris AppRotateFiles 1
nssm set RichIris AppRotateBytes 10485760

echo.
echo Starting service...
nssm start RichIris
echo.
echo Service installed and started. Access at http://localhost:8700
pause
