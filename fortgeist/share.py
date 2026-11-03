"""Packs finished sessions into one .zip each, ready to send.

The zip uses no compression (the video is already compressed), so packing is
just a fast copy. It is written as `*.zip.part` and renamed when complete, so a
half-written file never looks finished. The original session folder is kept.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import zipfile
from pathlib import Path

from . import formats as F

PREFIX = "fortgeist_"


def zip_path(session_dir: Path, out_dir: Path) -> Path:
    return out_dir / f"{PREFIX}{session_dir.name}.zip"


def pack_session(session_dir: Path, out_dir: Path) -> Path:
    """Session folder -> out_dir/fortgeist_<session>.zip (files inside <session>/)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = zip_path(session_dir, out_dir)
    part = dst.with_name(dst.name + ".part")
    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for f in sorted(session_dir.iterdir()):
            if f.is_file() and f.suffix != ".bin":
                z.write(f, f"{session_dir.name}/{f.name}")
    part.replace(dst)
    return dst


def unpacked_sessions(root: Path, out_dir: Path) -> list[Path]:
    """Finalized sessions that have no zip yet (e.g. the window was closed while packing)."""
    from .session import read_meta

    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d.is_dir() and (d / F.META_JSON).exists() and not zip_path(d, out_dir).exists()
                and read_meta(d).get("status") == "finalized"):
            out.append(d)
    return out


def reveal(path: Path) -> None:
    """Opens Explorer with the file selected."""
    subprocess.Popen(f'explorer /select,"{path}"')


class Packer:
    """Packs sessions one by one in a background thread so recording is never blocked."""

    def __init__(self, out_dir: Path, log=print):
        self.out_dir = out_dir
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
                self.ready.put(pack_session(d, self.out_dir))
            except Exception as e:
                self.log(f"could not pack {d.name}: {e!r}")
            finally:
                self.current = None

    def close(self) -> None:
        """Waits until everything queued is packed."""
        self.q.put(None)
        self.t.join()
