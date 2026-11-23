"""Windows 전용 도우미: 긴 경로, 절전 방지, 메모리, 드라이브 종류, 휴지통."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

IS_WIN = os.name == "nt"

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_NOT_LOCAL = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # 정션·마운트 지점
IO_REPARSE_TAG_SYMLINK = 0xA000000C
LINK_TAGS = (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)

DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE = 2, 3, 4


def long_path(p: str) -> str:
    """260자를 넘는 경로도 열 수 있게 \\\\?\\ 접두어를 붙인다(R5-B4)."""
    if not IS_WIN:
        return p
    if p.startswith("\\\\?\\"):
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def is_unc(p: str) -> bool:
    return os.path.abspath(p).startswith("\\\\")


def drive_root(p: str) -> str:
    drive, _ = os.path.splitdrive(os.path.abspath(p))
    return drive.rstrip("\\") + "\\"


if IS_WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int),
                    ("AdditionalParameters", ctypes.c_byte * 1)]

    class _SEEK_PENALTY(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                    ("IncursSeekPenalty", ctypes.c_ubyte)]

    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    _k32.SetThreadExecutionState.restype = wintypes.DWORD
    _k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]


def open_error_code(p: str) -> int | None:
    """파일 열기가 거부된 진짜 이유(Windows 오류 코드). 32=다른 프로그램이 사용 중, 5=접근 거부.

    파이썬 open()의 PermissionError에는 Windows 오류 코드가 없어, 직접 열어 보고 확인한다.
    """
    if not IS_WIN:
        return None
    h = _k32.CreateFileW(long_path(p), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return ctypes.get_last_error()
    _k32.CloseHandle(h)
    return None


def prevent_sleep(enable: bool) -> None:
    """R5-E4: 호출한 스레드가 살아 있는 동안 절전 모드 진입을 막는다."""
    if not IS_WIN:
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    _k32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0))


def memory_usage() -> tuple[int, int]:
    """(이 프로세스 사용량, 시스템 전체 메모리) 바이트. 알 수 없으면 (0, 0)."""
    if not IS_WIN:
        return 0, 0
    ms = _MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return 0, 0
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    if not _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return 0, ms.ullTotalPhys
    return pmc.WorkingSetSize, ms.ullTotalPhys


def drive_type(p: str) -> int:
    if not IS_WIN:
        return DRIVE_FIXED
    return _k32.GetDriveTypeW(drive_root(p))


def incurs_seek_penalty(p: str) -> bool | None:
    """HDD이면 True, SSD이면 False, 알 수 없으면 None (R2-4)."""
    if not IS_WIN or is_unc(p):
        return None
    drive, _ = os.path.splitdrive(os.path.abspath(p))
    h = _k32.CreateFileW("\\\\.\\" + drive, 0, 0x1 | 0x2, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        q = _STORAGE_PROPERTY_QUERY(7, 0)  # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery
        out = _SEEK_PENALTY()
        ret = wintypes.DWORD()
        ok = _k32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q),
                                  ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret), None)
        return bool(out.IncursSeekPenalty) if ok else None
    finally:
        _k32.CloseHandle(h)


def can_recycle(p: str) -> str | None:
    """휴지통으로 보낼 수 없으면 그 사유를, 가능하면 None을 돌려준다.

    휴지통이 없는 드라이브(USB, 네트워크)에서 휴지통 이동을 요청하면 Windows가
    파일을 영구 삭제하므로, 고정 디스크가 아니면 거부한다(R4-3: 영구 삭제 없음).
    """
    if not IS_WIN:
        return "Windows가 아닙니다"
    if drive_type(p) != DRIVE_FIXED:
        return "휴지통이 없는 드라이브입니다(영구 삭제 방지). 격리 폴더 이동을 사용하세요"
    if len(os.path.abspath(p)) >= 260:
        return "경로가 260자 이상이라 휴지통으로 보낼 수 없습니다. 격리 폴더 이동을 사용하세요"
    return None


def recycle(p: str) -> None:
    reason = can_recycle(p)
    if reason:
        raise OSError(reason)
    FO_DELETE = 3
    flags = 0x0004 | 0x0010 | 0x0040 | 0x0400  # SILENT|NOCONFIRMATION|ALLOWUNDO|NOERRORUI
    buf = ctypes.create_unicode_buffer(os.path.abspath(p) + "\0")  # 이중 널 종료
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, wintypes.LPCWSTR), None, flags, False, None, None)
    rc = _shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"휴지통 이동 실패(코드 {rc:#x})")
    if os.path.lexists(long_path(p)):
        raise OSError("휴지통 이동 후에도 파일이 남아 있습니다")
