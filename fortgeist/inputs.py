"""Input capture process: Raw Input (mouse + keyboard) and focus tracking.

One thread with raised priority pumps the message queue of a message-only window:
  * WM_INPUT          - Raw Input events (RIDEV_INPUTSINK, i.e. in background too);
  * WinEventHook      - foreground window change (same clock and same queue);
  * WM_TIMER          - flush to disk (<=100 ms), focus polling, pause transitions;
  * WM_USER           - commands from main.

Input is recorded ONLY when recording_active (session running, no manual pause,
foreground process whitelisted). Everything else is dropped right in this thread.
"""

from __future__ import annotations

import collections
import ctypes
import os
import queue
import struct
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

from . import formats as F
from . import shared as S
from . import winapi as W
from .focus import FocusMatcher
from .hotkeys import HotkeyMap

HEADER = struct.Struct("<IIQQ")  # dwType, dwSize, hDevice, wParam  (24 bytes on x64)
MOUSE = struct.Struct("<H2xHHIiiI")  # usFlags, usButtonFlags, usButtonData, ulRawButtons, lLastX, lLastY, extra
KEYBOARD = struct.Struct("<HHHHII")  # MakeCode, Flags, Reserved, VKey, Message, ExtraInformation
HDR_SIZE = 24

BUTTON_TABLE = (
    (W.RI_MOUSE_LEFT_BUTTON_DOWN, W.RI_MOUSE_LEFT_BUTTON_UP, F.MOUSE_LEFT),
    (W.RI_MOUSE_RIGHT_BUTTON_DOWN, W.RI_MOUSE_RIGHT_BUTTON_UP, F.MOUSE_RIGHT),
    (W.RI_MOUSE_MIDDLE_BUTTON_DOWN, W.RI_MOUSE_MIDDLE_BUTTON_UP, F.MOUSE_MIDDLE),
    (W.RI_MOUSE_BUTTON_4_DOWN, W.RI_MOUSE_BUTTON_4_UP, F.MOUSE_X1),
    (W.RI_MOUSE_BUTTON_5_DOWN, W.RI_MOUSE_BUTTON_5_UP, F.MOUSE_X2),
)
BUTTON_MASK = 0x3FF

WM_CMD = W.WM_USER + 1
TIMER_ID = 1
TIMER_MS = 50
FLUSH_NS = 100_000_000  # flush to the OS at least every 100 ms
FSYNC_S = 1.0  # fsync at least once per second
BUF_RECORDS = 4096
CACHE_REFRESH_NS = 5_000_000_000  # pid -> exe cache refresh


