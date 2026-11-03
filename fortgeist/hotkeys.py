"""Hotkeys.

Convention (see README): hotkey presses are NOT written to inputs.parquet
(neither down nor up). Instead every trigger is written to markers.jsonl
(type = "hotkey"). They work globally, regardless of focus, so F9 can
be pressed from inside the game.
"""

from __future__ import annotations

from dataclasses import dataclass

_NAMED = {
    "ESC": 0x1B, "ESCAPE": 0x1B, "TAB": 0x09, "SPACE": 0x20, "ENTER": 0x0D,
    "BACKSPACE": 0x08, "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "PAUSE": 0x13, "SCROLLLOCK": 0x91,
    "PRINTSCREEN": 0x2C, "NUMLOCK": 0x90, "CAPSLOCK": 0x14,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
}


def vk_from_name(name: str) -> int:
    n = name.strip().upper()
    if n.startswith("F") and n[1:].isdigit():
        k = int(n[1:])
        if 1 <= k <= 24:
            return 0x70 + k - 1
    if len(n) == 1 and (n.isalpha() or n.isdigit()):
        return ord(n)
    if n in _NAMED:
        return _NAMED[n]
    raise ValueError(f"unknown hotkey: {name!r}")


@dataclass(frozen=True)
class HotkeyMap:
    start_stop: int
    marker: int
    pause: int

    @classmethod
    def from_config(cls, hk) -> "HotkeyMap":
        m = cls(vk_from_name(hk.start_stop), vk_from_name(hk.marker), vk_from_name(hk.pause))
        if len({m.start_stop, m.marker, m.pause}) != 3:
            raise ValueError("hotkeys must be different")
        return m

    def action(self, vk: int) -> str | None:
        if vk == self.start_stop:
            return "start_stop"
        if vk == self.marker:
            return "marker"
        if vk == self.pause:
            return "pause"
        return None

    @property
    def vks(self) -> frozenset[int]:
        return frozenset((self.start_stop, self.marker, self.pause))
