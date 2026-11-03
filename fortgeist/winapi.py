"""Minimal ctypes wrappers over WinAPI.

Opens nothing in the game process: the foreground window's process name is determined
via a Toolhelp32 snapshot (no OpenProcess on the game).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("fortgeist only works on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD
)

# ---------------------------------------------------------------- constants
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_INPUT = 0x00FF
WM_USER = 0x0400
HWND_MESSAGE = wt.HWND(-3)

RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001

RI_KEY_BREAK = 1
RI_KEY_E0 = 2
RI_KEY_E1 = 4

RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
RI_MOUSE_BUTTON_4_DOWN = 0x0040
RI_MOUSE_BUTTON_4_UP = 0x0080
RI_MOUSE_BUTTON_5_DOWN = 0x0100
RI_MOUSE_BUTTON_5_UP = 0x0200
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HWHEEL = 0x0800

MOUSE_MOVE_ABSOLUTE = 0x01
MOUSE_VIRTUAL_DESKTOP = 0x02

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15
NORMAL_PRIORITY_CLASS = 0x20
BELOW_NORMAL_PRIORITY_CLASS = 0x4000

SYNCHRONIZE = 0x00100000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
WAIT_TIMEOUT = 0x102
TH32CS_SNAPPROCESS = 0x2

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


# ---------------------------------------------------------------- structures
class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
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


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


# ---------------------------------------------------------------- prototypes
def _proto(fn, res, *args):
    fn.restype = res
    fn.argtypes = list(args)
    return fn


_proto(user32.RegisterClassExW, wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_proto(user32.UnregisterClassW, wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
_proto(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
       wt.HINSTANCE, wt.LPVOID)
_proto(user32.DestroyWindow, wt.BOOL, wt.HWND)
_proto(user32.DefWindowProcW, LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32.GetMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_proto(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
_proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wt.MSG))
_proto(user32.PostMessageW, wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32.PostThreadMessageW, wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32.RegisterRawInputDevices, wt.BOOL, ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT)
_proto(user32.GetRawInputData, wt.UINT, wt.HANDLE, wt.UINT, wt.LPVOID,
       ctypes.POINTER(wt.UINT), wt.UINT)
_proto(user32.SetWinEventHook, wt.HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC,
       wt.DWORD, wt.DWORD, wt.DWORD)
_proto(user32.UnhookWinEvent, wt.BOOL, wt.HANDLE)
_proto(user32.GetForegroundWindow, wt.HWND)
_proto(user32.SetForegroundWindow, wt.BOOL, wt.HWND)
_proto(user32.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
_proto(user32.GetWindowTextW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
_proto(user32.SetTimer, ULONG_PTR, wt.HWND, ULONG_PTR, wt.UINT, wt.LPVOID)
_proto(user32.KillTimer, wt.BOOL, wt.HWND, ULONG_PTR)
_proto(user32.SendInput, wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_proto(user32.MapVirtualKeyW, wt.UINT, wt.UINT, wt.UINT)
_proto(user32.GetDC, wt.HDC, wt.HWND)
_proto(user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
_proto(gdi32.GetDeviceCaps, ctypes.c_int, wt.HDC, ctypes.c_int)

_proto(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
_proto(kernel32.GetCurrentThread, wt.HANDLE)
_proto(kernel32.GetCurrentThreadId, wt.DWORD)
_proto(kernel32.GetCurrentProcess, wt.HANDLE)
_proto(kernel32.SetThreadPriority, wt.BOOL, wt.HANDLE, ctypes.c_int)
_proto(kernel32.SetPriorityClass, wt.BOOL, wt.HANDLE, wt.DWORD)
_proto(kernel32.OpenProcess, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_proto(kernel32.CloseHandle, wt.BOOL, wt.HANDLE)
_proto(kernel32.WaitForSingleObject, wt.DWORD, wt.HANDLE, wt.DWORD)
_proto(kernel32.CreateToolhelp32Snapshot, wt.HANDLE, wt.DWORD, wt.DWORD)
_proto(kernel32.Process32FirstW, wt.BOOL, wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_proto(kernel32.Process32NextW, wt.BOOL, wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_proto(kernel32.CreateJobObjectW, wt.HANDLE, wt.LPVOID, wt.LPCWSTR)
_proto(kernel32.SetInformationJobObject, wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD)
_proto(kernel32.AssignProcessToJobObject, wt.BOOL, wt.HANDLE, wt.HANDLE)
_proto(kernel32.QueryPerformanceFrequency, wt.BOOL, ctypes.POINTER(ctypes.c_int64))
_proto(kernel32.QueryPerformanceCounter, wt.BOOL, ctypes.POINTER(ctypes.c_int64))

INVALID_HANDLE_VALUE = wt.HANDLE(-1).value


# ---------------------------------------------------------------- clock
def qpc_frequency() -> int:
    f = ctypes.c_int64()
    kernel32.QueryPerformanceFrequency(ctypes.byref(f))
    return f.value


QPC_FREQ = qpc_frequency()


def qpc_ticks_to_ns(ticks: int) -> int:
    """Converts QPC ticks to ns, the same way time.perf_counter_ns() does."""
    return ticks * 1_000_000_000 // QPC_FREQ


def now_ns() -> int:
    """The project's single clock: QPC (time.perf_counter_ns on Windows)."""
    return time.perf_counter_ns()


