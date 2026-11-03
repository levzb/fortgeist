"""ffmpeg control (NVENC).

Architecture A: raw BGRA frames come into stdin (rawvideo), ffmpeg encodes.
Architecture B: ffmpeg captures the screen itself (ddagrab, frames never leave the GPU),
frame timestamps are reconstructed from showinfo lines (see capture.py).

Common:
  * MKV, clusters <= 1 s (a crash loses at most one second of video);
  * frame PTS = frame_idx / fps (video timeline = frame index; real time is
    in *.frames.parquet);
  * NVENC converts RGB->YUV with BT.601 limited, but tags the stream as
    gbr/pc; the tag is fixed explicitly (otherwise third-party decoders distort colors).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import winapi as W
from .config import RecordConfig, parse_size

# The actual NVENC matrix for RGB input is BT.601 limited (verified, see docs/decisions.md).
# Set the correct frame metadata before the encoder (ffmpeg overrides output -colorspace).
SETPARAMS = "setparams=colorspace=smpte170m:range=tv:color_primaries=bt709:color_trc=iec61966-2-1"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _codec_args(rc: RecordConfig, fps: int) -> list[str]:
    gop = max(1, round(rc.gop_seconds * fps))
    args = [
        "-c:v", rc.encoder,
        "-preset", rc.preset,
        "-tune", "hq",
        "-rc", "constqp",
        "-qp", str(rc.qp),
        "-g", str(gop),
    ]
    if rc.encoder != "av1_nvenc":
        args += ["-forced-idr", "1"]
    return args


def _mux_args(out: Path) -> list[str]:
    return [
        "-f", "matroska",
        "-cluster_time_limit", "1000",
        "-flush_packets", "1",
        "-y", str(out),
    ]


def build_cmd_a(rc: RecordConfig, width: int, height: int, out: Path) -> list[str]:
    """dxcam -> stdin (BGRA rawvideo) -> NVENC."""
    cmd = [rc.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats"]
    size = parse_size(rc.downscale)
    if size:
        cmd += ["-init_hw_device", "cuda=cu", "-filter_hw_device", "cu"]
    cmd += [
        "-f", "rawvideo",
        "-pix_fmt", "bgra",
        "-video_size", f"{width}x{height}",
        "-framerate", str(rc.fps),
        "-thread_queue_size", "64",
        "-i", "pipe:0",
    ]
    vf = [SETPARAMS]
    if size:
        vf += ["hwupload_cuda", f"scale_cuda={size[0]}:{size[1]}:interp_algo=lanczos"]
    cmd += ["-vf", ",".join(vf), "-fps_mode", "passthrough"]
    cmd += _codec_args(rc, rc.fps)
    cmd += _mux_args(out)
    return cmd


def build_cmd_b(rc: RecordConfig, out: Path) -> list[str]:
    """ddagrab (D3D11, GPU) -> NVENC. showinfo prints the pts of every frame to stderr."""
    size = parse_size(rc.downscale)
    chain = [f"ddagrab=output_idx={rc.output_idx}:framerate={rc.fps}:draw_mouse=0:dup_frames=1"]
    chain.append("showinfo=checksum=0")
    chain.append(SETPARAMS)
    if size:
        chain.append(f"hwmap=derive_device=cuda,scale_cuda={size[0]}:{size[1]}:interp_algo=lanczos")
    chain.append(f"setpts=N/({rc.fps}*TB)")
    cmd = [
        rc.ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
        "-f", "lavfi", "-i", ",".join(chain),
        "-fps_mode", "passthrough",
    ]
    cmd += _codec_args(rc, rc.fps)
    cmd += _mux_args(out)
    return cmd


class FfmpegProc:
    """ffmpeg in a job object (dies with its owner, never left orphaned)."""

    def __init__(self, cmd: list[str], log_path: Path, stdin_pipe: bool, stderr_pipe: bool):
        self.cmd = cmd
        self.log = open(log_path, "ab")
        self.log.write(("\n# " + subprocess.list2cmdline(cmd) + "\n").encode())
        self.log.flush()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if stderr_pipe else self.log,
            bufsize=0,
            # own process group: Ctrl+C in the console does not kill ffmpeg before
            # the recorder has cleanly closed the MKV
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        self.job = W.KillOnCloseJob()
        self.job.assign(self.proc.pid)
        self.stdin = self.proc.stdin

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def finish(self, timeout: float = 15.0, quit_key: bool = False) -> int:
        """Shuts down cleanly: EOF on stdin (A) or 'q' (B), waits for MKV finalization."""
        try:
            if quit_key:
                self.stdin.write(b"q")
                self.stdin.flush()
            self.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait(5)
        try:
            self.log.close()
        except OSError:
            pass
        return rc

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


def ffmpeg_info(rc: RecordConfig) -> dict:
    """ffmpeg version and presence of the required encoders/filters. Bypasses nothing:
    if something is missing, the caller reports it to the user."""
    exe = shutil.which(rc.ffmpeg) or rc.ffmpeg
    out: dict = {"path": exe, "version": None, "encoders": [], "filters": []}
    try:
        v = subprocess.run([rc.ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10)
        out["version"] = v.stdout.splitlines()[0] if v.stdout else None
        e = subprocess.run([rc.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
        out["encoders"] = sorted(set(re.findall(r"\b(\w+_nvenc)\b", e.stdout)))
        f = subprocess.run([rc.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10)
        out["filters"] = [n for n in ("ddagrab", "showinfo", "scale_cuda", "hwupload_cuda", "hwmap") if re.search(rf"\b{n}\b", f.stdout)]
    except (OSError, subprocess.TimeoutExpired) as ex:
        out["error"] = repr(ex)
    return out


def check_requirements(rc: RecordConfig) -> list[str]:
    info = ffmpeg_info(rc)
    problems = []
    if info.get("error") or not info["version"]:
        problems.append(f"ffmpeg not found / does not start ({rc.ffmpeg}): {info.get('error')}")
        return problems
    if rc.encoder not in info["encoders"]:
        problems.append(f"ffmpeg has no encoder {rc.encoder} (available: {info['encoders']})")
    if rc.architecture == "B":
        for flt in ("ddagrab", "showinfo"):
            if flt not in info["filters"]:
                problems.append(f"ffmpeg has no filter {flt} (needed for architecture B)")
    if rc.downscale and "scale_cuda" not in info["filters"]:
        problems.append("ffmpeg has no scale_cuda (needed for GPU downscaling)")
    return problems


def probe_frames(ffprobe: str, path: Path) -> int | None:
    """Number of decodable video packets in the file (works on a truncated MKV too)."""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=300,
        )
        s = r.stdout.strip().splitlines()
        return int(s[0]) if s and s[0].strip().isdigit() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def probe_stream(ffprobe: str, path: Path) -> dict:
    import json

    r = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,pix_fmt,color_space,color_range:format=duration,size",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        return {}
    s = (d.get("streams") or [{}])[0]
    s.update(d.get("format") or {})
    return s
