"""Packs finished sessions into one .zip each, ready to send.

The zip uses no compression (the video is already compressed), so packing is
just a fast copy. With `small=True` (the default) each video chunk is first
shrunk 4x per side with area averaging (the training pipeline does exactly the
same reduction first), which makes the zip many times smaller for slow uploads.
Every frame is kept, so frame indices still match the frame tables. It is written as `*.zip.part` and renamed when complete, so a
half-written file never looks finished. The original session folder is kept.
"""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path

from . import formats as F

PREFIX = "fortgeist_"
SMALL_FACTOR = 4
SMALL_INFO = "downscaled.json"


def zip_path(session_dir: Path, out_dir: Path, small: bool = False) -> Path:
    return out_dir / f"{PREFIX}{session_dir.name}{'_small' if small else ''}.zip"


def _probe(ffprobe: str, video: Path, entries: str) -> list[str]:
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          f"stream={entries}", "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    return out.stdout.strip().split(",")


def shrink_video(src: Path, dst: Path, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    """Area-downscales by SMALL_FACTOR, keeping every frame. NVENC if available, else x264."""
    w, h, n = (int(x) for x in _probe(ffprobe, src, "width,height,nb_read_packets")[:3])
    vf = f"scale={w // SMALL_FACTOR}:{h // SMALL_FACTOR}:flags=area"
    base = [ffmpeg, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "passthrough"]
    err = ""
    for enc in (["-c:v", "hevc_nvenc", "-rc", "constqp", "-qp", "26", "-preset", "p5"],
                ["-c:v", "libx264", "-crf", "20", "-preset", "medium"]):
        r = subprocess.run(base + enc + [str(dst)], capture_output=True, text=True)
        if r.returncode == 0:
            n2 = int(_probe(ffprobe, dst, "nb_read_packets")[0])
            if n2 != n:
                raise RuntimeError(f"{src.name}: frame count changed {n} -> {n2}")
            return {"file": src.name, "source_size": [w, h], "frames": n, "encoder": enc[1]}
        err = r.stderr.strip()[-300:]
    raise RuntimeError(f"could not shrink {src.name}: {err}")


def pack_session(session_dir: Path, out_dir: Path, small: bool = False,
                 ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", log=None) -> Path:
    """Session folder -> out_dir/fortgeist_<session>[_small].zip (files inside <session>/)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = zip_path(session_dir, out_dir, small)
    part = dst.with_name(dst.name + ".part")
    files = sorted(f for f in session_dir.iterdir() if f.is_file() and f.suffix != ".bin")
    videos = [f for f in files if f.suffix == ".mkv"]
    with tempfile.TemporaryDirectory(prefix="fortgeist_") as tmp:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            info = []
            for f in files:
                src = f
                if small and f.suffix == ".mkv":
                    if log:
                        log(f"  shrinking video {videos.index(f) + 1}/{len(videos)} ...")
                    src = Path(tmp) / f.name
                    info.append(shrink_video(f, src, ffmpeg, ffprobe))
                z.write(src, f"{session_dir.name}/{f.name}")
                if src != f:
                    src.unlink()
            if small:
                z.writestr(f"{session_dir.name}/{SMALL_INFO}",
                           json.dumps({"factor": SMALL_FACTOR, "filter": "area", "chunks": info}, indent=2))
    part.replace(dst)
    return dst


def unpacked_sessions(root: Path, out_dir: Path, small: bool = False) -> list[Path]:
    """Finalized sessions that have no zip yet (e.g. the window was closed while packing)."""
    from .session import read_meta

    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d.is_dir() and (d / F.META_JSON).exists() and not zip_path(d, out_dir, small).exists()
                and read_meta(d).get("status") == "finalized"):
            out.append(d)
    return out


def reveal(path: Path) -> None:
    """Opens Explorer with the file selected."""
    subprocess.Popen(f'explorer /select,"{path}"')


class Packer:
    """Packs sessions one by one in a background thread so recording is never blocked."""

    def __init__(self, out_dir: Path, log=print, small: bool = False, ffmpeg: str = "ffmpeg",
                 ffprobe: str = "ffprobe"):
        self.out_dir = out_dir
        self.small, self.ffmpeg, self.ffprobe = small, ffmpeg, ffprobe
        self.log = log
        self.q: queue.Queue[Path | None] = queue.Queue()
        self.ready: queue.Queue[Path] = queue.Queue()
        self.current: str | None = None
        self.t = threading.Thread(target=self._run, name="packer", daemon=True)
        self.t.start()

    def add(self, session_dir: Path) -> None:
        self.q.put(session_dir)

    @property
    def busy(self) -> bool:
        return self.current is not None or not self.q.empty()

    def _run(self) -> None:
        while True:
            d = self.q.get()
            if d is None:
                return
            self.current = d.name
            try:
                self.ready.put(pack_session(d, self.out_dir, self.small, self.ffmpeg, self.ffprobe))
            except Exception as e:
                self.log(f"could not pack {d.name}: {e!r}")
            finally:
                self.current = None

    def close(self) -> None:
        """Waits until everything queued is packed."""
        self.q.put(None)
        self.t.join()
