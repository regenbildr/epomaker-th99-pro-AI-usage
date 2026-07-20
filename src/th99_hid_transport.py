"""Minimal Windows HID transport for the TH99 Pro MI_02 interface.

Importing this module has no device side effects.  A HID handle is opened only
when ``open_hid`` is called explicitly.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import platform


VID = 0x0C45
PID = 0x800A
CONFIG_INTERFACE_MARKER = "mi_02"
WINDOWS_REPORT_SIZE = 65

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def windows_libraries():
    if platform.system() != "Windows":
        raise RuntimeError("live TH99 HID access is supported only on Windows")
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(OVERLAPPED),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
    ]
    kernel32.GetOverlappedResult.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    kernel32.CancelIoEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return setupapi, hid, kernel32


def enumerate_hid_paths() -> list[str]:
    setupapi, hid, _ = windows_libraries()
    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
    device_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if device_info == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    paths: list[str] = []
    try:
        index = 0
        while True:
            interface_data = SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = ctypes.sizeof(interface_data)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                device_info,
                None,
                ctypes.byref(hid_guid),
                index,
                ctypes.byref(interface_data),
            ):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_ITEMS:
                    break
                raise ctypes.WinError(error)

            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                device_info,
                ctypes.byref(interface_data),
                None,
                0,
                ctypes.byref(required),
                None,
            )
            error = ctypes.get_last_error()
            if error != ERROR_INSUFFICIENT_BUFFER:
                raise ctypes.WinError(error)

            detail = ctypes.create_string_buffer(required.value)
            ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                device_info,
                ctypes.byref(interface_data),
                detail,
                required.value,
                None,
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info)
    return paths


def th99_config_paths(paths: list[str]) -> list[str]:
    vid_pid = f"vid_{VID:04x}&pid_{PID:04x}"
    return [
        path
        for path in paths
        if vid_pid in path.lower()
        and CONFIG_INTERFACE_MARKER in path.lower()
    ]


def open_hid(path: str):
    _, _, kernel32 = windows_libraries()
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OVERLAPPED,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle


def overlapped_io(
    kernel32,
    handle,
    payload: bytes | None,
    read_size: int,
    timeout_ms: int,
) -> bytes:
    event = kernel32.CreateEventW(None, True, False, None)
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())
    transferred = wintypes.DWORD()
    operation = OVERLAPPED()
    operation.hEvent = event
    try:
        if payload is None:
            buffer = ctypes.create_string_buffer(read_size)
            completed = kernel32.ReadFile(
                handle,
                buffer,
                read_size,
                ctypes.byref(transferred),
                ctypes.byref(operation),
            )
        else:
            buffer = ctypes.create_string_buffer(payload, len(payload))
            completed = kernel32.WriteFile(
                handle,
                buffer,
                len(payload),
                ctypes.byref(transferred),
                ctypes.byref(operation),
            )

        if not completed:
            error = ctypes.get_last_error()
            if error != ERROR_IO_PENDING:
                raise ctypes.WinError(error)
            wait_result = kernel32.WaitForSingleObject(event, timeout_ms)
            if wait_result == WAIT_TIMEOUT:
                kernel32.CancelIoEx(handle, ctypes.byref(operation))
                raise TimeoutError(f"HID operation timed out after {timeout_ms} ms")
            if wait_result != WAIT_OBJECT_0:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.GetOverlappedResult(
                handle,
                ctypes.byref(operation),
                ctypes.byref(transferred),
                False,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

        if payload is None:
            return buffer.raw[: transferred.value]
        if transferred.value != len(payload):
            raise OSError(
                f"short HID write: sent {transferred.value} of {len(payload)} bytes"
            )
        return b""
    finally:
        kernel32.CloseHandle(event)


def normalize_input_report(raw: bytes) -> bytes:
    if len(raw) >= 65 and raw[0] == 0 and raw[1] == 0x55:
        return raw[1:65]
    if len(raw) >= 64 and raw[0] == 0x55:
        return raw[:64]
    raise ValueError(f"unexpected HID input report: {raw[:16].hex(' ')}")