class InputRecorder:
    def __init__(self, cfg, state, events_q, cmd_q, parent_pid: int, allowed_pids: set[int]):
        self.cfg = cfg
        self.state = state
        self.events_q = events_q
        self.cmd_q = cmd_q
        self.parent_pid = parent_pid
        self.hotkeys = HotkeyMap.from_config(cfg.hotkeys)
        self.hotkey_vks = self.hotkeys.vks
        self.matcher = FocusMatcher(cfg.focus.process_whitelist, allowed_pids)
        self.poll_ns = cfg.focus.poll_ms * 1_000_000

        self.buf = bytearray(BUF_RECORDS * F.INPUT_STRUCT.size)
        self.n = 0
        self.last_flush = 0
        self.file: F.AppendFile | None = None
        self.file_lock = threading.Lock()
        self.events_written = 0

        self.focus_ok = False
        self.focus_pid = -1
        self.active = False
        self.keys_down: dict[int, int] = {}  # code -> vk
        self.hotkeys_down: set[int] = set()
        self.buttons_down: set[int] = set()
        self.last_poll = 0
        self.last_cache_refresh = 0

        self.pending_cmds: collections.deque = collections.deque()
        self.hwnd = None
        self.tid = 0
        self.raw = ctypes.create_string_buffer(1024)
        self.raw_size = wt.UINT()
        self._running = True

    # ------------------------------------------------------------ output
    def _emit(self, t, device, kind, state, flags, code, vk, dx, dy, wheel):
        if self.n >= BUF_RECORDS:
            self._flush()
        F.INPUT_STRUCT.pack_into(
            self.buf, self.n * F.INPUT_STRUCT.size, t, device, kind, state, flags, code, vk, dx, dy, wheel
        )
        self.n += 1

    def _flush(self):
        if self.n and self.file is not None:
            with self.file_lock:
                self.file.write(memoryview(self.buf)[: self.n * F.INPUT_STRUCT.size])
            self.events_written += self.n
            self.state[S.INPUT_EVENTS] = self.events_written
        self.n = 0
        self.last_flush = time.perf_counter_ns()

    # ------------------------------------------------------------ activity / focus
    def _compute_active(self) -> bool:
        st = self.state
        return self.file is not None and bool(st[S.SESSION_ON]) and not st[S.MANUAL_PAUSE] and self.focus_ok

    def _update_active(self, t: int) -> bool:
        """Handles the active<->inactive transition, synthesizing releases/presses
        so the dataset has no keys "stuck" across a pause."""
        a = self._compute_active()
        if a != self.active:
            if a:
                # resume: keys already held down count as pressed from this moment
                self.active = True
                for code, vk in self.keys_down.items():
                    self._emit(t, F.DEVICE_KEYBOARD, F.KIND_KEY, 1, F.FLAG_SYNTHETIC, code, vk, 0, 0, 0)
                for b in sorted(self.buttons_down):
                    self._emit(t, F.DEVICE_MOUSE, F.KIND_BUTTON, 1, F.FLAG_SYNTHETIC, b, 0, 0, 0, 0)
            else:
                for code, vk in self.keys_down.items():
                    self._emit(t, F.DEVICE_KEYBOARD, F.KIND_KEY, 0, F.FLAG_SYNTHETIC, code, vk, 0, 0, 0)
                for b in sorted(self.buttons_down):
                    self._emit(t, F.DEVICE_MOUSE, F.KIND_BUTTON, 0, F.FLAG_SYNTHETIC, b, 0, 0, 0, 0)
                self.active = False
                self._flush()
            # exact recording interval boundaries (fortgeist info uses them to check
            # that there is not a single event outside focus/pause)
            self.events_q.put(("input_active", a, t))
        return self.active

    def _set_focus(self, fs, t: int) -> None:
        if fs.ok != self.focus_ok or fs.pid != self.focus_pid:
            changed = fs.ok != self.focus_ok
            self.focus_ok = fs.ok
            self.focus_pid = fs.pid
            self.state[S.FOCUS_OK] = 1 if fs.ok else 0
            if changed:
                self.events_q.put(("focus", fs.ok, t, fs.exe, fs.pid))
        self._update_active(t)

    def _poll_focus(self, t: int) -> None:
        self.last_poll = t
        if t - self.last_cache_refresh >= CACHE_REFRESH_NS:
            self.matcher.invalidate()  # the pid may have been reused
            self.last_cache_refresh = t
        fs = self.matcher.check_foreground()
        # take the time AFTER determining focus: the flag is set right after it,
        # so the boundary in focus.jsonl matches the moment recording stops
        self._set_focus(fs, time.perf_counter_ns())

    # ------------------------------------------------------------ raw input
    def _on_input(self, hraw) -> None:
        t = time.perf_counter_ns()
        self.raw_size.value = 1024
        got = W.user32.GetRawInputData(hraw, W.RID_INPUT, self.raw, ctypes.byref(self.raw_size), HDR_SIZE)
        if got == 0xFFFFFFFF or got < HDR_SIZE:
            return
        self._process(self.raw, t)

    def _process(self, buf, t: int) -> None:
        """Parses one RAWINPUT (separated from WinAPI for tests)."""
        dtype, _size, hdev, _wp = HEADER.unpack_from(buf, 0)
        inj = F.FLAG_INJECTED if hdev == 0 else 0
        if dtype == W.RIM_TYPEKEYBOARD:
            make, kfl, _r, vk, _msg, _ex = KEYBOARD.unpack_from(buf, HDR_SIZE)
            if make == 0xFF or vk == 0xFF:  # overrun / "fake" shift codes
                return
            down = not (kfl & W.RI_KEY_BREAK)
            flags = inj
            code = make
            if kfl & W.RI_KEY_E0:
                code |= 0xE000
                flags |= F.FLAG_KEY_E0
            elif kfl & W.RI_KEY_E1:
                code |= 0xE100
                flags |= F.FLAG_KEY_E1
            if vk in self.hotkey_vks:
                # hotkeys: never written to the dataset
                if down and vk not in self.hotkeys_down:
                    self.hotkeys_down.add(vk)
                    self.events_q.put(("hotkey", self.hotkeys.action(vk), t))
                elif not down:
                    self.hotkeys_down.discard(vk)
                return
            # handle the active transition BEFORE updating the key state, otherwise this same
            # press would be duplicated by a synthetic "already held at resume"
            active = self._update_active(t)
            was = code in self.keys_down
            if down:
                if was:
                    return  # autorepeat: record transitions only
                self.keys_down[code] = vk
            else:
                self.keys_down.pop(code, None)
            if active:
                self._emit(t, F.DEVICE_KEYBOARD, F.KIND_KEY, 1 if down else 0, flags, code, vk, 0, 0, 0)
        elif dtype == W.RIM_TYPEMOUSE:
            usf, bfl, bdata, _rawb, lx, ly, _ex = MOUSE.unpack_from(buf, HDR_SIZE)
            active = self._update_active(t)
            if lx or ly or (usf & W.MOUSE_MOVE_ABSOLUTE):
                if active:
                    mfl = inj
                    if usf & W.MOUSE_MOVE_ABSOLUTE:
                        mfl |= F.FLAG_MOUSE_ABSOLUTE
                    if usf & W.MOUSE_VIRTUAL_DESKTOP:
                        mfl |= F.FLAG_MOUSE_VIRTUAL_DESKTOP
                    self._emit(t, F.DEVICE_MOUSE, F.KIND_MOVE, F.STATE_NA, mfl, 0, 0, lx, ly, 0)
            if bfl & BUTTON_MASK:
                for dflag, uflag, code in BUTTON_TABLE:
                    if bfl & dflag:
                        self.buttons_down.add(code)
                        if active:
                            self._emit(t, F.DEVICE_MOUSE, F.KIND_BUTTON, 1, inj, code, 0, 0, 0, 0)
                    if bfl & uflag:
                        self.buttons_down.discard(code)
                        if active:
                            self._emit(t, F.DEVICE_MOUSE, F.KIND_BUTTON, 0, inj, code, 0, 0, 0, 0)
            if active and bfl & (W.RI_MOUSE_WHEEL | W.RI_MOUSE_HWHEEL):
                delta = bdata - 0x10000 if bdata >= 0x8000 else bdata
                wcode = F.WHEEL_HORIZONTAL if bfl & W.RI_MOUSE_HWHEEL else F.WHEEL_VERTICAL
                self._emit(t, F.DEVICE_MOUSE, F.KIND_WHEEL, F.STATE_NA, inj, wcode, 0, 0, 0, delta)
        if t - self.last_flush >= FLUSH_NS:
            self._flush()

    # ------------------------------------------------------------ commands
    def _on_cmd(self) -> None:
        while self.pending_cmds:
            cmd = self.pending_cmds.popleft()
            t = time.perf_counter_ns()
            if cmd[0] == "start":
                self._close_session(t)
                session_dir = Path(cmd[1])
                self.file = F.AppendFile(session_dir / F.INPUTS_BIN)
                self.events_written = 0
                self.state[S.INPUT_EVENTS] = 0
                self.matcher.invalidate()
                self._poll_focus(t)
                self.events_q.put(("input_started", str(session_dir), t))
            elif cmd[0] == "stop":
                self._close_session(t)
            elif cmd[0] == "exit":
                self._close_session(t)
                self._running = False
                W.user32.DestroyWindow(self.hwnd)

    def _close_session(self, t: int) -> None:
        if self.file is None:
            return
        # close the active interval with synthetic releases
        f = self.file
        if self.active:
            for code, vk in self.keys_down.items():
                self._emit(t, F.DEVICE_KEYBOARD, F.KIND_KEY, 0, F.FLAG_SYNTHETIC, code, vk, 0, 0, 0)
            for b in sorted(self.buttons_down):
                self._emit(t, F.DEVICE_MOUSE, F.KIND_BUTTON, 0, F.FLAG_SYNTHETIC, b, 0, 0, 0, 0)
            self.active = False
            self.events_q.put(("input_active", False, t))
        self._flush()
        with self.file_lock:
            f.close()
            self.file = None
        self.events_q.put(("input_closed", self.events_written, t))

    def _cmd_thread(self) -> None:
        watch = W.ParentWatch(self.parent_pid)
        while self._running:
            try:
                cmd = self.cmd_q.get(timeout=0.5)
            except queue.Empty:
                if not watch.alive():
                    cmd = ("exit",)
                else:
                    continue
            self.pending_cmds.append(cmd)
            W.user32.PostMessageW(self.hwnd, WM_CMD, 0, 0)
            if cmd[0] == "exit":
                return

    def _fsync_thread(self) -> None:
        while self._running:
            time.sleep(FSYNC_S)
            with self.file_lock:
                if self.file is not None:
                    self.file.fsync()

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        W.set_current_thread_priority(W.THREAD_PRIORITY_HIGHEST)
        hinst = W.kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == W.WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
            return W.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = W.WNDPROC(wndproc)
        cls_name = f"fortgeist_input_{os.getpid()}"
        wc = W.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(W.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        if not W.user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassExW")
        self.hwnd = W.user32.CreateWindowExW(0, cls_name, "fortgeist", 0, 0, 0, 0, 0, W.HWND_MESSAGE, None, hinst, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")

        devs = (W.RAWINPUTDEVICE * 2)()
        devs[0] = W.RAWINPUTDEVICE(0x01, 0x02, W.RIDEV_INPUTSINK, self.hwnd)  # mouse
        devs[1] = W.RAWINPUTDEVICE(0x01, 0x06, W.RIDEV_INPUTSINK, self.hwnd)  # keyboard
        if not W.user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(W.RAWINPUTDEVICE)):
            raise OSError(ctypes.get_last_error(), "RegisterRawInputDevices")

        def on_winevent(_hook, event, hwnd, id_obj, _id_child, _tid, _ms):
            if id_obj != 0:  # OBJID_WINDOW
                return
            # minimize: focus may formally remain, re-check the real foreground.
            # The exe name comes from the cache (process snapshot only on a miss), and the time is taken
            # after determining it, so the FOCUS_OK flag changes right after the timestamp.
            fs = self.matcher.check_foreground()
            self._set_focus(fs, time.perf_counter_ns())

        self._winevent = W.WINEVENTPROC(on_winevent)
        hook1 = W.user32.SetWinEventHook(
            W.EVENT_SYSTEM_FOREGROUND, W.EVENT_SYSTEM_FOREGROUND, None, self._winevent, 0, 0, W.WINEVENT_OUTOFCONTEXT
        )
        hook2 = W.user32.SetWinEventHook(
            W.EVENT_SYSTEM_MINIMIZESTART, W.EVENT_SYSTEM_MINIMIZEEND, None, self._winevent, 0, 0, W.WINEVENT_OUTOFCONTEXT
        )
        W.user32.SetTimer(self.hwnd, TIMER_ID, TIMER_MS, None)
        self.tid = W.kernel32.GetCurrentThreadId()

        threading.Thread(target=self._cmd_thread, daemon=True, name="input-cmd").start()
        threading.Thread(target=self._fsync_thread, daemon=True, name="input-fsync").start()

        self._poll_focus(time.perf_counter_ns())
        self.state[S.INPUT_READY] = 1
        self.events_q.put(("input_ready", os.getpid()))

        msg = wt.MSG()
        pmsg = ctypes.byref(msg)
        get = W.user32.GetMessageW
        on_input = self._on_input
        try:
            while self._running:
                r = get(pmsg, None, 0, 0)
                if r == 0 or r == -1:
                    break
                m = msg.message
                if m == W.WM_INPUT:
                    on_input(msg.lParam)
                    W.user32.DefWindowProcW(msg.hWnd, m, msg.wParam, msg.lParam)
                elif m == W.WM_TIMER:
                    t = time.perf_counter_ns()
                    if t - self.last_poll >= self.poll_ns:
                        self._poll_focus(t)
                    else:
                        self._update_active(t)
                    if t - self.last_flush >= FLUSH_NS:
                        self._flush()
                elif m == WM_CMD:
                    self._on_cmd()
                else:
                    W.user32.TranslateMessage(pmsg)
                    W.user32.DispatchMessageW(pmsg)
        finally:
            self._close_session(time.perf_counter_ns())
            W.user32.UnhookWinEvent(hook1)
            W.user32.UnhookWinEvent(hook2)
            self.state[S.INPUT_READY] = 0


def input_process_main(cfg, state, events_q, cmd_q, parent_pid: int, allowed_pids: list[int]) -> None:
    """Child process entry point (multiprocessing spawn)."""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is handled by main
    try:
        InputRecorder(cfg, state, events_q, cmd_q, parent_pid, set(allowed_pids)).run()
    except Exception as e:  # pragma: no cover - diagnostics in main
        import traceback

        events_q.put(("error", "input", f"{e!r}\n{traceback.format_exc()}"))
        raise
