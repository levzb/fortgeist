"""Determines whether the foreground window belongs to a whitelisted process.

The process name comes from a Toolhelp32 snapshot; the game process handle is never opened.
For privacy, the outside world (focus.jsonl) only gets the exe name for
whitelisted processes; everything else is "(other)", window titles are never recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import winapi


@dataclass
class FocusState:
    ok: bool
    pid: int
    exe: str  # exe name if whitelisted, otherwise "(other)" / "(none)"


class FocusMatcher:
    def __init__(self, whitelist: list[str], allowed_pids: set[int] | None = None):
        self.any = "*" in whitelist
        self.names = {w.lower() for w in whitelist if w != "*"}
        self.allowed_pids = set(allowed_pids or ())
        self._cache: dict[int, str] = {}

    def _exe(self, pid: int) -> str:
        name = self._cache.get(pid)
        if name is None:
            # cache miss: refresh the whole snapshot (the pid may have been reused)
            self._cache = winapi.process_names()
            name = self._cache.get(pid, "")
        return name

    def invalidate(self) -> None:
        self._cache = {}

    def check_pid(self, pid: int) -> FocusState:
        if pid == 0:
            return FocusState(False, 0, "(none)")
        if pid in self.allowed_pids:
            return FocusState(True, pid, f"(pid {pid})")
        exe = self._exe(pid)
        ok = self.any or exe.lower() in self.names
        return FocusState(ok, pid, exe if ok else "(other)")

    def check_foreground(self) -> FocusState:
        _hwnd, pid = winapi.foreground_pid()
        return self.check_pid(pid)
