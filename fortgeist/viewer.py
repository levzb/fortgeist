"""fortgeist view: view a session with an input overlay and export a clip to MP4.

The viewer timeline is "recorded time": global frame number / fps (pauses
are cut out, since no frames are written during a pause). The real time of each frame (QPC)
is shown in the status line.

Controls: Space = pause/play, ',' / '.' = frame back/forward, '[' / ']' =
speed 0.25x..2x, n / p = next/previous marker, Left/Right = -/+5 s,
click on the timeline = seek, q / Esc = exit.
"""

from __future__ import annotations

import collections
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import formats as F
from .session import read_meta

# --------------------------------------------------------------------- layout
# (scancode, label, column, row, width in "keys")
KEY_LAYOUT = [
    (0x01, "Esc", 0, 0, 1), (0x02, "1", 1, 0, 1), (0x03, "2", 2, 0, 1), (0x04, "3", 3, 0, 1),
    (0x05, "4", 4, 0, 1), (0x06, "5", 5, 0, 1), (0x07, "6", 6, 0, 1),
    (0x0F, "Tab", 0, 1, 1.5), (0x10, "Q", 1.5, 1, 1), (0x11, "W", 2.5, 1, 1), (0x12, "E", 3.5, 1, 1),
    (0x13, "R", 4.5, 1, 1), (0x14, "T", 5.5, 1, 1),
    (0x3A, "Caps", 0, 2, 1.75), (0x1E, "A", 1.75, 2, 1), (0x1F, "S", 2.75, 2, 1), (0x20, "D", 3.75, 2, 1),
    (0x21, "F", 4.75, 2, 1), (0x22, "G", 5.75, 2, 1),
    (0x2A, "Shift", 0, 3, 2.25), (0x2C, "Z", 2.25, 3, 1), (0x2D, "X", 3.25, 3, 1), (0x2E, "C", 4.25, 3, 1),
    (0x2F, "V", 5.25, 3, 1), (0x30, "B", 6.25, 3, 1),
    (0x1D, "Ctrl", 0, 4, 1.5), (0x38, "Alt", 1.5, 4, 1.25), (0x39, "Space", 2.75, 4, 4.5),
]
DISPLAY_SCANCODES = np.array([k[0] for k in KEY_LAYOUT], dtype=np.int32)


@dataclass
class FrameRef:
    chunk: int
    local: int


class Session:
    def __init__(self, session_dir: Path, offset_ms: float = 0.0):
        self.dir = session_dir
        self.meta = read_meta(session_dir)
        self.fps = (self.meta.get("recording") or {}).get("fps") or 30
        self.chunks = F.list_chunks(session_dir)
        frames = [F.load_frames(session_dir, c) for c in self.chunks]
        self.chunk_start = np.cumsum([0] + [len(f) for f in frames])  # global index of each chunk start
        self.n = int(self.chunk_start[-1])
        if self.n == 0:
            raise ValueError("session has no frames")
        self.t = np.concatenate([f["t_ns"] for f in frames]).astype(np.int64)
        self.dropped = np.concatenate([f["dropped_before"] for f in frames])
        self.inputs = F.load_inputs(session_dir)
        self.offset_ns = int(offset_ms * 1e6)
        self.markers = [m for m in F.read_jsonl(session_dir / F.MARKERS_JSONL) if m.get("type") == "marker"]
        self.marker_frames = [int(np.searchsorted(self.t, m["t_ns"])) for m in self.markers]
        self._precompute()

    def ref(self, g: int) -> FrameRef:
        c = int(np.searchsorted(self.chunk_start, g, side="right") - 1)
        return FrameRef(self.chunks[c], g - int(self.chunk_start[c]))

    def _precompute(self) -> None:
        inp = self.inputs
        t_in = inp["t_ns"] + self.offset_ns  # offset > 0: input shifted later
        t = self.t
        prev = np.concatenate([[t[0] - int(1e9 / self.fps)], t[:-1]])
        # mouse: sum of deltas over the interval (t_{i-1}, t_i]
        mv = (inp["kind"] == F.KIND_MOVE) & ((inp["flags"] & F.FLAG_MOUSE_ABSOLUTE) == 0)
        tm = t_in[mv]
        cx = np.concatenate([[0], np.cumsum(inp["dx"][mv].astype(np.int64))])
        cy = np.concatenate([[0], np.cumsum(inp["dy"][mv].astype(np.int64))])
        hi = np.searchsorted(tm, t, side="right")
        lo = np.searchsorted(tm, prev, side="right")
        self.mdx = cx[hi] - cx[lo]
        self.mdy = cy[hi] - cy[lo]
        # wheel
        wh = inp["kind"] == F.KIND_WHEEL
        tw = t_in[wh]
        cw = np.concatenate([[0], np.cumsum(inp["wheel"][wh].astype(np.int64))])
        self.wheel = cw[np.searchsorted(tw, t, side="right")] - cw[np.searchsorted(tw, prev, side="right")]
        # key and mouse button states at the frame + press "flashes" within the frame interval
        self.key_state = np.zeros((len(t), len(DISPLAY_SCANCODES)), dtype=bool)
        self.key_press = np.zeros_like(self.key_state)
        keys = inp["kind"] == F.KIND_KEY
        for j, sc in enumerate(DISPLAY_SCANCODES):
            m = keys & (inp["code"] == sc)
            self.key_state[:, j], self.key_press[:, j] = self._state_at(t_in[m], inp["state"][m], t, prev)
        self.btn_state = np.zeros((len(t), 5), dtype=bool)
        self.btn_press = np.zeros_like(self.btn_state)
        btn = inp["kind"] == F.KIND_BUTTON
        for b in range(1, 6):
            m = btn & (inp["code"] == b)
            self.btn_state[:, b - 1], self.btn_press[:, b - 1] = self._state_at(t_in[m], inp["state"][m], t, prev)
        # other keys (not on the diagram), for the text line
        other = keys & ~np.isin(inp["code"], DISPLAY_SCANCODES)
        self.other_codes = {}
        for c in np.unique(inp["code"][other]).tolist():
            m = other & (inp["code"] == c)
            self.other_codes[c] = (t_in[m], inp["state"][m])

    @staticmethod
    def _state_at(te, se, t, prev):
        if len(te) == 0:
            z = np.zeros(len(t), dtype=bool)
            return z, z
        idx = np.searchsorted(te, t, side="right") - 1
        state = np.where(idx >= 0, se[np.clip(idx, 0, None)] == 1, False)
        downs = te[se == 1]
        press = np.searchsorted(downs, t, side="right") > np.searchsorted(downs, prev, side="right")
        return state, press

    def other_keys_down(self, g: int) -> list[str]:
        t = self.t[g]
        out = []
        for c, (te, se) in self.other_codes.items():
            i = int(np.searchsorted(te, t, side="right")) - 1
            if i >= 0 and se[i] == 1:
                out.append(f"sc 0x{c:X}")
        return out


