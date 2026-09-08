from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from .errors import AgentError
from .models import Project

FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
DRIVE_FIXED = 3
SUPPORTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_COMPONENT = re.compile(r'[<>:"|?*\x00-\x1f]')


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation_time", _FileTime),
        ("last_access_time", _FileTime),
        ("last_write_time", _FileTime),
        ("volume_serial", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("link_count", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


@dataclass(frozen=True, slots=True)
class FileIdentity:
    volume_serial: int
    file_id: int
    link_count: int

    @property
    def fingerprint(self) -> str:
        raw = f"{self.volume_serial:08x}:{self.file_id:016x}".encode()
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidatedPath:
    relative: str
    absolute: Path
    parent_identity: FileIdentity
    target_identity: FileIdentity | None


def _windows_attributes(path: Path) -> int:
    if os.name != "nt":
        mode = path.lstat().st_mode
        return FILE_ATTRIBUTE_REPARSE_POINT if stat.S_ISLNK(mode) else 0
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    value = int(get_attributes(str(path)))
    if value == INVALID_FILE_ATTRIBUTES:
        raise OSError(ctypes.get_last_error(), "GetFileAttributesW failed")
    return value


def _is_reparse(path: Path) -> bool:
    return bool(_windows_attributes(path) & FILE_ATTRIBUTE_REPARSE_POINT)


def file_identity(path: Path) -> FileIdentity:
    """Read stable volume/file identity without following a reparse point on Windows."""

    if os.name != "nt":
        stat_info = path.lstat()
        return FileIdentity(int(stat_info.st_dev), int(stat_info.st_ino), int(stat_info.st_nlink))

    kernel32 = ctypes.windll.kernel32
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        handle_info = _ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(handle_info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        return FileIdentity(
            int(handle_info.volume_serial),
            (int(handle_info.file_index_high) << 32) | int(handle_info.file_index_low),
            int(handle_info.link_count),
        )
    finally:
        kernel32.CloseHandle(handle)


def root_fingerprint(root: Path) -> str:
    return file_identity(root).fingerprint


def validate_relative_path(value: str, *, allow_root: bool = False) -> str:
    """Validate and normalize an untrusted project-relative Windows path."""

    if not isinstance(value, str) or "\x00" in value:
        raise AgentError("invalid_path", "Path must be a text value without NUL bytes")
    if value in {"", "."}:
        if allow_root:
            return "."
        raise AgentError("invalid_path", "A file path is required")
    if value.startswith(("\\", "//", "\\?\\", "\\.\\")):
        raise AgentError("invalid_path", "UNC and device paths are not allowed")

    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or windows.root:
        raise AgentError("invalid_path", "Only project-relative paths are allowed")

    normalized_parts: list[str] = []
    for component in value.replace("\\", "/").split("/"):
        if component in {"", ".", ".."}:
            raise AgentError("invalid_path", "Empty, dot, and parent path components are forbidden")
        if component[-1:] in {".", " "}:
            raise AgentError("invalid_path", "Trailing dots and spaces are forbidden")
        if _INVALID_COMPONENT.search(component):
            raise AgentError("invalid_path", "The path contains a forbidden Windows character")
        stem = component.split(".", 1)[0].upper()
        if stem in _RESERVED_NAMES:
            raise AgentError("invalid_path", "The path contains a reserved Windows name")
        normalized_parts.append(component)
    return "/".join(normalized_parts)


def _assert_fixed_drive(root: Path) -> None:
    if os.name != "nt":
        return
    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint32
    drive = root.drive + "\\"
    if int(get_drive_type(drive)) != DRIVE_FIXED:
        raise AgentError("invalid_project_root", "Only fixed local drives are supported")


def _assert_supported_filesystem(root: Path) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    get_volume_information = kernel32.GetVolumeInformationW
    get_volume_information.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_volume_information.restype = ctypes.c_int
    serial = ctypes.c_uint32()
    maximum_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    filesystem = ctypes.create_unicode_buffer(32)
    drive = root.drive + "\\"
    if not get_volume_information(
        drive,
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        raise OSError(ctypes.get_last_error(), "GetVolumeInformationW failed")
    if filesystem.value.upper() not in SUPPORTED_FILESYSTEMS:
        raise AgentError("invalid_project_root", "Only NTFS and ReFS project volumes are supported")


def validate_project_root(path: Path) -> tuple[Path, str]:
    raw = str(path)
    if raw.startswith(("\\", "//", "\\?\\", "\\.\\")):
        raise AgentError("invalid_project_root", "UNC and device roots are not supported")
    if not path.is_absolute():
        raise AgentError("invalid_project_root", "Project roots must be absolute")
    if not path.exists() or not path.is_dir():
        raise AgentError("invalid_project_root", "Project root must be an existing directory")
    if Path(path.anchor) == path:
        raise AgentError("invalid_project_root", "A drive root cannot be registered")
    _assert_fixed_drive(path)
    _assert_supported_filesystem(path)

    absolute = path.absolute()
    anchor = Path(absolute.anchor)
    current = anchor
    for component in absolute.parts[1:]:
        current = current / component
        if _is_reparse(current):
            raise AgentError("invalid_project_root", "Project roots cannot traverse reparse points")
        attrs = _windows_attributes(current)
        if attrs & (
            FILE_ATTRIBUTE_OFFLINE
            | FILE_ATTRIBUTE_RECALL_ON_OPEN
            | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        ):
            raise AgentError("invalid_project_root", "Cloud-placeholder roots are not supported")
    resolved = absolute.resolve(strict=True)
    return resolved, root_fingerprint(resolved)


class ProjectPathResolver:
    """Resolve paths beneath a registered root using conservative identity checks.

    Python cannot provide a Win32 handle-relative open with all of the desired NT
    semantics. This resolver therefore rejects every reparse component, captures
    parent identities, verifies opened-file identity, and requires callers to
    revalidate immediately before mutation. The native broker is the final
    authority for isolated process filesystem grants.
    """

    def verify_project(self, project: Project) -> None:
        try:
            current = root_fingerprint(project.root)
        except OSError as exc:
            raise AgentError("project_unavailable", "Project root is unavailable") from exc
        if current != project.root_fingerprint:
            raise AgentError("project_identity_changed", "Project root identity changed")
        if _is_reparse(project.root):
            raise AgentError("project_identity_changed", "Project root became a reparse point")

    def resolve(
        self,
        project: Project,
        relative: str,
        *,
        must_exist: bool = True,
        directory: bool | None = None,
        for_write: bool = False,
        allow_root: bool = False,
    ) -> ValidatedPath:
        self.verify_project(project)
        normalized = validate_relative_path(relative, allow_root=allow_root)
        if normalized == ".":
            identity = file_identity(project.root)
            return ValidatedPath(normalized, project.root, identity, identity)

        current = project.root
        parts = normalized.split("/")
        target_identity: FileIdentity | None = None
        for index, component in enumerate(parts):
            candidate = current / component
            is_last = index == len(parts) - 1
            if candidate.exists():
                if _is_reparse(candidate):
                    raise AgentError("unsafe_path", "Reparse-point traversal is forbidden")
                identity = file_identity(candidate)
                if is_last:
                    target_identity = identity
                current = candidate
                continue
            if not is_last or must_exist:
                raise AgentError("path_not_found", "The requested project path does not exist")
            current = candidate

        try:
            common = os.path.commonpath((str(project.root), str(current)))
        except ValueError as exc:
            raise AgentError("unsafe_path", "The path is outside the registered project") from exc
        if os.path.normcase(common) != os.path.normcase(str(project.root)):
            raise AgentError("unsafe_path", "The path is outside the registered project")
        if directory is True and (not current.exists() or not current.is_dir()):
            raise AgentError("not_a_directory", "The requested path is not a directory")
        if directory is False and current.exists() and not current.is_file():
            raise AgentError("not_a_file", "The requested path is not a regular file")
        if for_write and target_identity is not None and target_identity.link_count > 1:
            raise AgentError("unsafe_hardlink", "Mutating multiply-linked files is forbidden")
        parent = current if normalized == "." else current.parent
        return ValidatedPath(normalized, current, file_identity(parent), target_identity)

    def revalidate_for_mutation(self, project: Project, validated: ValidatedPath) -> None:
        current = self.resolve(
            project,
            validated.relative,
            must_exist=validated.target_identity is not None,
            for_write=True,
        )
        if current.parent_identity != validated.parent_identity:
            raise AgentError("path_race", "Parent directory identity changed during validation")
        if current.target_identity != validated.target_identity:
            raise AgentError("path_race", "Target identity changed during validation")

    @contextmanager
    def open_read(self, project: Project, relative: str) -> Iterator[BinaryIO]:
        validated = self.resolve(project, relative, directory=False)
        before = validated.target_identity
        try:
            stream = validated.absolute.open("rb")
        except OSError as exc:
            raise AgentError("read_failed", "The project file could not be opened") from exc
        try:
            after = file_identity(validated.absolute)
            if after != before or _is_reparse(validated.absolute):
                raise AgentError("path_race", "File identity changed while opening")
            yield stream
        finally:
            stream.close()
