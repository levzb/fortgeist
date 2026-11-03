"""Video capture process.

Architecture A (default): dxcam (DXGI Desktop Duplication) -> frame ring
buffer -> feeder thread -> ffmpeg stdin (NVENC).
  * Timestamp of each frame: t_ns = perf_counter_ns() at AcquireNextFrame
    and t_present_ns = DXGI LastPresentTime (also QPC), when the frame was shown.
  * Pause (focus/manual): frames are simply not fed, the encoder stays alive,
    the chunk is not closed (the gap is visible in t_ns). Chunks are cut by frame count.
  * The next ffmpeg is started ahead of time, so chunk switching is lossless.
  * Frames are never serialized (pickle): queues carry only slot numbers.

Architecture B: ffmpeg captures the screen itself (ddagrab -> NVENC, frames never
leave the GPU). Frame time is reconstructed: ddagrab pts (us since the first
frame on ffmpeg's monotonic clock) + an offset estimated as
min(showinfo_line_arrival_t - pts). Pause = chunk close.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

from . import formats as F
from . import shared as S
from . import winapi as W
from .encoder import FfmpegProc, build_cmd_a, build_cmd_b, probe_frames

PRESPAWN_S = 3.0  # how many seconds before chunk end to start the next ffmpeg
ROW_FLUSH_NS = 250_000_000


class _Common:
    def __init__(self, cfg, state, events_q, cmd_q, parent_pid):
        self.cfg = cfg
        self.rc = cfg.record
        self.state = state
        self.events_q = events_q
        self.cmd_q = cmd_q
        self.parent_pid = parent_pid
        self.fps = self.rc.fps
        self.interval_ns = 1_000_000_000 // self.fps
        self.chunk_frames = max(1, int(round(self.rc.chunk_minutes * 60 * self.fps)))
        self.session_dir: Path | None = None
        self.cmds: queue.Queue = queue.Queue()
        self.running = True

    def log(self, msg: str) -> None:
        self.events_q.put(("log", "capture", msg))

    def error(self, msg: str) -> None:
        self.state[S.CAPTURE_ERROR] = 1
        self.events_q.put(("error", "capture", msg))

    def _cmd_thread(self) -> None:
        watch = W.ParentWatch(self.parent_pid)
        while self.running:
            try:
                cmd = self.cmd_q.get(timeout=0.5)
            except queue.Empty:
                if not watch.alive():
                    # main died: close the chunk cleanly and exit
                    self.cmds.put(("stop",))
                    self.cmds.put(("exit",))
                    return
                continue
            self.cmds.put(cmd)
            if cmd[0] == "exit":
                return


def finalize_chunk_frames(session_dir: Path, idx: int, ffprobe: str) -> dict:
    """frames.bin -> frames.parquet, checked against the real frame count in the MKV.

    If the MKV is shorter than the table (crash: frames went into the pipe but not the file),
    the table is truncated to the number of decodable frames."""
    video = session_dir / F.chunk_video(idx)
    b_path = session_dir / F.chunk_frames_b_bin(idx)
    info: dict = {"chunk": idx}
    if b_path.exists() and not (session_dir / F.chunk_frames_bin(idx)).exists():
        rows, binfo = frames_from_b(session_dir, idx)
        info.update(binfo)
    else:
        rows = F.read_bin(session_dir / F.chunk_frames_bin(idx), F.FRAME_DTYPE)
    n_video = probe_frames(ffprobe, video) if video.exists() else 0
    info["rows"] = int(len(rows))
    info["video_frames"] = n_video
    if n_video is not None and n_video < len(rows):
        info["truncated_rows"] = int(len(rows) - n_video)
        rows = rows[:n_video]
    F.write_parquet(F.frames_to_table(rows), session_dir / F.chunk_frames_parquet(idx))
    for p in (session_dir / F.chunk_frames_bin(idx), b_path):
        if p.exists():
            p.unlink()
    return info


def frames_from_b(session_dir: Path, idx: int, fps: int | None = None) -> tuple[np.ndarray, dict]:
    raw = F.read_bin(session_dir / F.chunk_frames_b_bin(idx), F.FRAME_B_DTYPE)
    out = np.zeros(len(raw), dtype=F.FRAME_DTYPE)
    info: dict = {}
    if not len(raw):
        return out, info
    pts_ns = raw["pts_us"].astype(np.int64) * 1000
    lag = raw["arrival_ns"] - pts_ns
    offset = int(lag.min())
    info["b_offset_ns"] = offset
    info["b_arrival_lag_ms_median"] = float(np.median(lag - offset) / 1e6)
    out["frame_idx"] = raw["n"]
    out["t_ns"] = pts_ns + offset
    out["t_present_ns"] = -1
    out["accumulated_frames"] = -1
    out["is_new"] = -1
    if len(raw) > 1:
        d = np.diff(pts_ns)
        step = np.median(d) if fps is None else 1e9 / fps
        drops = np.clip(np.round(d / step) - 1, 0, None).astype(np.int32)
        out["dropped_before"][1:] = drops
    return out, info


# =========================================================================== A
class _Chunk:
    def __init__(self, idx: int, proc: FfmpegProc, frames_path: Path, video: Path):
        self.idx = idx
        self.proc = proc
        self.frames = F.AppendFile(frames_path)
        self.video = video
        self.n = 0
        self.rows = bytearray()
        self.last_row_flush = 0
        self.t_open = time.perf_counter_ns()


class CaptureA(_Common):
    def __init__(self, *a):
        super().__init__(*a)
        import dxcam

        self.cam = dxcam.create(output_idx=self.rc.output_idx, output_color="BGRA", processor_backend="numpy")
        self.width, self.height = self.cam.width, self.cam.height
        self.region = self.cam.region
        nslots = self.rc.ring_slots
        # frame ring buffer; only slot indices travel through the queues
        self.ring = np.empty((nslots, self.height, self.width, 4), dtype=np.uint8)
        self.free_q: queue.SimpleQueue = queue.SimpleQueue()
        for i in range(nslots):
            self.free_q.put(i)
        self.work_q: queue.SimpleQueue = queue.SimpleQueue()
        self.enq = 0
        self.deq = 0
        self.has_frame = False

        self.chunk: _Chunk | None = None
        self.next_chunk: _Chunk | None = None
        self.chunk_idx = 0
        self.closed_bytes = 0
        self.last_slot = -1
        self.lat_ema = 0.0
        self.fed_window: list[int] = []
        self.fed = 0
        self.dropped = 0
        self.repeats = 0
        self.finishers: list[threading.Thread] = []
        self.chunk_infos: list[dict] = []

    # --------------------------------------------------------------- chunks
    def _open_chunk(self, idx: int) -> _Chunk:
        assert self.session_dir is not None
        video = self.session_dir / F.chunk_video(idx)
        cmd = build_cmd_a(self.rc, self.width, self.height, video)
        proc = FfmpegProc(cmd, self.session_dir / "ffmpeg.log", stdin_pipe=True, stderr_pipe=False)
        ch = _Chunk(idx, proc, self.session_dir / F.chunk_frames_bin(idx), video)
        self.events_q.put(("chunk_open", idx, str(video), time.perf_counter_ns()))
        return ch

    def _close_chunk(self, ch: _Chunk, wait: bool) -> None:
        self._flush_rows(ch, force=True)
        ch.frames.close()
        session_dir = self.session_dir

        def finish():
            rc = ch.proc.finish()
            info = finalize_chunk_frames(session_dir, ch.idx, self.rc.ffprobe)
            info["ffmpeg_rc"] = rc
            try:
                info["bytes"] = ch.video.stat().st_size
            except OSError:
                info["bytes"] = 0
            self.closed_bytes += info["bytes"]
            self.chunk_infos.append(info)
            self.events_q.put(("chunk_closed", ch.idx, info))

        if wait:
            finish()
        else:
            th = threading.Thread(target=finish, daemon=False, name=f"finish-{ch.idx}")
            th.start()
            self.finishers.append(th)

    def _flush_rows(self, ch: _Chunk, force=False) -> None:
        t = time.perf_counter_ns()
        if ch.rows and (force or t - ch.last_row_flush >= ROW_FLUSH_NS):
            ch.frames.write(ch.rows)
            ch.rows = bytearray()
            ch.last_row_flush = t

    # --------------------------------------------------------------- feeder
    def _feeder(self) -> None:
        while True:
            item = self.work_q.get()
            self.deq += 1
            kind = item[0]
            if kind == "stop":
                if self.chunk is not None:
                    self._close_chunk(self.chunk, wait=True)
                    self.chunk = None
                if self.next_chunk is not None:
                    # ffmpeg started ahead of time but unused: no need for an empty chunk
                    nc = self.next_chunk
                    self.next_chunk = None
                    nc.frames.close()
                    nc.proc.kill()
                    nc.proc.finish(timeout=5)
                    for p in (nc.video, self.session_dir / F.chunk_frames_bin(nc.idx)):
                        try:
                            p.unlink()
                        except OSError:
                            pass
                for th in self.finishers:
                    th.join()
                self.finishers.clear()
                item[1].set()
                continue
            _, slot, t_ns, t_present, acc, dropped = item
            if kind == "new":
                src = self.ring[slot]
            else:  # "rep": screen did not change, repeat the last frame
                if self.last_slot < 0:
                    continue
                src = self.ring[self.last_slot]
            if self.chunk is None:
                self.chunk = self._open_chunk(self.chunk_idx)
            ch = self.chunk
            try:
                mv = memoryview(src).cast("B")
                while mv:
                    n = ch.proc.stdin.write(mv)
                    mv = mv[n:]
            except (OSError, ValueError) as e:
                self.error(f"encoder (chunk {ch.idx}) crashed: {e!r}; opening a new chunk")
                try:
                    self._close_chunk(ch, wait=False)
                except Exception:
                    pass
                self.chunk_idx += 1
                self.chunk = None
                if kind == "new":
                    self._release_slot(slot)
                continue
            t_done = time.perf_counter_ns()
            if kind == "new":
                self._release_slot(slot)
            F_ROW = F.FRAME_STRUCT.pack(ch.n, t_ns, t_present, dropped, acc, 1 if kind == "new" else 0)
            ch.rows += F_ROW
            ch.n += 1
            self.fed += 1
            self.fed_window.append(t_done)
            if kind != "new":
                self.repeats += 1
            self.lat_ema = 0.95 * self.lat_ema + 0.05 * (t_done - t_ns) / 1000
            self._flush_rows(ch)
            self._stats(t_done)
            # chunk rotation
            remaining = self.chunk_frames - ch.n
            if remaining <= 0:
                self._close_chunk(ch, wait=False)
                self.chunk_idx += 1
                self.chunk = self.next_chunk  # may be None -> opened on demand
                self.next_chunk = None
            elif remaining <= PRESPAWN_S * self.fps and self.next_chunk is None:
                self.next_chunk = self._open_chunk(self.chunk_idx + 1)

    def _release_slot(self, slot: int) -> None:
        # keep the last frame's slot so it can be repeated
        if self.last_slot >= 0:
            self.free_q.put(self.last_slot)
        self.last_slot = slot

    def _stats(self, t: int) -> None:
        st = self.state
        cut = t - 1_000_000_000
        w = self.fed_window
        while w and w[0] < cut:
            w.pop(0)
        st[S.CAPTURE_FPS_X100] = len(w) * 100
        st[S.FRAMES_FED] = self.fed
        st[S.FRAMES_REPEAT] = self.repeats
        st[S.ENC_QUEUE] = self.enq - self.deq
        st[S.QUEUE_LAT_US] = int(self.lat_ema)
        st[S.LAST_FRAME_NS] = t
        if self.chunk is not None:
            st[S.CHUNK_IDX] = self.chunk.idx
            if self.fed % self.fps == 0:
                try:
                    cur = self.chunk.video.stat().st_size
                except OSError:
                    cur = 0
                st[S.CHUNK_BYTES] = self.closed_bytes + cur

    # --------------------------------------------------------------- capture loop
    def _start_session(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.chunk_idx = 0
        self.closed_bytes = 0
        self.fed = self.dropped = self.repeats = 0
        self.fed_window = []
        self.chunk_infos = []
        st = self.state
        for k in (S.FRAMES_FED, S.FRAMES_DROPPED, S.FRAMES_REPEAT, S.ENC_QUEUE, S.CHUNK_IDX, S.CHUNK_BYTES, S.CAPTURE_ERROR):
            st[k] = 0
        # start the encoder right away so NVENC init does not eat the start of the recording
        self.chunk = self._open_chunk(0)
        self.events_q.put(("capture_started", str(session_dir), time.perf_counter_ns(),
                           {"width": self.width, "height": self.height, "architecture": "A"}))

    def _stop_session(self) -> None:
        if self.session_dir is None:
            return
        done = threading.Event()
        self.enq += 1
        self.work_q.put(("stop", done))
        done.wait()
        self.events_q.put(("capture_closed", {"frames": self.fed, "dropped": self.dropped,
                                              "repeats": self.repeats, "chunks": self.chunk_infos}))
        self.session_dir = None

    def run(self) -> None:
        threading.Thread(target=self._cmd_thread, daemon=True, name="cap-cmd").start()
        threading.Thread(target=self._feeder, daemon=True, name="cap-feeder").start()
        self.state[S.CAPTURE_READY] = 1
        self.events_q.put(("capture_ready", os.getpid(), {"width": self.width, "height": self.height}))
        st = self.state
        interval = self.interval_ns
        next_tick: int | None = None
        pending_drop = 0
        perf = time.perf_counter_ns
        grab = self.cam._grab_into
        while self.running:
            # commands
            try:
                while True:
                    cmd = self.cmds.get_nowait()
                    if cmd[0] == "start":
                        self._stop_session()
                        self._start_session(Path(cmd[1]))
                        next_tick, pending_drop = None, 0
                    elif cmd[0] == "stop":
                        self._stop_session()
                    elif cmd[0] == "exit":
                        self._stop_session()
                        self.running = False
                        break
            except queue.Empty:
                pass
            if not self.running:
                break
            if self.session_dir is None or not S.recording_active(st):
                next_tick, pending_drop = None, 0
                time.sleep(0.005)
                continue
            now = perf()
            if next_tick is None:
                next_tick = now
            elif now < next_tick:
                time.sleep((next_tick - now) / 1e9)
            now = perf()
            late = now - next_tick
            if late >= interval:  # overslept whole slots: these are drops
                k = late // interval
                pending_drop += k
                self.dropped += k
                st[S.FRAMES_DROPPED] = self.dropped
                next_tick += k * interval
            next_tick += interval
            try:
                slot = self.free_q.get_nowait()
            except queue.Empty:
                # encoder can't keep up, ring is full: honestly count a drop
                pending_drop += 1
                self.dropped += 1
                st[S.FRAMES_DROPPED] = self.dropped
                continue
            if not S.recording_active(st):
                # focus/pause changed while waiting for the tick: do not write the frame
                self.free_q.put(slot)
                next_tick, pending_drop = None, 0
                continue
            t_ns = perf()
            try:
                ok, ticks, w, h = grab(self.region, self.ring[slot])
            except Exception as e:
                self.free_q.put(slot)
                self.error(f"dxcam grab: {e!r}")
                time.sleep(0.1)
                continue
            if ok:
                self.has_frame = True
                acc = int(getattr(self.cam._duplicator, "accumulated_frames", -1))
                t_present = W.qpc_ticks_to_ns(ticks) if ticks > 0 else -1
                self.enq += 1
                self.work_q.put(("new", slot, t_ns, t_present, acc, pending_drop))
                pending_drop = 0
            else:
                self.free_q.put(slot)
                if w or h:
                    self.error(f"screen size changed: {w}x{h} (expected {self.width}x{self.height})")
                    continue
                if not self.has_frame:
                    continue  # no frame yet, wait
                self.enq += 1
                self.work_q.put(("rep", -1, t_ns, -1, 0, pending_drop))
                pending_drop = 0
        self.state[S.CAPTURE_READY] = 0
        try:
            self.cam.release()
        except Exception:
            pass


# =========================================================================== B
SHOWINFO_RE = re.compile(rb"\[Parsed_showinfo[^\]]*\]\s+n:\s*(\d+)\s+pts:\s*(-?\d+)")


class CaptureB(_Common):
    def __init__(self, *a):
        super().__init__(*a)
        info = W.screen_info()
        self.width, self.height = info["width"], info["height"]
        self.proc: FfmpegProc | None = None
        self.chunk_idx = 0
        self.frames_file: F.AppendFile | None = None
        self.n_in_chunk = 0
        self.fed = 0
        self.fed_window: list[int] = []
        self.closed_bytes = 0
        self.chunk_infos: list[dict] = []
        self.reader: threading.Thread | None = None
        self.last_pts = None
        self.dropped = 0

    def _reader(self, proc: FfmpegProc, frames: F.AppendFile, log) -> None:
        rows = bytearray()
        last_flush = time.perf_counter_ns()
        stream = proc.proc.stderr
        while True:
            line = stream.readline()
            if not line:
                break
            t = time.perf_counter_ns()
            m = SHOWINFO_RE.search(line)
            if m:
                n, pts = int(m.group(1)), int(m.group(2))
                rows += F.FRAME_B_STRUCT.pack(n, pts, t)
                self.n_in_chunk = n + 1
                self.fed += 1
                if self.last_pts is not None:
                    gap = pts - self.last_pts
                    k = int(round(gap / (1e6 / self.fps))) - 1
                    if k > 0:
                        self.dropped += k
                        self.state[S.FRAMES_DROPPED] = self.dropped
                self.last_pts = pts
                self.fed_window.append(t)
                cut = t - 1_000_000_000
                while self.fed_window and self.fed_window[0] < cut:
                    self.fed_window.pop(0)
                st = self.state
                st[S.FRAMES_FED] = self.fed
                st[S.CAPTURE_FPS_X100] = len(self.fed_window) * 100
                st[S.LAST_FRAME_NS] = t
                if t - last_flush >= ROW_FLUSH_NS:
                    frames.write(rows)
                    rows = bytearray()
                    last_flush = t
            else:
                log.write(line)
        if rows:
            frames.write(rows)

    def _open(self) -> None:
        assert self.session_dir is not None
        video = self.session_dir / F.chunk_video(self.chunk_idx)
        self.frames_file = F.AppendFile(self.session_dir / F.chunk_frames_b_bin(self.chunk_idx))
        self.proc = FfmpegProc(build_cmd_b(self.rc, video), self.session_dir / "ffmpeg.log",
                               stdin_pipe=True, stderr_pipe=True)
        self.n_in_chunk = 0
        self.last_pts = None
        self.reader = threading.Thread(target=self._reader, args=(self.proc, self.frames_file, self.proc.log), daemon=True)
        self.reader.start()
        self.state[S.CHUNK_IDX] = self.chunk_idx
        self.events_q.put(("chunk_open", self.chunk_idx, str(video), time.perf_counter_ns()))

    def _close(self) -> None:
        if self.proc is None:
            return
        rc = self.proc.finish(quit_key=True)
        if self.reader:
            self.reader.join(5)
        self.frames_file.close()
        info = finalize_chunk_frames(self.session_dir, self.chunk_idx, self.rc.ffprobe)
        info["ffmpeg_rc"] = rc
        video = self.session_dir / F.chunk_video(self.chunk_idx)
        info["bytes"] = video.stat().st_size if video.exists() else 0
        self.closed_bytes += info["bytes"]
        self.state[S.CHUNK_BYTES] = self.closed_bytes
        self.chunk_infos.append(info)
        self.events_q.put(("chunk_closed", self.chunk_idx, info))
        self.proc = None
        self.chunk_idx += 1

    def run(self) -> None:
        threading.Thread(target=self._cmd_thread, daemon=True, name="cap-cmd").start()
        self.state[S.CAPTURE_READY] = 1
        self.events_q.put(("capture_ready", os.getpid(), {"width": self.width, "height": self.height}))
        st = self.state
        while self.running:
            try:
                cmd = self.cmds.get(timeout=0.02)
                if cmd[0] == "start":
                    self._close()
                    self.session_dir = Path(cmd[1])
                    self.chunk_idx = 0
                    self.fed = self.dropped = 0
                    self.closed_bytes = 0
                    self.chunk_infos = []
                    for k in (S.FRAMES_FED, S.FRAMES_DROPPED, S.CHUNK_IDX, S.CHUNK_BYTES, S.CAPTURE_ERROR):
                        st[k] = 0
                    self.events_q.put(("capture_started", str(self.session_dir), time.perf_counter_ns(),
                                       {"width": self.width, "height": self.height, "architecture": "B"}))
                elif cmd[0] in ("stop", "exit"):
                    self._close()
                    if self.session_dir is not None:
                        self.events_q.put(("capture_closed", {"frames": self.fed, "dropped": self.dropped,
                                                              "repeats": -1, "chunks": self.chunk_infos}))
                    self.session_dir = None
                    if cmd[0] == "exit":
                        self.running = False
            except queue.Empty:
                pass
            if self.session_dir is None:
                continue
            active = S.recording_active(st)
            if active and self.proc is None:
                self._open()
            elif not active and self.proc is not None:
                self._close()
            elif self.proc is not None:
                if not self.proc.alive():
                    self.error(f"ffmpeg (B) exited, code {self.proc.proc.returncode}")
                    self._close()
                elif self.n_in_chunk >= self.chunk_frames:
                    self._close()
            if self.proc is not None and self.proc.alive() and self.fed % self.fps == 0:
                try:
                    cur = (self.session_dir / F.chunk_video(self.chunk_idx)).stat().st_size
                except OSError:
                    cur = 0
                st[S.CHUNK_BYTES] = self.closed_bytes + cur
        self.state[S.CAPTURE_READY] = 0


def capture_process_main(cfg, state, events_q, cmd_q, parent_pid: int) -> None:
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is handled by main
    try:
        W.set_dpi_aware()
        cls = CaptureA if cfg.record.architecture == "A" else CaptureB
        cls(cfg, state, events_q, cmd_q, parent_pid).run()
    except Exception as e:  # pragma: no cover
        import traceback

        events_q.put(("error", "capture", f"{e!r}\n{traceback.format_exc()}"))
        raise
