@echo off
:: Must run as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Run this script as Administrator
    pause
    exit /b 1
)

echo === Removing RichIris Service ===
nssm stop RichIris
nssm remove RichIris confirm
echo Service removed.
pause
