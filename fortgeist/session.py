"""Session lifecycle: processes, hotkeys, meta.json, finalization."""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import queue
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import formats as F
from . import shared as S
from . import winapi as W
from .capture import capture_process_main, finalize_chunk_frames
from .config import Config
from .inputs import input_process_main

SYNCTEST_LATEST = "synctest_latest.json"


def git_commit() -> str:
    here = Path(__file__).resolve().parent
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, timeout=5)
        commit = r.stdout.strip() or "unknown"
        d = subprocess.run(["git", "status", "--porcelain"], cwd=here, capture_output=True, text=True, timeout=5)
        return commit + ("-dirty" if d.stdout.strip() else "")
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def new_session_dir(root: Path) -> Path:
    name = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    p = root / name
    i = 1
    while p.exists():
        p = root / f"{name}_{i}"
        i += 1
    p.mkdir(parents=True)
    return p


def write_meta(session_dir: Path, meta: dict) -> None:
    tmp = session_dir / (F.META_JSON + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, session_dir / F.META_JSON)


def read_meta(session_dir: Path) -> dict:
    p = session_dir / F.META_JSON
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def disk_free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def disk_write_speed(path: Path, mb: int = 256) -> float:
    """Sequential write speed, MB/s (with fsync)."""
    path.mkdir(parents=True, exist_ok=True)
    p = path / ".fortgeist_speedtest.tmp"
    block = os.urandom(4 << 20)
    t0 = time.perf_counter()
    with open(p, "wb", buffering=0) as f:
        for _ in range(mb // 4):
            f.write(block)
        os.fsync(f.fileno())
    el = time.perf_counter() - t0
    try:
        p.unlink()
    except OSError:
        pass
    return mb / el if el > 0 else float("inf")


def load_sync_calibration(data_root: Path) -> dict | None:
    p = data_root / SYNCTEST_LATEST
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ finalize
def finalize_session(session_dir: Path, ffprobe: str = "ffprobe", extra: dict | None = None) -> dict:
    """Brings a session to its final form (idempotent; works after a crash too):
    frames.bin -> parquet (truncated to the real number of frames in the MKV),
    inputs.bin -> parquet, statistics into meta.json."""
    meta = read_meta(session_dir)
    chunks_info = {c["chunk"]: c for c in meta.get("chunks", []) if "chunk" in c}
    for idx in F.list_chunks(session_dir):
        if not (session_dir / F.chunk_frames_parquet(idx)).exists():
            info = finalize_chunk_frames(session_dir, idx, ffprobe)
            info["recovered"] = True
            chunks_info[idx] = {**chunks_info.get(idx, {}), **info}
    # inputs
    bin_path = session_dir / F.INPUTS_BIN
    if bin_path.exists():
        arr = F.read_bin(bin_path, F.INPUT_DTYPE)
        F.write_parquet(F.inputs_to_table(arr), session_dir / F.INPUTS_PARQUET)
        bin_path.unlink()
    elif not (session_dir / F.INPUTS_PARQUET).exists():
        F.write_parquet(F.inputs_to_table(F.read_bin(bin_path, F.INPUT_DTYPE)), session_dir / F.INPUTS_PARQUET)

    inputs = F.load_inputs(session_dir)
    n_frames = 0
    n_dropped = 0
    first_t = None
    last_t = None
    chunk_list = []
    total_bytes = 0
    for idx in F.list_chunks(session_dir):
        fr = F.load_frames(session_dir, idx)
        n_frames += len(fr)
        n_dropped += int(fr["dropped_before"].sum()) if len(fr) else 0
        if len(fr):
            first_t = fr["t_ns"][0] if first_t is None else min(first_t, fr["t_ns"][0])
            last_t = fr["t_ns"][-1] if last_t is None else max(last_t, fr["t_ns"][-1])
        vb = (session_dir / F.chunk_video(idx)).stat().st_size
        total_bytes += vb
        ci = chunks_info.get(idx, {"chunk": idx})
        ci.update({"frames": int(len(fr)), "bytes": vb,
                   "t_first_ns": int(fr["t_ns"][0]) if len(fr) else None,
                   "t_last_ns": int(fr["t_ns"][-1]) if len(fr) else None})
        chunk_list.append(ci)
    markers = F.read_jsonl(session_dir / F.MARKERS_JSONL)
    fps = meta.get("recording", {}).get("fps") or 30
    active_s = n_frames / fps if fps else 0
    stats = {
        "frames": n_frames,
        "dropped": n_dropped,
        "drop_pct": round(100.0 * n_dropped / max(1, n_frames + n_dropped), 3),
        "recorded_seconds": round(active_s, 2),
        "wall_span_seconds": round((last_t - first_t) / 1e9, 2) if first_t is not None else 0,
        "input_events": int(len(inputs)),
        "input_events_by_kind": {F.KIND_NAMES[k]: int((inputs["kind"] == k).sum()) for k in F.KIND_NAMES},
        "markers": sum(1 for m in markers if m.get("type") == "marker"),
        "video_bytes": total_bytes,
        "gb_per_hour": round(total_bytes / 1e9 / (active_s / 3600), 2) if active_s > 1 else None,
    }
    meta["chunks"] = chunk_list
    meta["stats"] = stats
    meta["status"] = "finalized"
    meta["finalized_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if extra:
        meta.update(extra)
    write_meta(session_dir, meta)
    return meta


def find_unfinalized(root: Path) -> list[Path]:
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / F.META_JSON).exists() and read_meta(d).get("status") != "finalized":
            out.append(d)
    return out


# ------------------------------------------------------------------ recorder
class Recorder:
    """Manages child processes and sessions. Used by CLI record,
    synctest and bench."""

    def __init__(self, cfg: Config, tag: str = "", note: str = "", allowed_pids: list[int] | None = None,
                 on_event: Callable[[tuple], None] | None = None, sounds=None, data_root: Path | None = None):
        self.cfg = cfg
        self.tag = tag
        self.note = note
        self.allowed_pids = list(allowed_pids or [])
        self.on_event = on_event
        self.sounds = sounds
        self.data_root = Path(data_root or cfg.record.data_dir)
        self.ctx = mp.get_context("spawn")
        self.state = S.create()
        self.events_q = self.ctx.Queue()
        self.input_cmd = self.ctx.Queue()
        self.capture_cmd = self.ctx.Queue()
        self.input_proc = None
        self.capture_proc = None
        self.session_dir: Path | None = None
        self.meta: dict = {}
        self.markers: F.JsonlWriter | None = None
        self.focus_log: F.JsonlWriter | None = None
        self.log_f = None
        self.focus_ok = False
        self.focus_exe = "(none)"
        self.n_markers = 0
        self.session_t0 = 0
        self.errors: list[str] = []
        self.last_error: str | None = None
        self.capture_info: dict = {}
        self.pending_hotkeys: list[tuple] = []
        self._closed: dict[str, Any] = {}

    # -------------------------------------------------------------- processes
    def launch(self, timeout: float = 30.0) -> None:
        me = os.getpid()
        self.input_proc = self.ctx.Process(
            target=input_process_main, name="fortgeist-input",
            args=(self.cfg, self.state, self.events_q, self.input_cmd, me, self.allowed_pids), daemon=True)
        self.capture_proc = self.ctx.Process(
            target=capture_process_main, name="fortgeist-capture",
            args=(self.cfg, self.state, self.events_q, self.capture_cmd, me), daemon=True)
        self.input_proc.start()
        self.capture_proc.start()
        self._wait_for({"input_ready", "capture_ready"}, timeout)

    def shutdown(self) -> None:
        if self.session_dir is not None:
            self.stop_session()
        for q_ in (self.input_cmd, self.capture_cmd):
            try:
                q_.put(("exit",))
            except (OSError, ValueError):
                pass
        for p in (self.input_proc, self.capture_proc):
            if p is not None:
                p.join(10)
                if p.is_alive():
                    p.kill()

    def _wait_for(self, kinds: set[str], timeout: float) -> dict[str, tuple]:
        need = set(kinds)
        got: dict[str, tuple] = {}
        deadline = time.monotonic() + timeout
        while need:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"did not get events {need}; errors: {self.errors[-3:]}")
            for p in (self.input_proc, self.capture_proc):
                if p is not None and not p.is_alive() and need:
                    self._drain()
                    raise RuntimeError(f"process {p.name} exited (code {p.exitcode}); errors: {self.errors[-3:]}")
            try:
                ev = self.events_q.get(timeout=min(0.2, left))
            except queue.Empty:
                continue
            self._handle(ev)
            if ev[0] in need:
                need.discard(ev[0])
                got[ev[0]] = ev
        return got

    def _drain(self) -> None:
        while True:
            try:
                self._handle(self.events_q.get_nowait())
            except queue.Empty:
                return

    # -------------------------------------------------------------- logging
    def _log(self, msg: str) -> None:
        line = f"{dt.datetime.now().isoformat(timespec='milliseconds')} {msg}\n"
        if self.log_f:
            self.log_f.write(line)
            self.log_f.flush()

    def _marker(self, obj: dict) -> None:
        if self.markers:
            self.markers.write(obj)

    # -------------------------------------------------------------- events
    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "hotkey":
            self.pending_hotkeys.append(ev)
        elif kind == "focus":
            _, ok, t, exe, pid = ev
            self.focus_ok, self.focus_exe = ok, exe
            if self.focus_log:
                self.focus_log.write({"t_ns": t, "event": "focus_gained" if ok else "focus_lost", "exe": exe})
            self._log(f"focus {'gained' if ok else 'lost'} exe={exe}")
        elif kind == "input_active":
            if self.focus_log:
                self.focus_log.write({"t_ns": ev[2], "event": "input_active" if ev[1] else "input_inactive"})
        elif kind == "error":
            self.errors.append(f"{ev[1]}: {ev[2]}")
            self.last_error = f"{ev[1]}: {ev[2].splitlines()[0][:120]}"
            self._log(f"ERROR {ev[1]}: {ev[2]}")
            self._marker({"t_ns": W.now_ns(), "type": "error", "source": ev[1], "msg": ev[2][:500]})
            if self.sounds:
                self.sounds.error()
        elif kind == "log":
            self._log(f"{ev[1]}: {ev[2]}")
        elif kind == "chunk_open":
            self._marker({"t_ns": ev[3], "type": "chunk_open", "chunk": ev[1]})
            self._log(f"chunk {ev[1]} open")
        elif kind == "chunk_closed":
            info = ev[2]
            self._marker({"t_ns": W.now_ns(), "type": "chunk_closed", "chunk": ev[1]})
            self._log(f"chunk {ev[1]} closed {info}")
        elif kind in ("capture_ready", "capture_started"):
            self.capture_info.update(ev[-1])
        elif kind in ("input_closed", "capture_closed"):
            self._closed[kind] = ev
        if self.on_event:
            self.on_event(ev)

    def poll(self, timeout: float = 0.1) -> list[tuple]:
        """Process events; return unhandled hotkey presses."""
        try:
            ev = self.events_q.get(timeout=timeout)
            self._handle(ev)
            self._drain()
        except queue.Empty:
            pass
        for p in (self.input_proc, self.capture_proc):
            if p is not None and not p.is_alive():
                raise RuntimeError(f"process {p.name} exited unexpectedly (code {p.exitcode}): {self.errors[-1:]}")
        hk, self.pending_hotkeys = self.pending_hotkeys, []
        return hk

    def handle_hotkey(self, action: str, t: int) -> None:
        if action == "start_stop":
            if self.session_dir is None:
                self.start_session(t)
            else:
                self.stop_session(t)
        elif self.session_dir is None:
            return
        elif action == "marker":
            self.add_marker(t)  # marker = moment F8 was pressed (Raw Input time)
        elif action == "pause":
            self.toggle_pause(t)

    # -------------------------------------------------------------- session control
    def start_session(self, t: int | None = None) -> Path:
        assert self.session_dir is None
        t = t or W.now_ns()
        d = new_session_dir(self.data_root)
        self.session_dir = d
        self.log_f = open(d / F.RECORDER_LOG, "a", encoding="utf-8")
        self.markers = F.JsonlWriter(d / F.MARKERS_JSONL)
        self.focus_log = F.JsonlWriter(d / F.FOCUS_JSONL)
        self.n_markers = 0
        self.errors = []
        self.last_error = None
        self._closed = {}
        rc = self.cfg.record
        scr = W.screen_info()
        self.meta = {
            "fortgeist_version": __version__,
            "git_commit": git_commit(),
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "recording",
            "clock": {"source": "QueryPerformanceCounter (time.perf_counter_ns)", "qpc_frequency_hz": W.QPC_FREQ,
                      "session_start_t_ns": t},
            "monitor": {"output_idx": rc.output_idx, **scr},
            "recording": {
                "architecture": rc.architecture, "fps": rc.fps, "encoder": rc.encoder, "rc": "constqp",
                "qp": rc.qp, "preset": rc.preset, "gop_seconds": rc.gop_seconds,
                "downscale": rc.downscale or None, "chunk_minutes": rc.chunk_minutes,
                "color": "yuv420p bt601(smpte170m) limited range, sRGB transfer",
                "capture_size": None,
            },
            "player": self.cfg.player.__dict__.copy(),
            "focus": {"process_whitelist": self.cfg.focus.process_whitelist,
                      "extra_allowed_pids": self.allowed_pids},
            "hotkeys": {"policy": "excluded_from_inputs; logged in markers.jsonl",
                        **self.cfg.hotkeys.__dict__},
            "sync_calibration": load_sync_calibration(self.data_root),
            "tag": self.tag,
            "note": self.note,
        }
        # game bindings and settings from files on disk (read-only; an error does not stop recording)
        try:
            from .game_settings import read_game_settings, summary

            gs = read_game_settings()
            (d / F.GAME_SETTINGS_JSON).write_text(json.dumps(gs, ensure_ascii=False, indent=1), encoding="utf-8")
            self.meta["game_settings"] = summary(gs)
        except Exception as e:  # pragma: no cover
            self.meta["game_settings"] = {"error": repr(e)}
        write_meta(d, self.meta)
        self._log(f"session start {d} meta={json.dumps(self.meta['recording'])}")
        self.state[S.MANUAL_PAUSE] = 0
        self.state[S.SESSION_GEN] += 1
        self.input_cmd.put(("start", str(d)))
        self.capture_cmd.put(("start", str(d)))
        self._wait_for({"input_started", "capture_started"}, 20)
        self.meta["recording"]["capture_size"] = [self.capture_info.get("width"), self.capture_info.get("height")]
        write_meta(d, self.meta)
        self.session_t0 = W.now_ns()
        self.state[S.SESSION_ON] = 1
        self._marker({"t_ns": self.session_t0, "type": "session_start", "focus_ok": self.focus_ok,
                      "focus_exe": self.focus_exe})
        self.focus_log.write({"t_ns": self.session_t0, "event": "session_start",
                              "focus_ok": self.focus_ok, "exe": self.focus_exe})
        if self.sounds:
            self.sounds.start()
        return d

    def stop_session(self, t: int | None = None) -> dict:
        assert self.session_dir is not None
        self.state[S.SESSION_ON] = 0
        t_stop = W.now_ns()
        self._marker({"t_ns": t_stop, "type": "session_stop", "hotkey_t_ns": t})
        self.focus_log.write({"t_ns": t_stop, "event": "session_stop"})
        self.input_cmd.put(("stop",))
        self.capture_cmd.put(("stop",))
        try:
            self._wait_for({"input_closed", "capture_closed"} - set(self._closed), 120)
        except (TimeoutError, RuntimeError) as e:
            self._log(f"stop: {e!r}")
        cap = self._closed.get("capture_closed", (None, {}))[1]
        d = self.session_dir
        self.meta["chunks"] = cap.get("chunks", [])
        self.meta["errors"] = self.errors
        write_meta(d, self.meta)
        for w in (self.markers, self.focus_log):
            if w:
                w.close()
        self.markers = self.focus_log = None
        meta = finalize_session(d, self.cfg.record.ffprobe)
        self._log(f"session finalized stats={meta.get('stats')}")
        if self.log_f:
            self.log_f.close()
            self.log_f = None
        self.session_dir = None
        if self.sounds:
            self.sounds.stop()
        self.last_finalized = d
        return meta

    def add_marker(self, t: int | None = None, label: str = "") -> None:
        t = t or W.now_ns()
        self.n_markers += 1
        self._marker({"t_ns": t, "type": "marker", "n": self.n_markers, "label": label})
        if self.sounds:
            self.sounds.marker()

    def toggle_pause(self, t_hotkey: int | None = None) -> bool:
        # interval boundary: pause = moment AFTER setting the flag, resume =
        # BEFORE clearing it (so the "no input during pause" check gives no false positives)
        paused = not self.state[S.MANUAL_PAUSE]
        if paused:
            self.state[S.MANUAL_PAUSE] = 1
            t = W.now_ns()
        else:
            t = W.now_ns()
            self.state[S.MANUAL_PAUSE] = 0
        ev = "manual_pause" if paused else "manual_resume"
        self.focus_log.write({"t_ns": t, "event": ev, "hotkey_t_ns": t_hotkey})
        self._marker({"t_ns": t, "type": ev, "hotkey_t_ns": t_hotkey})
        if self.sounds:
            (self.sounds.pause if paused else self.sounds.resume)()
        return paused

    # -------------------------------------------------------------- status
    def mode(self) -> str:
        if self.session_dir is None:
            return "idle"
        if self.state[S.MANUAL_PAUSE]:
            return "paused_manual"
        if not self.state[S.FOCUS_OK]:
            return "paused_focus"
        return "rec"
