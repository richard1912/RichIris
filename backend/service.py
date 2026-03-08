"""Windows Service wrapper for RichIris NVR."""

import asyncio
import os
import sys
import traceback

import servicemanager
import win32event
import win32service
import win32serviceutil

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))


class RichIrisService(win32serviceutil.ServiceFramework):
    _svc_name_ = "RichIris"
    _svc_display_name_ = "RichIris NVR"
    _svc_description_ = "RichIris Network Video Recorder - camera recording and live streaming"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            self._run_server()
        except Exception:
            servicemanager.LogErrorMsg(
                f"RichIris service failed:\n{traceback.format_exc()}"
            )

    def _run_server(self):
        # Set working directory and path for imports
        os.chdir(SERVICE_DIR)
        sys.path.insert(0, SERVICE_DIR)

        # Windows services have no console - redirect stdout/stderr to log file
        # Must happen before any logging/uvicorn init that calls isatty()
        log_file = open(os.path.join(SERVICE_DIR, "service_output.log"), "a")
        sys.stdout = log_file
        sys.stderr = log_file

        import uvicorn
        from app.config import get_config

        config = get_config()
        uvi_config = uvicorn.Config(
            "app.main:app",
            host=config.server.host,
            port=config.server.port,
            reload=False,
            log_level="info",
            access_log=False,
            use_colors=False,
        )
        self.server = uvicorn.Server(uvi_config)

        # Run uvicorn in its own event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.server.serve())
        loop.close()
        log_file.close()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(RichIrisService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(RichIrisService)
