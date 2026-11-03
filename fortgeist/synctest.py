"""fortgeist synctest: measures frame/input synchronization.

1. Our own fullscreen window (pygame) instantly changes to the next palette color
   on every key press / click.
2. The recorder records a session (whitelist = only the test window's pid).
3. A series of presses is sent into THIS window via SendInput at random
   intervals. SendInput goes through GuardedSynthInput: if the foreground is not the
   test window, input is not sent and the test aborts.
4. For each press: offset = t_ns(first frame with the new color) - t_ns(event).

Criteria (SPEC 1.9): offset std <= 1 frame interval; no frame reacting
before the input event.
"""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import random
import subprocess
import time
from pathlib import Path

import numpy as np

from . import formats as F
from . import winapi as W

PALETTE = [  # BGR does not matter, we compare in RGB
    (230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200),
    (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230),
]
BASE = (0, 0, 0)
KEY_SCAN = 0x25  # 'K'


def expected_color(k: int) -> tuple[int, int, int]:
    """Window color after the k-th press (k=0 = initial)."""
    return BASE if k == 0 else PALETTE[(k - 1) % len(PALETTE)]


# --------------------------------------------------------------------- window
def window_main(ready_q, stop_ev) -> None:
    W.set_dpi_aware()
    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    import pygame

    pygame.init()
    info = W.screen_info()
    size = (info["width"], info["height"])
    screen = pygame.display.set_mode(size, pygame.NOFRAME)
    pygame.display.set_caption("fortgeist synctest")
    font = pygame.font.SysFont("consolas", 22)
    k = 0

    def draw():
        screen.fill(expected_color(k))
        txt = font.render("fortgeist synctest: do not touch the mouse or keyboard (Esc to abort)", True, (128, 128, 128))
        screen.blit(txt, (20, 10))
        pygame.display.flip()

    draw()
    hwnd = pygame.display.get_wm_info()["window"]
    W.user32.SetForegroundWindow(hwnd)
    ready_q.put(("ready", os.getpid(), size))
    while not stop_ev.is_set():
        ev = pygame.event.wait(100)
        if ev.type == pygame.NOEVENT:
            continue
        if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
            ready_q.put(("abort",))
            break
        if ev.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
            k += 1
            draw()
    pygame.quit()


# --------------------------------------------------------------------- analysis
def decode_colors(ffmpeg: str, video: Path, w: int = 32, h: int = 18) -> np.ndarray:
    """Mean color of the central area of each frame (RGB)."""
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vf", f"crop=iw/2:ih/2,scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True, creationflags=0x08000000)
    a = np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3)
    return a.reshape(len(a), -1, 3).mean(axis=1)


def classify(colors: np.ndarray) -> np.ndarray:
    ref = np.array([BASE] + PALETTE, dtype=float)
    d = ((colors[:, None, :] - ref[None, :, :]) ** 2).sum(-1)
    idx = d.argmin(1)
    idx[np.sqrt(d.min(1)) > 60] = -1  # matches nothing (transition/artifact)
    return idx  # 0 = BASE, 1.. = PALETTE[i-1]