# --------------------------------------------------------------------- decoding
class Decoder:
    """Sequential chunk decoding via ffmpeg with exact seek."""

    def __init__(self, ffmpeg: str, path: Path, start: int, fps: int, size: tuple[int, int]):
        self.w, self.h = size
        self.frame_bytes = self.w * self.h * 3
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if start > 0:
            cmd += ["-ss", f"{start / fps:.6f}"]
        cmd += ["-i", str(path), "-vf", f"scale={self.w}:{self.h}:flags=area", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=self.frame_bytes * 4, creationflags=0x08000000)
        self.pos = start

    def read(self) -> np.ndarray | None:
        buf = self.proc.stdout.read(self.frame_bytes)
        if not buf or len(buf) < self.frame_bytes:
            return None
        self.pos += 1
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)

    def close(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(2)
        except OSError:
            pass


class FrameSource:
    def __init__(self, sess: Session, ffmpeg: str, size: tuple[int, int]):
        self.s = sess
        self.ffmpeg = ffmpeg
        self.size = size
        self.dec: Decoder | None = None
        self.dec_chunk = -1
        self.cache: collections.OrderedDict[int, np.ndarray] = collections.OrderedDict()

    def get(self, g: int) -> np.ndarray:
        if g in self.cache:
            return self.cache[g]
        r = self.s.ref(g)
        if self.dec is None or self.dec_chunk != r.chunk or not (self.dec.pos <= r.local <= self.dec.pos + 60):
            if self.dec:
                self.dec.close()
            self.dec = Decoder(self.ffmpeg, self.s.dir / F.chunk_video(r.chunk), r.local, self.s.fps, self.size)
            self.dec_chunk = r.chunk
        img = None
        while self.dec.pos <= r.local:
            local = self.dec.pos
            img = self.dec.read()
            if img is None:
                break
            gi = int(self.s.chunk_start[self.s.chunks.index(r.chunk)]) + local
            self.cache[gi] = img
            while len(self.cache) > 150:
                self.cache.popitem(last=False)
        if g in self.cache:
            return self.cache[g]
        return np.zeros((self.size[1], self.size[0], 3), np.uint8)

    def close(self):
        if self.dec:
            self.dec.close()


# --------------------------------------------------------------------- drawing
WHITE = (255, 255, 255)
ON = (40, 200, 255)
FLASH = (60, 60, 255)
OFF = (70, 70, 70)


def draw_overlay(img: np.ndarray, s: Session, g: int, speed: float = 1.0, playing: bool = True,
                 timeline: bool = True) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    u = max(18, int(w / 45))  # "key" size
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = u / 50
    # --- keyboard (bottom left corner)
    x0, y0 = int(u * 0.5), h - int(u * 6.4) - (int(u * 1.2) if timeline else 0)
    panel = out[max(0, y0 - u // 3): y0 + int(u * 5.3), max(0, x0 - u // 3): x0 + int(u * 7.8)]
    panel[:] = (panel * 0.45).astype(np.uint8)
    for j, (_sc, label, col, row, wk) in enumerate(KEY_LAYOUT):
        x1, y1 = int(x0 + col * u), int(y0 + row * u)
        x2, y2 = int(x1 + wk * u) - 3, y1 + u - 3
        down = s.key_state[g, j]
        press = s.key_press[g, j]
        color = FLASH if press else (ON if down else OFF)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, -1 if (down or press) else 1, cv2.LINE_AA)
        cv2.putText(out, label, (x1 + 3, y2 - u // 4), font, fs * (0.8 if len(label) > 2 else 1.0),
                    (0, 0, 0) if (down or press) else WHITE, 1, cv2.LINE_AA)
    # --- mouse (bottom right corner)
    mx, my = w - int(u * 4.5), h - int(u * 5.6) - (int(u * 1.2) if timeline else 0)
    panel = out[my - u // 3: my + int(u * 4.6), mx - int(u * 0.4): mx + int(u * 4.2)]
    panel[:] = (panel * 0.45).astype(np.uint8)
    bw = int(u * 0.9)
    for b, label, bx in ((0, "L", 0), (2, "M", 1), (1, "R", 2)):
        x1 = mx + bx * (bw + 3)
        down, press = s.btn_state[g, b], s.btn_press[g, b]
        color = FLASH if press else (ON if down else OFF)
        cv2.rectangle(out, (x1, my), (x1 + bw, my + int(u * 1.2)), color, -1 if (down or press) else 1)
        cv2.putText(out, label, (x1 + bw // 3, my + int(u * 0.85)), font, fs, (0, 0, 0) if (down or press) else WHITE, 1, cv2.LINE_AA)
    for b, label, k in ((3, "X1", 0), (4, "X2", 1)):
        down, press = s.btn_state[g, b], s.btn_press[g, b]
        if down or press:
            cv2.putText(out, label, (mx + int(u * 3.0), my + int(u * 0.5) + k * int(u * 0.6)), font, fs, FLASH if press else ON, 2)
    wv = int(s.wheel[g])
    if wv:
        cv2.putText(out, f"wheel {'+' if wv > 0 else ''}{wv // 120 if abs(wv) >= 120 else wv}",
                    (mx, my + int(u * 1.7)), font, fs, FLASH, 1, cv2.LINE_AA)
    # per-frame movement arrow (length ~ log scale)
    cxm, cym = mx + int(u * 1.6), my + int(u * 3.2)
    r = int(u * 1.2)
    cv2.circle(out, (cxm, cym), r, OFF, 1, cv2.LINE_AA)
    dx, dy = int(s.mdx[g]), int(s.mdy[g])
    mag = (dx * dx + dy * dy) ** 0.5
    if mag > 0:
        L = r * min(1.0, np.log1p(mag) / np.log1p(400))
        ex, ey = int(cxm + dx / mag * L), int(cym + dy / mag * L)
        cv2.arrowedLine(out, (cxm, cym), (ex, ey), ON, 2, cv2.LINE_AA, tipLength=0.3)
    cv2.putText(out, f"{dx:+d},{dy:+d}", (mx, cym + r + u // 2), font, fs * 0.8, WHITE, 1, cv2.LINE_AA)
    # --- status line at the top
    sec = g / s.fps
    rel_ms = (s.t[g] - s.t[0]) / 1e6
    others = s.other_keys_down(g)
    info = (f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}  frame {g}/{s.n - 1}  "
            f"chunk {s.ref(g).chunk}  t+{rel_ms / 1000:.3f}s  {speed:g}x{'' if playing else ' PAUSED'}")
    if s.dropped[g]:
        info += f"  DROP {int(s.dropped[g])}"
    if others:
        info += "  keys " + " ".join(others[:8])
    cv2.rectangle(out, (0, 0), (w, int(u * 0.9)), (0, 0, 0), -1)
    cv2.putText(out, info, (8, int(u * 0.65)), font, fs, WHITE, 1, cv2.LINE_AA)
    # --- timeline
    if timeline:
        ty = h - int(u * 0.8)
        cv2.rectangle(out, (0, ty - u // 2), (w, h), (0, 0, 0), -1)
        cv2.line(out, (10, ty), (w - 10, ty), OFF, 2)
        for c0 in s.chunk_start[1:-1]:
            x = int(10 + (w - 20) * c0 / max(1, s.n - 1))
            cv2.line(out, (x, ty - 6), (x, ty + 6), (150, 150, 150), 1)
        for mf in s.marker_frames:
            x = int(10 + (w - 20) * min(mf, s.n - 1) / max(1, s.n - 1))
            cv2.line(out, (x, ty - 10), (x, ty + 10), (0, 220, 0), 2)
        x = int(10 + (w - 20) * g / max(1, s.n - 1))
        cv2.circle(out, (x, ty), 6, ON, -1)
    return out


# --------------------------------------------------------------------- UI
def parse_ts(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


def fit_size(meta: dict, width: int) -> tuple[int, int]:
    cs = (meta.get("recording") or {}).get("capture_size") or [1920, 1080]
    ds = (meta.get("recording") or {}).get("downscale")
    if ds:
        cs = [int(x) for x in ds.split("x")]
    cw, ch = cs if cs[0] else (1920, 1080)
    w = min(width, cw)
    h = int(round(w * ch / cw / 2)) * 2
    return w - (w % 2), h


def view(session_dir: Path, ffmpeg: str = "ffmpeg", width: int = 1280, offset_ms: float = 0.0) -> None:
    s = Session(session_dir, offset_ms)
    size = fit_size(s.meta, width)
    src = FrameSource(s, ffmpeg, size)
    win = f"fortgeist view - {session_dir.name}"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    state = {"g": 0, "seek": None}

    def on_mouse(ev, x, y, _flags, _p):
        if ev == cv2.EVENT_LBUTTONDOWN and y > size[1] - 40:
            state["seek"] = int(np.clip((x - 10) / (size[0] - 20), 0, 1) * (s.n - 1))

    cv2.setMouseCallback(win, on_mouse)
    speeds = [0.25, 0.5, 1.0, 1.5, 2.0]
    si = 2
    playing = True
    g = 0
    t_last = time.perf_counter()
    try:
        while True:
            img = draw_overlay(src.get(g), s, g, speeds[si], playing)
            cv2.imshow(win, img)
            frame_dt = 1.0 / (s.fps * speeds[si])
            wait = max(1, int((t_last + frame_dt - time.perf_counter()) * 1000)) if playing else 30
            k = cv2.waitKeyEx(wait)
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                break
            if state["seek"] is not None:
                g, state["seek"] = state["seek"], None
            if k in (ord("q"), 27):
                break
            elif k == ord(" "):
                playing = not playing
            elif k == ord("."):
                playing, g = False, min(s.n - 1, g + 1)
            elif k == ord(","):
                playing, g = False, max(0, g - 1)
            elif k == ord("]"):
                si = min(len(speeds) - 1, si + 1)
            elif k == ord("["):
                si = max(0, si - 1)
            elif k == ord("n"):
                nxt = [m for m in s.marker_frames if m > g]
                if nxt:
                    g = min(nxt[0], s.n - 1)
            elif k == ord("p"):
                prv = [m for m in s.marker_frames if m < g]
                if prv:
                    g = prv[-1]
            elif k in (2555904, 0x270000):  # →
                g = min(s.n - 1, g + 5 * s.fps)
            elif k in (2424832, 0x250000):  # ←
                g = max(0, g - 5 * s.fps)
            elif playing:
                now = time.perf_counter()
                if now - t_last >= frame_dt:
                    t_last = now
                    if g < s.n - 1:
                        g += 1
                    else:
                        playing = False
    finally:
        src.close()
        cv2.destroyAllWindows()


def export(session_dir: Path, rng: str, out: Path, ffmpeg: str = "ffmpeg", width: int = 1280,
           offset_ms: float = 0.0, encoder: str = "h264_nvenc") -> int:
    s = Session(session_dir, offset_ms)
    a, b = rng.split("-")
    g0 = int(np.clip(round(parse_ts(a) * s.fps), 0, s.n - 1))
    g1 = int(np.clip(round(parse_ts(b) * s.fps), g0, s.n - 1))
    size = fit_size(s.meta, width)
    src = FrameSource(s, ffmpeg, size)
    enc = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-video_size", f"{size[0]}x{size[1]}", "-framerate", str(s.fps), "-i", "-",
         *(["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "constqp", "-qp", "20"] if encoder == "h264_nvenc"
           else ["-c:v", encoder, "-crf", "20", "-preset", "veryfast"]),
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE, creationflags=0x08000000)
    n = 0
    try:
        for g in range(g0, g1 + 1):
            enc.stdin.write(draw_overlay(src.get(g), s, g, timeline=True).tobytes())
            n += 1
    finally:
        enc.stdin.close()
        enc.wait()
        src.close()
    return n
