"""Windows-only read handles. Pin ancestors; never reopen a checked file by name."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from types import TracebackType
from typing import Any

from codito_protocol.device_read import normalize_read_scope

from .errors import AgentError
from .paths import _assert_fixed_drive, _assert_supported_filesystem, validate_relative_path


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

    @property
    def identity(self) -> str:
        return f"{self.volume:x}:{self.index_high:x}:{self.index_low:x}"


class WindowsReadScope:
    """No delete sharing pins every ancestor against rename/junction replacement.

    Attribute handles are opened before consent; data is accessed only afterwards.
    File data is read from the same validated handle, with writes excluded while read.
    """

    def __init__(self, scope_path: str) -> None:
        if os.name != "nt":
            raise AgentError("unsupported_platform", "Approved device reads require Windows")
        self.scope_path = normalize_read_scope(scope_path)
        self.root = Path(self.scope_path)
        self._handles: list[int] = []
        self._directories: dict[Path, tuple[int, _FileInfo]] = {}
        self._kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel.CreateFileW.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileInfo),
        ]
        self._kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self._kernel.GetFileType.restype = wintypes.DWORD
        self._kernel.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel.ReadFile.restype = wintypes.BOOL
        self.identity = ""

    def __enter__(self) -> WindowsReadScope:
        try:
            _assert_fixed_drive(self.root)
            _assert_supported_filesystem(self.root)
            current = Path(self.root.anchor)
            _, info = self._open(current, directory=True)
            for component in self.root.parts[1:]:
                current /= component
                _, info = self._open(current, directory=True)
            self.identity = info.identity
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for handle in reversed(self._handles):
            self._kernel.CloseHandle(handle)
        self._handles.clear()
        self._directories.clear()

    def release_for_mutation(self) -> None:
        """Release read pins before an atomic rename; callers must revalidate identities."""
        self.close()

    def _open(self, path: Path, *, directory: bool) -> tuple[int, _FileInfo]:
        if directory and path in self._directories:
            return self._directories[path]
        # OPEN_REPARSE_POINT + BACKUP_SEMANTICS, OPEN_EXISTING. No elevation.
        handle = self._kernel.CreateFileW(
            str(path),
            0x81 if directory else 0x80000000,  # LIST_DIRECTORY | READ_ATTRIBUTES
            1,  # Neither write nor delete sharing: also blocks in-place reparse conversion.
            None,
            3,
            0x02200000,
            None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise AgentError("read_failed", "Windows denied access or the path is unavailable")
        self._handles.append(handle)
        info = _FileInfo()
        if not self._kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise AgentError("read_failed", "Windows could not validate the opened handle")
        if self._kernel.GetFileType(handle) != 1:
            raise AgentError("unsafe_path", "Only local disk files can be read")
        # Reparse, offline, recall-on-open/data-access (cloud placeholder) attributes.
        if info.attributes & (0x400 | 0x1000 | 0x40000 | 0x400000):
            raise AgentError("unsafe_path", "Reparse points and cloud placeholders are blocked")
        if bool(info.attributes & 0x10) != directory:
            raise AgentError("invalid_path", "The path has the wrong file type")
        if not directory and info.links != 1:
            raise AgentError("unsafe_path", "Hardlinked files cannot use directory read consent")
        if directory:
            self._directories[path] = (handle, info)
        return handle, info

    def _target(
        self, relative: str, *, directory: bool
    ) -> tuple[Path, int | None, _FileInfo | None]:
        parts = validate_relative_path(relative, allow_root=directory)
        # validate_relative_path returns a normalized relative string.
        components = Path(parts).parts if parts else ()
        current = self.root
        handle: int | None = None
        info: _FileInfo | None = None
        for index, component in enumerate(components):
            current /= component
            handle, info = self._open(current, directory=directory or index < len(components) - 1)
        return current, handle, info

    def directory(self, relative: str) -> Path:
        path, _, _ = self._target(relative, directory=True)
        return path

    def file_size(self, relative: str) -> int:
        """Validate a file by handle without reading its content; release it immediately."""
        _, handle, info = self._target(relative, directory=False)
        if handle is None or info is None:
            raise AgentError("invalid_path", "A file path is required")
        self._handles.remove(handle)
        self._kernel.CloseHandle(handle)
        return int((info.size_high << 32) | info.size_low)

    def read_bytes(
        self,
        relative: str,
        check: Callable[[], None],
        *,
        release_file: bool = False,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> bytes:
        _, handle, info = self._target(relative, directory=False)
        if handle is None or info is None:
            raise AgentError("invalid_path", "A file path is required")
        try:
            return self._read_handle(handle, info, check, max_bytes)
        finally:
            if release_file:
                self._handles.remove(handle)
                self._kernel.CloseHandle(handle)

    def _read_handle(
        self, handle: int, info: _FileInfo, check: Callable[[], None], maximum: int
    ) -> bytes:
        if not 0 <= maximum <= 16 * 1024 * 1024:
            raise AgentError("invalid_request", "Read byte budget is out of range")
        if (info.size_high << 32) | info.size_low > maximum:
            raise AgentError("file_too_large", "File exceeds the remaining read byte budget")
        data = bytearray()
        buffer = ctypes.create_string_buffer(65536)
        while True:
            check()
            count = wintypes.DWORD()
            if not self._kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise AgentError("read_failed", "Windows could not read the validated handle")
            if count.value == 0:
                return bytes(data)
            data.extend(buffer.raw[: count.value])
            if len(data) > maximum:
                raise AgentError("file_too_large", "Device text reads are limited to 16 MiB files")
