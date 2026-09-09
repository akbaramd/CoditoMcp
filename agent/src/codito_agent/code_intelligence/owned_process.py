"""Cleanup of owned provider process trees; not an OS filesystem sandbox."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t),
        ("active", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class OwnedProcessTree:
    """Track only the process created by this caller; close its Windows Job on exit."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._handle: int | None = None
        self._kernel: Any = None  # Isolated Win32 ABI boundary.
        if os.name == "nt":
            self._attach_windows_job()

    @staticmethod
    def spawn_options() -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": 0x08000000 | 0x00000200}
        return {"start_new_session": True}

    def _attach_windows_job(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("Provider lifetime boundary could not be created")
        process_handle = None
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x00002000
            if not kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise OSError("Provider lifetime boundary could not be configured")
            process_handle = kernel.OpenProcess(0x0101, False, self.process.pid)
            if not process_handle or not kernel.AssignProcessToJobObject(handle, process_handle):
                raise OSError("Provider process could not be assigned to its lifetime boundary")
            self._kernel = kernel
            self._handle = int(handle)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        finally:
            if process_handle:
                kernel.CloseHandle(process_handle)

    async def stop(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
        elif os.name != "nt" and self.process.returncode is None:
            try:
                killpg = getattr(os, "killpg", None)
                if killpg is not None:
                    killpg(self.process.pid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass  # Already exited; no process remains to terminate.
        if self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(self.process.wait(), 3)
