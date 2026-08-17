@echo off
REM Decommission the Windows RichIris NVR service (box deployment is primary now).
REM Leaves RichIrisInference (GPU inference for the box) and RichIris-Demo untouched.
REM E:\ data (recordings/thumbnails/DB) is NOT deleted here.

echo Removing RichIris NSSM service...
nssm stop RichIris >nul 2>&1
nssm remove RichIris confirm

echo Removing stale LED scheduled tasks (broken since May, disabled)...
schtasks /delete /tn "RichIris LEDs On" /f
schtasks /delete /tn "RichIris LEDs Off" /f

echo.
echo Remaining RichIris services:
sc query RichIris >nul 2>&1 && echo   RichIris: STILL PRESENT (removal failed) || echo   RichIris: removed
sc query RichIrisInference | findstr STATE
sc query RichIris-Demo | findstr STATE
exit /b 0
