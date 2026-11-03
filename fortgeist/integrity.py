"""Session integrity check (fortgeist info)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import formats as F
from .encoder import probe_frames, probe_stream
from .session import read_meta


def active_intervals(focus_events: list[dict]) -> list[tuple[int, int]]:
    """Intervals when the input process actually recorded input (input_active..input_inactive)."""
    ev = sorted((e for e in focus_events if e.get("event") in ("input_active", "input_inactive")),
                key=lambda e: e["t_ns"])
    out = []
    start = None
    for e in ev:
        if e["event"] == "input_active":
            if start is None:
                start = e["t_ns"]
        elif start is not None:
            out.append((start, e["t_ns"]))
            start = None
    if start is not None:  # session ended by a crash
        out.append((start, np.iinfo(np.int64).max))
    return out


def focus_lost_intervals(focus_events: list[dict]) -> list[tuple[int, int]]:
    """Intervals when the foreground process was not whitelisted or a manual pause was on."""
    ev = sorted(focus_events, key=lambda e: e["t_ns"])
    out = []
    focus_ok = None
    paused = False
    bad_since = None
    end_t = None
    for e in ev:
        name = e.get("event")
        if name == "session_start":
            focus_ok = bool(e.get("focus_ok"))
        elif name == "focus_gained":
            focus_ok = True
        elif name == "focus_lost":
            focus_ok = False
        elif name == "manual_pause":
            paused = True
        elif name == "manual_resume":
            paused = False
        elif name == "session_stop":
            end_t = e["t_ns"]
        else:
            continue
        bad = (focus_ok is False) or paused
        if bad and bad_since is None:
            bad_since = e["t_ns"]
        elif not bad and bad_since is not None:
            out.append((bad_since, e["t_ns"]))
            bad_since = None
    if bad_since is not None:
        out.append((bad_since, end_t if end_t is not None else np.iinfo(np.int64).max))
    return out


def _outside(t: np.ndarray, intervals: list[tuple[int, int]]) -> np.ndarray:
    inside = np.zeros(len(t), dtype=bool)
    for a, b in intervals:
        inside |= (t >= a) & (t <= b)
    return ~inside


def _inside(t: np.ndarray, intervals: list[tuple[int, int]]) -> np.ndarray:
    return ~_outside(t, intervals) if intervals else np.zeros(len(t), dtype=bool)


def check_session(session_dir: Path, ffprobe: str = "ffprobe", deep: bool = True) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    meta = read_meta(session_dir)
    if not meta:
        problems.append("no meta.json")
    elif meta.get("status") != "finalized":
        problems.append("session not finalized (crash?): run `fortgeist repair`")
    fps = (meta.get("recording") or {}).get("fps") or 30
    interval = 1e9 / fps

    chunks = F.list_chunks(session_dir)
    if not chunks:
        problems.append("no chunk_*.mkv at all")
    frames_all = []
    chunk_rows = []
    prev_last = None
    for idx in chunks:
        video = session_dir / F.chunk_video(idx)
        fr = F.load_frames(session_dir, idx)
        has_pq = (session_dir / F.chunk_frames_parquet(idx)).exists()
        if not has_pq:
            problems.append(f"chunk {idx}: no frames.parquet")
        row = {"chunk": idx, "frames": int(len(fr)), "bytes": video.stat().st_size}
        if deep:
            n_video = probe_frames(ffprobe, video)
            row["video_frames"] = n_video
            if n_video is None:
                problems.append(f"chunk {idx}: MKV is unreadable")
            elif n_video != len(fr):
                problems.append(f"chunk {idx}: frames in MKV {n_video} != rows in table {len(fr)}")
            ps = probe_stream(ffprobe, video)
            row["codec"] = ps.get("codec_name")
            row["size"] = f"{ps.get('width')}x{ps.get('height')}"
        if len(fr):
            if not np.array_equal(fr["frame_idx"], np.arange(len(fr))):
                problems.append(f"chunk {idx}: frame_idx is not 0..n-1")
            d = np.diff(fr["t_ns"])
            if (d <= 0).any():
                problems.append(f"chunk {idx}: frame t_ns not strictly increasing ({int((d <= 0).sum())} violations)")
            if prev_last is not None and fr["t_ns"][0] <= prev_last:
                problems.append(f"chunk {idx}: starts before the end of the previous chunk")
            prev_last = fr["t_ns"][-1]
            row["dropped"] = int(fr["dropped_before"].sum())
            row["repeats"] = int((fr["is_new"] == 0).sum())
            tp = fr["t_present_ns"]
            ok_tp = tp > 0
            if ok_tp.any() and (tp[ok_tp] > fr["t_ns"][ok_tp] + 1_000_000).any():
                problems.append(f"chunk {idx}: t_present_ns later than capture t_ns, clocks do not match")
            frames_all.append(fr)
        chunk_rows.append(row)

    inputs = F.load_inputs(session_dir)
    focus_ev = F.read_jsonl(session_dir / F.FOCUS_JSONL)
    markers = F.read_jsonl(session_dir / F.MARKERS_JSONL)
    in_t = inputs["t_ns"]
    if len(in_t) and (np.diff(in_t) < 0).any():
        problems.append(f"input t_ns not monotonic ({int((np.diff(in_t) < 0).sum())} violations)")

    # input outside recording intervals / inside focus-loss intervals
    act = active_intervals(focus_ev)
    lost = focus_lost_intervals(focus_ev)
    n_out = int(_outside(in_t, act).sum()) if len(in_t) else 0
    if len(in_t) and not act:
        problems.append("focus.jsonl has no input_active intervals, but there is input")
    elif n_out:
        problems.append(f"{n_out} input events outside recording intervals")
    real = (inputs["flags"] & F.FLAG_SYNTHETIC) == 0
    n_lost = int(_inside(in_t[real], lost).sum()) if len(in_t) else 0
    if n_lost:
        problems.append(f"{n_lost} input events during focus loss / manual pause")
    for a, b in act:
        if lost and any(la < b and a < lb for la, lb in lost):
            # overlap is allowed only at the boundary (delay delivering the pause flag from main)
            overlap = max(min(b, lb) - max(a, la) for la, lb in lost if la < b and a < lb)
            if overlap > 50_000_000:
                problems.append(f"recording interval overlaps focus loss by {overlap / 1e6:.0f} ms")

    # frames during focus loss (tolerance 1 frame interval: the flag is read once per tick)
    if frames_all:
        ft = np.concatenate([f["t_ns"] for f in frames_all])
        shrunk = [(a + int(interval) + 1, b) for a, b in lost if b - a > interval]
        n_bad_frames = int(_inside(ft, shrunk).sum())
        if n_bad_frames:
            problems.append(f"{n_bad_frames} frames recorded during focus loss / pause")
        # one clock: input and frames in the same time range
        if len(in_t):
            span = (ft.min() - 60e9, ft.max() + 60e9)
            if in_t.min() < span[0] or in_t.max() > span[1]:
                problems.append("input and frame timestamps are in different ranges: different clocks?")
    else:
        ft = np.zeros(0, np.int64)

    n_frames = int(sum(len(f) for f in frames_all))
    dropped = int(sum(int(f["dropped_before"].sum()) for f in frames_all))
    recorded_s = n_frames / fps
    if n_frames and dropped / (n_frames + dropped) > 0.01:
        warnings.append(f"dropped {dropped} frames ({100 * dropped / (n_frames + dropped):.2f}%)")
    by_kind = {F.KIND_NAMES[k]: int((inputs["kind"] == k).sum()) for k in F.KIND_NAMES}
    return {
        "session": str(session_dir),
        "status": meta.get("status"),
        "tag": meta.get("tag"),
        "note": meta.get("note"),
        "recording": meta.get("recording"),
        "recorded_seconds": round(recorded_s, 2),
        "frames": n_frames,
        "dropped": dropped,
        "chunks": chunk_rows,
        "input_events": int(len(inputs)),
        "input_by_kind": by_kind,
        "input_synthetic": int((~real).sum()),
        "input_injected": int(((inputs["flags"] & F.FLAG_INJECTED) != 0).sum()),
        "markers": [m for m in markers if m.get("type") == "marker"],
        "active_intervals": len(act),
        "focus_lost_intervals": len(lost),
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }
