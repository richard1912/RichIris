@echo off
:: Must run as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Run this script as Administrator
    pause
    exit /b 1
)

echo === Installing RichIris Windows Service ===
cd /d "C:\01-Self-Hosting\RichIris\backend"
python service.py install
python service.py update --start=auto
echo.
echo Starting service...
net start RichIris
echo.
echo Service installed and started. Access at http://localhost:8700
pause
