@echo off
echo === Building RichIris Frontend ===
cd /d "C:\01-Self-Hosting\RichIris\frontend"
call npm run build
if %ERRORLEVEL% NEQ 0 (
    echo Frontend build FAILED
    pause
    exit /b 1
)
echo === Frontend built to frontend\dist ===
echo.
echo Done. You can now install/restart the service:
echo   service-install.bat   (first time)
echo   service-restart.bat   (after rebuild)
