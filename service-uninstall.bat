@echo off
:: Must run as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Run this script as Administrator
    pause
    exit /b 1
)

echo === Removing RichIris Windows Service ===
net stop RichIris 2>nul
cd /d "C:\01-Self-Hosting\RichIris\backend"
python service.py remove
echo Service removed.
pause
