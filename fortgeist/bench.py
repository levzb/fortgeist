"""fortgeist bench: compare recording configurations.

perf mode: for each configuration (architecture x encoder x QP x FPS) records
N seconds and measures actual FPS, % dropped, recorder CPU (+ffmpeg),
GPU encoder load (nvidia-smi), GB/hour, queue latency.
Image source: our own "test scene" (moving pattern, small
text, noise to load the encoder) or whatever is on screen (--scene none; then
it records only while the foreground process is in the config.toml whitelist).

quality mode: captures a short lossless clip and encodes it offline
with every encoder/QP, computing SSIM/PSNR at full resolution and after downscaling
to 256x256 (exactly how frames will be fed to the model).
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import multiprocessing as mp
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import formats as F
from . import shared as S
from . import winapi as W


# --------------------------------------------------------------------- scene
def scene_main(ready_q, stop_ev, fps: int = 60) -> None:
    """Test scene: motion across the whole frame + small text/icons (like a HUD)."""
    W.set_dpi_aware()
    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    import pygame

    pygame.init()
    info = W.screen_info()
    w, h = info["width"], info["height"]
    screen = pygame.display.set_mode((w, h), pygame.NOFRAME)
    small = pygame.font.SysFont("consolas", 14)
    big = pygame.font.SysFont("consolas", 40, bold=True)
    rng = np.random.default_rng(0)
    noise = pygame.surfarray.make_surface(rng.integers(0, 255, (w // 4, h // 4, 3), dtype=np.uint8))
    noise = pygame.transform.scale(noise, (w, h))
    hwnd = pygame.display.get_wm_info()["window"]
    W.user32.SetForegroundWindow(hwnd)
    ready_q.put(("ready", os.getpid()))
    clock = pygame.time.Clock()
    t = 0
    while not stop_ev.is_set():
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                stop_ev.set()
        # "camera": shifting noise background (imitates camera rotation in game)
        ox = int(t * 7) % w
        screen.blit(noise, (-ox, 0))
        screen.blit(noise, (w - ox, 0))
        for i in range(12):
            x = int((np.sin(t / 30 + i) * 0.4 + 0.5) * w)
            y = int((np.cos(t / 23 + i * 1.3) * 0.4 + 0.5) * h)
            pygame.draw.circle(screen, ((i * 40) % 255, 200, 255 - i * 15), (x, y), 40 + i * 3)
        # "HUD": small text and icons in the corners
        for r in range(6):
            screen.blit(small.render(f"AMMO {t % 30:02d}/120  HP {100 - t % 100:3d}  SHIELD {t % 50:2d}  "
                                     f"W{r} 0123456789", True, (255, 255, 255)), (20, h - 160 + r * 18))
        for i in range(5):
            pygame.draw.rect(screen, (255, 255, 255), (w // 2 - 160 + i * 66, h - 80, 60, 60), 2)
            screen.blit(small.render(str(i + 1), True, (255, 220, 0)), (w // 2 - 155 + i * 66, h - 78))
        screen.blit(big.render(f"{t:06d}", True, (255, 255, 255)), (w - 220, 20))
        pygame.display.flip()
        clock.tick(fps)
        t += 1
    pygame.quit()


class Scene:
    def __init__(self, fps: int = 60):
        ctx = mp.get_context("spawn")
        self.q = ctx.Queue()
        self.stop = ctx.Event()
        self.p = ctx.Process(target=scene_main, args=(self.q, self.stop, fps), daemon=True)

    def __enter__(self):
        self.p.start()
        msg = self.q.get(timeout=30)
        self.pid = msg[1]
        deadline = time.monotonic() + 5
        while W.foreground_pid()[1] != self.pid:
            if time.monotonic() > deadline:
                self.__exit__()
                raise RuntimeError("test scene did not come to the foreground")
            time.sleep(0.05)
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.p.join(5)
        if self.p.is_alive():
            self.p.kill()


# --------------------------------------------------------------------- sampling
class GpuSampler:
    def __init__(self):
        self.vals: list[tuple[float, float]] = []
        self.proc = None

    def start(self):
        try:
            self.proc = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=utilization.encoder,utilization.gpu", "--format=csv,noheader,nounits",
                 "-lms", "500"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=0x08000000)
        except OSError:
            self.proc = None
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            m = re.findall(r"\d+", line)
            if len(m) >= 2:
                self.vals.append((float(m[0]), float(m[1])))

    def stop(self) -> dict:
        if self.proc:
            self.proc.kill()
        if not self.vals:
            return {"enc_util_pct": None, "gpu_util_pct": None}
        a = np.array(self.vals[2:] or self.vals)
        return {"enc_util_pct": round(float(a[:, 0].mean()), 1), "gpu_util_pct": round(float(a[:, 1].mean()), 1)}


class CpuSampler:
    """CPU time of all recorder processes (main + children + ffmpeg)."""

    def __init__(self, root_pids: list[int]):
        import psutil

        self.psutil = psutil
        self.roots = root_pids
        self.last: dict[int, float] = {}
        self.first: dict[int, float] = {}
        self.running = True

    def _sample(self):
        for rp in self.roots:
            try:
                procs = [self.psutil.Process(rp)]
                procs += procs[0].children(recursive=True)
            except self.psutil.Error:
                continue
            for p in procs:
                try:
                    ct = p.cpu_times()
                    v = ct.user + ct.system
                except self.psutil.Error:
                    continue
                self.first.setdefault(p.pid, v)
                self.last[p.pid] = v

    def start(self):
        self._sample()
        self.t0 = time.perf_counter()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            self._sample()
            time.sleep(0.5)

    def stop(self) -> dict:
        self._sample()
        self.running = False
        el = time.perf_counter() - self.t0
        used = sum(self.last[p] - self.first.get(p, self.last[p]) for p in self.last)
        ncpu = os.cpu_count() or 1
        return {"cpu_cores": round(used / el, 3), "cpu_pct_total": round(100 * used / el / ncpu, 2)}


# --------------------------------------------------------------------- perf
def configs(quick: bool) -> list[dict]:
    if quick:
        return [
            {"architecture": "A", "encoder": "hevc_nvenc", "qp": 24, "fps": 30},
            {"architecture": "B", "encoder": "hevc_nvenc", "qp": 24, "fps": 30},
            {"architecture": "A", "encoder": "av1_nvenc", "qp": 28, "fps": 30},
            {"architecture": "A", "encoder": "hevc_nvenc", "qp": 24, "fps": 60},
        ]
    out = []
    for arch, enc, fps in itertools.product(["A", "B"], ["hevc_nvenc", "av1_nvenc"], [30, 60]):
        qps = [20, 24, 28] if enc == "hevc_nvenc" else [24, 28, 32]
        for qp in qps:
            out.append({"architecture": arch, "encoder": enc, "qp": qp, "fps": fps})
    return out


def run_one(cfg, conf: dict, duration: float, allowed_pids: list[int], data_root: Path, log=print) -> dict:
    from .session import Recorder

    c = copy.deepcopy(cfg)
    c.record.architecture = conf["architecture"]
    c.record.encoder = conf["encoder"]
    c.record.qp = conf["qp"]
    c.record.fps = conf["fps"]
    if "downscale" in conf:
        c.record.downscale = conf["downscale"]
    rec = Recorder(c, tag="bench", note=json.dumps(conf), allowed_pids=allowed_pids, data_root=data_root)
    rec.launch()
    lat = []
    q_max = 0
    try:
        sess = rec.start_session()
        cpu = CpuSampler([os.getpid(), rec.input_proc.pid, rec.capture_proc.pid])
        gpu = GpuSampler()
        time.sleep(0.0)
        cpu.start()
        gpu.start()
        t_end = time.monotonic() + duration
        while time.monotonic() < t_end:
            rec.poll(0.2)
            lat.append(rec.state[S.QUEUE_LAT_US])
            q_max = max(q_max, rec.state[S.ENC_QUEUE])
        cpu_r = cpu.stop()
        gpu_r = gpu.stop()
        meta = rec.stop_session()
    finally:
        rec.shutdown()
    st = meta["stats"]
    span = st["wall_span_seconds"] or duration
    frames = st["frames"]
    chunks = meta.get("chunks", [])
    b_lag = [ch.get("b_arrival_lag_ms_median") for ch in chunks if ch.get("b_arrival_lag_ms_median") is not None]
    res = {
        **conf,
        "session": str(sess),
        "fps_actual": round(frames / span, 2) if span else 0,
        "drop_pct": st["drop_pct"],
        "repeats": int(sum((F.load_frames(sess, ch["chunk"])["is_new"] == 0).sum() for ch in chunks)),
        **cpu_r,
        **gpu_r,
        "gb_per_hour": st["gb_per_hour"],
        "queue_latency_ms": round(float(np.median(lat[5:] or lat)) / 1000, 2) if conf["architecture"] == "A" else (
            round(float(np.median(b_lag)), 2) if b_lag else None),
        "enc_queue_max": q_max if conf["architecture"] == "A" else None,
        "errors": meta.get("errors", []),
    }
    log(f"  -> {res['fps_actual']} fps, drop {res['drop_pct']}%, CPU {res['cpu_pct_total']}% "
        f"({res['cpu_cores']} cores), ENC {res['enc_util_pct']}%, {res['gb_per_hour']} GB/h")
    return res


def run_perf(cfg, duration: float = 60, quick: bool = False, scene: bool = True, keep: bool = False,
             docs_dir: Path = Path("docs"), log=print) -> list[dict]:
    import shutil

    data_root = Path(cfg.record.data_dir).parent / "bench"
    results = []
    confs = configs(quick)
    ctxm = Scene() if scene else None
    try:
        allowed = []
        if ctxm:
            ctxm.__enter__()
            allowed = [ctxm.pid]
            cfg = copy.deepcopy(cfg)
            cfg.focus.process_whitelist = ["__bench_scene_only__"]
        for i, conf in enumerate(confs, 1):
            log(f"[{i}/{len(confs)}] {conf}")
            try:
                r = run_one(cfg, conf, duration, allowed, data_root, log)
            except Exception as e:  # a configuration may not work; that is a result too
                r = {**conf, "failed": repr(e)}
                log(f"  -> FAILED {e!r}")
            results.append(r)
            if not keep and r.get("session"):
                shutil.rmtree(r["session"], ignore_errors=True)
    finally:
        if ctxm:
            ctxm.__exit__()
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = {"date": dt.datetime.now().isoformat(timespec="seconds"), "duration_s": duration,
           "scene": "synthetic" if scene else "screen", "monitor": W.screen_info(), "results": results}
    (docs_dir / "bench_perf.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (docs_dir / "bench_perf.md").write_text(perf_markdown(out), encoding="utf-8")
    return results


def perf_markdown(out: dict) -> str:
    m = out["monitor"]
    lines = [f"# bench perf — {out['date']}", "",
             f"Monitor {m['width']}x{m['height']} @ {m['refresh_hz']} Hz, {out['duration_s']} s per configuration, "
             f"scene: {out['scene']}.", "",
             "| arch | encoder | QP | FPS target | FPS actual | dropped % | CPU % (cores) | ENC GPU % | GB/h | queue, ms |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in out["results"]:
        if r.get("failed"):
            lines.append(f"| {r['architecture']} | {r['encoder']} | {r['qp']} | {r['fps']} | FAILED: {r['failed'][:60]} |||||| ")
            continue
        lines.append(f"| {r['architecture']} | {r['encoder']} | {r['qp']} | {r['fps']} | {r['fps_actual']} | "
                     f"{r['drop_pct']} | {r['cpu_pct_total']} ({r['cpu_cores']}) | {r['enc_util_pct']} | "
                     f"{r['gb_per_hour']} | {r['queue_latency_ms']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- quality
def capture_reference(path: Path, seconds: float, fps: int, output_idx: int = 0) -> tuple[int, int, int]:
    """Lossless clip (FFV1, BGR0) via dxcam: reference for quality comparison."""
    import dxcam

    cam = dxcam.create(output_idx=output_idx, output_color="BGRA", processor_backend="numpy")
    w, h = cam.width, cam.height
    enc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
         "-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", "-", "-c:v", "ffv1", "-pix_fmt", "bgr0",
         str(path)], stdin=subprocess.PIPE, creationflags=0x08000000)
    buf = np.empty((h, w, 4), np.uint8)
    n = 0
    t_next = time.perf_counter()
    while n < seconds * fps:
        t_next += 1 / fps
        ok, *_ = cam._grab_into(cam.region, buf)
        enc.stdin.write(buf.tobytes())
        n += 1
        time.sleep(max(0.0, t_next - time.perf_counter()))
    enc.stdin.close()
    enc.wait()
    cam.release()
    return w, h, n


def _metric(dist: Path, ref: Path, size: str | None) -> dict:
    sc = f"scale={size}:flags=area," if size else ""
    graph = (f"[0:v]{sc}format=gbrp[a];[1:v]{sc}format=gbrp[b];[a][b]ssim;"
             f"[0:v]{sc}format=gbrp[c];[1:v]{sc}format=gbrp[d];[c][d]psnr")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(dist), "-i", str(ref), "-lavfi", graph, "-f", "null", "-"],
                       capture_output=True, text=True, creationflags=0x08000000)
    ssim = re.search(r"SSIM .*All:([\d.]+)", r.stderr)
    psnr = re.search(r"PSNR .*average:([\d.inf]+)", r.stderr)
    return {"ssim": float(ssim.group(1)) if ssim else None,
            "psnr": float(psnr.group(1)) if psnr and psnr.group(1) != "inf" else None}


def run_quality(cfg, seconds: float = 5, fps: int = 30, scene: bool = True, ref: Path | None = None,
                docs_dir: Path = Path("docs"), log=print) -> list[dict]:
    from .config import RecordConfig
    from .encoder import build_cmd_a

    work = Path(cfg.record.data_dir).parent / "bench" / "quality"
    work.mkdir(parents=True, exist_ok=True)
    if ref is None:
        ref = work / "reference.mkv"
        if scene:
            with Scene():
                time.sleep(0.5)
                w, h, n = capture_reference(ref, seconds, fps, cfg.record.output_idx)
        else:
            w, h, n = capture_reference(ref, seconds, fps, cfg.record.output_idx)
    else:
        from .encoder import probe_stream

        ps = probe_stream("ffprobe", ref)
        w, h = int(ps["width"]), int(ps["height"])
    log(f"reference: {ref} {w}x{h}")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ref), "-f", "rawvideo", "-pix_fmt", "bgra", "-"],
                         capture_output=True, check=True).stdout
    n = len(raw) // (w * h * 4)
    dur = n / fps
    results = []
    grid = [("hevc_nvenc", q) for q in (18, 20, 22, 24, 26, 28, 32)] + [("av1_nvenc", q) for q in (20, 24, 28, 32, 36)]
    for enc, qp in grid:
        rc = RecordConfig(encoder=enc, qp=qp, fps=fps)
        out = work / f"q_{enc}_{qp}.mkv"
        subprocess.run(build_cmd_a(rc, w, h, out), input=raw, capture_output=True, check=True)
        size = out.stat().st_size
        full = _metric(out, ref, None)
        small = _metric(out, ref, "256:256")
        row = {"encoder": enc, "qp": qp, "gb_per_hour": round(size / 1e9 / dur * 3600, 2),
               "ssim_full": full["ssim"], "psnr_full": full["psnr"],
               "ssim_256": small["ssim"], "psnr_256": small["psnr"]}
        log(f"  {enc} qp{qp}: {row['gb_per_hour']} GB/h, SSIM full {row['ssim_full']:.4f}, 256² {row['ssim_256']:.4f}, "
            f"PSNR 256² {row['psnr_256']:.1f} dB")
        results.append(row)
        out.unlink()
    out = {"date": dt.datetime.now().isoformat(timespec="seconds"), "reference": str(ref),
           "size": f"{w}x{h}", "frames": n, "scene": "synthetic" if scene else "screen", "results": results}
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "bench_quality.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# bench quality — {out['date']}", "", f"Reference {w}x{h}, {n} frames ({out['scene']}).", "",
             "| encoder | QP | GB/h | SSIM full | PSNR full | SSIM 256² | PSNR 256² |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['encoder']} | {r['qp']} | {r['gb_per_hour']} | {r['ssim_full']:.4f} | {r['psnr_full']:.2f} | "
                     f"{r['ssim_256']:.4f} | {r['psnr_256']:.2f} |")
    (docs_dir / "bench_quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results
