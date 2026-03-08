@echo off
:: Must run as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Run this script as Administrator
    pause
    exit /b 1
)

echo Restarting RichIris service...
net stop RichIris
net start RichIris
echo Done.
pause