def analyze(session_dir: Path, ffmpeg: str, sends: list[dict], fps: int) -> dict:
    inputs = F.load_inputs(session_dir)
    inj = (inputs["flags"] & F.FLAG_INJECTED) != 0
    real = ~inj & ((inputs["flags"] & F.FLAG_SYNTHETIC) == 0)
    is_down = (inputs["state"] == 1) & (
        ((inputs["kind"] == F.KIND_KEY) & (inputs["code"] == KEY_SCAN))
        | ((inputs["kind"] == F.KIND_BUTTON) & (inputs["code"] == F.MOUSE_LEFT)))
    real_downs = int(((inputs["state"] == 1) & real & ((inputs["kind"] == F.KIND_KEY) | (inputs["kind"] == F.KIND_BUTTON))).sum())
    ev = inputs[is_down & inj]
    t_events = ev["t_ns"].astype(np.int64)

    frames = []
    cls = []
    for idx in F.list_chunks(session_dir):
        fr = F.load_frames(session_dir, idx)
        c = classify(decode_colors(ffmpeg, session_dir / F.chunk_video(idx)))
        n = min(len(fr), len(c))
        frames.append(fr[:n])
        cls.append(c[:n])
    fr = np.concatenate(frames)
    cl = np.concatenate(cls)
    t_cap = fr["t_ns"]
    t_pres = fr["t_present_ns"]

    rows = []
    pos = 0
    for k, te in enumerate(t_events, start=1):
        want = 1 + (k - 1) % len(PALETTE)
        hit = None
        j = pos
        while j < len(cl):
            if cl[j] == want:
                hit = j
                break
            j += 1
        if hit is None:
            rows.append({"k": k, "t_event": int(te), "frame": None})
            continue
        pos = hit
        row = {"k": k, "t_event": int(te), "frame": int(hit),
               "lag_capture_ms": (int(t_cap[hit]) - int(te)) / 1e6,
               "lag_present_ms": ((int(t_pres[hit]) - int(te)) / 1e6) if t_pres[hit] > 0 else None,
               "prev_frame_lag_ms": (int(t_cap[hit - 1]) - int(te)) / 1e6 if hit > 0 else None}
        rows.append(row)

    # input timestamp latency: SendInput call time -> t_ns recorded by the recorder
    send_t = np.array([s["t_send"] for s in sends], dtype=np.int64)
    stamp_lat = []
    for te in t_events:
        i = np.searchsorted(send_t, te, side="right") - 1
        if i >= 0:
            stamp_lat.append((int(te) - int(send_t[i])) / 1e6)

    lags = np.array([r["lag_capture_ms"] for r in rows if r.get("lag_capture_ms") is not None])
    pres = np.array([r["lag_present_ms"] for r in rows if r.get("lag_present_ms") is not None])
    interval_ms = 1000 / fps

    def stats(a):
        if not len(a):
            return None
        return {"n": int(len(a)), "median_ms": float(np.median(a)), "mean_ms": float(a.mean()),
                "std_ms": float(a.std(ddof=1)) if len(a) > 1 else 0.0, "min_ms": float(a.min()),
                "max_ms": float(a.max()), "p95_ms": float(np.percentile(a, 95))}

    s_cap = stats(lags)
    missing = sum(1 for r in rows if r["frame"] is None)
    crit_std = bool(s_cap and s_cap["std_ms"] <= interval_ms)
    crit_order = bool(s_cap and s_cap["min_ms"] >= 0)
    return {
        "fps": fps,
        "frame_interval_ms": interval_ms,
        "presses_sent": len(sends),
        "presses_recorded": int(len(t_events)),
        "reactions_found": int(len(lags)),
        "reactions_missing": missing,
        "real_user_presses_during_test": real_downs,
        "lag_capture": s_cap,
        "lag_present": stats(pres),
        "input_timestamp_latency": stats(np.array(stamp_lat)),
        "criteria": {
            "std_le_frame_interval": crit_std,
            "no_reaction_before_input": crit_order,
            "all_presses_recorded": int(len(t_events)) == len(sends),
            "no_missing_reactions": missing == 0,
            "not_contaminated": real_downs == 0,
        },
        "pass": crit_std and crit_order and missing == 0 and int(len(t_events)) == len(sends) and real_downs == 0,
        "rows": rows,
    }


