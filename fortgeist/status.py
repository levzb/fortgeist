"""Status line (rich) and sound signals."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from rich.text import Text

HIGH, LOW, MID = 1200, 500, 900


class Sounds:
    """Signals play in a separate thread, since winsound.Beep blocks."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        if enabled:
            threading.Thread(target=self._run, daemon=True, name="beeper").start()

    def _run(self) -> None:
        import winsound

        while True:
            seq = self.q.get()
            for freq, ms in seq:
                if freq <= 0:
                    threading.Event().wait(ms / 1000)
                else:
                    try:
                        winsound.Beep(freq, ms)
                    except RuntimeError:
                        pass

    def play(self, seq) -> None:
        if self.enabled:
            self.q.put(seq)

    def start(self):
        self.play([(HIGH, 150)])

    def stop(self):
        self.play([(LOW, 150), (0, 60), (LOW, 150)])

    def marker(self):
        self.play([(MID, 60), (0, 50), (MID, 60)])

    def pause(self):
        self.play([(LOW, 200)])

    def resume(self):
        self.play([(HIGH, 200)])

    def error(self):
        self.play([(300, 400), (0, 100), (300, 400)])


@dataclass
class StatusData:
    mode: str  # "rec" | "paused_focus" | "paused_manual" | "stopped" | "idle"
    elapsed_s: float = 0.0
    fps: float = 0.0
    target_fps: int = 30
    drop_pct: float = 0.0
    enc_queue: int = 0
    inputs_per_s: float = 0.0
    free_gb: float = 0.0
    hours_left: float | None = None
    chunk: int = 0
    markers: int = 0
    error: str | None = None


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(d: StatusData) -> Text:
    t = Text()
    if d.mode == "rec":
        t.append("● REC ", style="bold red")
    elif d.mode == "paused_focus":
        t.append("❚❚ PAUSED (focus lost) ", style="bold yellow")
    elif d.mode == "paused_manual":
        t.append("❚❚ PAUSED (F10) ", style="bold yellow")
    elif d.mode == "idle":
        t.append("○ READY (F9 = start) ", style="bold cyan")
    else:
        t.append("■ STOPPED ", style="bold grey50")
    hours = f" (~{d.hours_left:.0f} h)" if d.hours_left is not None else ""
    t.append(
        f"{_hms(d.elapsed_s)} | {d.fps:4.1f}/{d.target_fps} fps | drop {d.drop_pct:.1f}% | "
        f"enc queue {d.enc_queue} | inputs {d.inputs_per_s:.0f}/s | "
        f"disk {d.free_gb:.0f} GB{hours} | chunk {d.chunk:03d} | markers {d.markers}"
    )
    if d.error:
        t.append("\n")
        t.append(f"⚠ {d.error}", style="bold white on red")
    return t
