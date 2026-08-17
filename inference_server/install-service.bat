@echo off
REM Install the RichIris inference server as a Windows service (NSSM).
REM Run from an elevated prompt. Uses the same system Python as the RichIris
REM backend service (it already has fastapi/uvicorn/numpy/cv2/onnxruntime-directml).

set SVC=RichIrisInference
set DIR=%~dp0
set PYTHON=C:\Users\Richard\AppData\Local\Programs\Python\Python313\python.exe

if not exist "%PYTHON%" (
    echo Python not found at %PYTHON%
    exit /b 1
)

nssm stop %SVC% >nul 2>&1
nssm remove %SVC% confirm >nul 2>&1

if not exist "%DIR%logs" mkdir "%DIR%logs"

nssm install %SVC% "%PYTHON%" server.py
nssm set %SVC% AppDirectory "%DIR:~0,-1%"
nssm set %SVC% DisplayName "RichIris Inference Server"
nssm set %SVC% Description "GPU inference (RT-DETR/SCRFD/ArcFace) for the RichIris NVR on the Debian box"
nssm set %SVC% Start SERVICE_AUTO_START
nssm set %SVC% AppStdout "%DIR%logs\service-stdout.log"
nssm set %SVC% AppStderr "%DIR%logs\service-stderr.log"
nssm set %SVC% AppRotateFiles 1
nssm set %SVC% AppRotateBytes 10485760

nssm start %SVC%
nssm status %SVC%
echo.
echo Done. Health check: curl http://127.0.0.1:8701/health