def plot(report: dict, out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lags = [r["lag_capture_ms"] for r in report["rows"] if r.get("lag_capture_ms") is not None]
    pres = [r["lag_present_ms"] for r in report["rows"] if r.get("lag_present_ms") is not None]
    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    bins = np.arange(-5, max(lags + pres + [40]) + 2.5, 2.5)
    ax.hist(lags, bins=bins, alpha=0.75, label="capture t_ns - input t_ns")
    if pres:
        ax.hist(pres, bins=bins, alpha=0.6, label="t_present_ns (DXGI) - input t_ns")
    ax.axvline(0, color="k", lw=1)
    ax.axvline(report["frame_interval_ms"], color="r", lw=1, ls="--", label="1 frame interval")
    ax.set_xlabel("offset, ms")
    ax.set_ylabel("presses")
    arch = report.get("architecture", "?")
    ax.set_title(f"fortgeist synctest: arch {arch}, {report['fps']} fps, n={len(lags)}")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


# --------------------------------------------------------------------- runner
def run(cfg, n: int = 60, docs_dir: Path = Path("docs"), seed: int | None = None, log=print) -> dict:
    from .session import SYNCTEST_LATEST, Recorder

    rng = random.Random(seed)
    cfg.focus.process_whitelist = ["__synctest_only__"]  # record ONLY while the test window is in the foreground
    ctx = mp.get_context("spawn")
    ready_q = ctx.Queue()
    stop_ev = ctx.Event()
    win = ctx.Process(target=window_main, args=(ready_q, stop_ev), daemon=True)
    win.start()
    msg = ready_q.get(timeout=30)
    if msg[0] != "ready":
        raise RuntimeError("test window did not start")
    win_pid = msg[1]
    guard = W.GuardedSynthInput(win_pid)
    deadline = time.monotonic() + 5
    while W.foreground_pid()[1] != win_pid:
        if time.monotonic() > deadline:
            stop_ev.set()
            raise RuntimeError("could not bring the test window to the foreground; no input was sent")
        time.sleep(0.05)

    rec = Recorder(cfg, tag="synctest", note=f"synctest n={n}", allowed_pids=[win_pid])
    sends: list[dict] = []
    aborted = None
    try:
        rec.launch()
        session = rec.start_session()
        log(f"synctest: session {session}, window pid={win_pid}, presses {n}")
        t_end = time.monotonic() + 1.5  # encoder warm-up
        while time.monotonic() < t_end:
            rec.poll(0.05)
        for i in range(n):
            gap = rng.uniform(0.15, 0.6)
            t_end = time.monotonic() + gap
            while time.monotonic() < t_end:
                rec.poll(0.01)
            try:
                t_send = W.now_ns()
                if i % 2 == 0:
                    guard.key_scancode(KEY_SCAN, up=False)
                    time.sleep(rng.uniform(0.03, 0.08))
                    guard.key_scancode(KEY_SCAN, up=True)
                else:
                    guard.mouse(W.MOUSEEVENTF_LEFTDOWN)
                    time.sleep(rng.uniform(0.03, 0.08))
                    guard.mouse(W.MOUSEEVENTF_LEFTUP)
                sends.append({"i": i, "t_send": t_send, "kind": "key" if i % 2 == 0 else "mouse"})
            except W.ForegroundGuardError as e:
                aborted = str(e)
                break
            if not ready_q.empty() and ready_q.get_nowait()[0] == "abort":
                aborted = "aborted by user (Esc)"
                break
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            rec.poll(0.05)
        meta = rec.stop_session()
    finally:
        stop_ev.set()
        rec.shutdown()
        win.join(5)
    if aborted:
        raise RuntimeError(f"synctest aborted: {aborted}")
    report = analyze(session, cfg.record.ffmpeg, sends, cfg.record.fps)
    report.update({"session": str(session), "architecture": cfg.record.architecture,
                   "encoder": cfg.record.encoder, "date": dt.datetime.now().isoformat(timespec="seconds"),
                   "monitor": meta.get("monitor"), "drop_pct": meta["stats"]["drop_pct"]})
    docs_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{cfg.record.architecture}_{cfg.record.fps}fps"
    (docs_dir / f"synctest_{tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    plot(report, docs_dir / f"synctest_{tag}.png")
    calib = {k: report[k] for k in ("date", "architecture", "fps", "lag_capture", "lag_present",
                                     "input_timestamp_latency", "pass")}
    calib["median_offset_ms"] = report["lag_capture"]["median_ms"] if report["lag_capture"] else None
    calib["median_present_offset_ms"] = report["lag_present"]["median_ms"] if report["lag_present"] else None
    if report["pass"] and report["architecture"] == cfg.record.architecture:
        root = Path(cfg.record.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / SYNCTEST_LATEST).write_text(json.dumps(calib, indent=2), encoding="utf-8")
    return report
