"""Windows Job Object to ensure child processes are killed when the parent dies.

Creates a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, which automatically
terminates all child processes when the parent process handle is closed (e.g., crash,
service restart). All ffmpeg subprocesses are assigned to this job.
"""

import ctypes
import ctypes.wintypes
import logging
import os

logger = logging.getLogger(__name__)

# Windows API constants
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9

kernel32 = ctypes.windll.kernel32


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_job_handle = None


def create_job_object() -> None:
    """Create a Windows Job Object that kills children when the parent exits."""
    global _job_handle

    if os.name != "nt":
        logger.debug("Not Windows, skipping Job Object creation")
        return

    _job_handle = kernel32.CreateJobObjectW(None, None)
    if not _job_handle:
        logger.error("Failed to create Job Object", extra={"error": ctypes.GetLastError()})
        return

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    result = kernel32.SetInformationJobObject(
        _job_handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not result:
        logger.error("Failed to set Job Object limits", extra={"error": ctypes.GetLastError()})
        kernel32.CloseHandle(_job_handle)
        _job_handle = None
        return

    logger.info("Windows Job Object created (children will be killed on parent exit)")


def assign_to_job(pid: int) -> bool:
    """Assign a process to the Job Object by PID."""
    if _job_handle is None:
        return False

    PROCESS_ALL_ACCESS = 0x1F0FFF
    process_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not process_handle:
        logger.warning("Failed to open process for Job Object", extra={"pid": pid, "error": ctypes.GetLastError()})
        return False

    result = kernel32.AssignProcessToJobObject(_job_handle, process_handle)
    kernel32.CloseHandle(process_handle)

    if not result:
        logger.warning("Failed to assign process to Job Object", extra={"pid": pid, "error": ctypes.GetLastError()})
        return False

    logger.debug("Assigned process to Job Object", extra={"pid": pid})
    return True