# ---------------------------------------------------------------- helpers
def set_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def screen_info() -> dict:
    """Resolution and refresh rate of the primary monitor."""
    set_dpi_aware()
    hdc = user32.GetDC(None)
    try:
        return {
            "width": gdi32.GetDeviceCaps(hdc, 118),  # DESKTOPHORZRES
            "height": gdi32.GetDeviceCaps(hdc, 117),  # DESKTOPVERTRES
            "refresh_hz": gdi32.GetDeviceCaps(hdc, 116),  # VREFRESH
        }
    finally:
        user32.ReleaseDC(None, hdc)


def process_names() -> dict[int, str]:
    """pid -> exe name via Toolhelp32 (without opening process handles)."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    out: dict[int, str] = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = e.szExeFile
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def foreground_pid() -> tuple[int, int]:
    """(hwnd, pid) of the current foreground window; (0, 0) if there is none (secure desktop etc.)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, 0
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(hwnd), int(pid.value)


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def set_current_thread_priority(prio: int = THREAD_PRIORITY_HIGHEST) -> bool:
    return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), prio))


def set_process_priority(pid: int | None, cls: int) -> bool:
    if pid is None or pid == os.getpid():
        return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), cls))
    h = kernel32.OpenProcess(0x0200, False, pid)  # PROCESS_SET_INFORMATION
    if not h:
        return False
    try:
        return bool(kernel32.SetPriorityClass(h, cls))
    finally:
        kernel32.CloseHandle(h)


class ParentWatch:
    """Checks whether the parent process is alive (for clean finalization if main crashes)."""

    def __init__(self, pid: int):
        self.h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)

    def alive(self) -> bool:
        if not self.h:
            return True
        return kernel32.WaitForSingleObject(self.h, 0) == WAIT_TIMEOUT


class KillOnCloseJob:
    """Job object: processes in it are killed when the last job handle closes
    (i.e. when the owner process dies). Guarantees ffmpeg is never left orphaned."""

    def __init__(self):
        self.h = kernel32.CreateJobObjectW(None, None)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            self.h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )

    def assign(self, pid: int) -> bool:
        hp = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hp:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self.h, hp))
        finally:
            kernel32.CloseHandle(hp)


def time_begin_period(ms: int = 1) -> None:
    winmm.timeBeginPeriod(ms)


# ---------------------------------------------------------------- SendInput (synctest only!)
class ForegroundGuardError(RuntimeError):
    pass


class GuardedSynthInput:
    """The only way to synthesize input in the project.

    SendInput sends input to the CURRENT foreground window, so before EVERY
    call we check that the foreground belongs to our test process
    (target_pid). Otherwise an exception is raised and no input is sent. This way input never
    goes to the game or any other foreign window.
    """

    def __init__(self, target_pid: int):
        if target_pid <= 0:
            raise ValueError("target_pid")
        self.target_pid = target_pid

    def _check(self) -> None:
        _hwnd, pid = foreground_pid()
        if pid != self.target_pid:
            raise ForegroundGuardError(
                f"foreground pid {pid} != test window {self.target_pid}: synthetic input is forbidden"
            )

    def _send(self, inp: INPUT) -> None:
        self._check()
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def key_scancode(self, scan: int, up: bool) -> None:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
        self._send(inp)

    def mouse(self, flags: int, dx: int = 0, dy: int = 0) -> None:
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        self._send(inp)
