@echo off
REM Cutover step 1: stop and disable the Windows RichIris NVR service.
REM (Kept installed for rollback: nssm set RichIris Start SERVICE_AUTO_START && nssm start RichIris)
nssm stop RichIris
nssm set RichIris Start SERVICE_DISABLED
sc query RichIris | findstr STATE
taskkill /F /IM go2rtc.exe >nul 2>&1
echo Done. Remaining ffmpeg/go2rtc processes (should only be RichIris-Demo's):
tasklist | findstr /i "ffmpeg go2rtc"
exit /b 0
