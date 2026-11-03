"""fortgeist CLI: record / view / info / repair / config / synctest / bench."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _cfg(args):
    from .config import load_config, validate

    cfg = load_config(args.config)
    r = cfg.record
    for name in ("architecture", "fps", "encoder", "qp", "downscale"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(r, name, v)
    validate(cfg)
    return cfg


def _resolve_session(cfg, s: str | None) -> Path:
    root = Path(cfg.record.data_dir)
    if s in (None, "last", "latest"):
        dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []
        if not dirs:
            raise SystemExit(f"no sessions in {root}")
        return dirs[-1]
    p = Path(s)
    if p.exists():
        return p
    if (root / s).exists():
        return root / s
    raise SystemExit(f"session not found: {s}")


# ------------------------------------------------------------------ record
def cmd_record(args) -> int:
    from rich.console import Console
    from rich.live import Live

    from . import shared as S
    from . import winapi as W
    from .encoder import check_requirements
    from .session import Recorder, disk_free_bytes, disk_write_speed, finalize_session, find_unfinalized
    from .status import Sounds, StatusData, render

    con = Console()
    cfg = _cfg(args)
    rc = cfg.record
    problems = check_requirements(rc)
    if problems:
        for p in problems:
            con.print(f"[bold red]✗[/] {p}")
        return 2
    root = Path(rc.data_dir)
    for d in find_unfinalized(root):
        con.print(f"[yellow]Found unfinished session {d.name}, finalizing…[/]")
        finalize_session(d, rc.ffprobe, {"recovered_after_crash": True})
    if "*" in cfg.focus.process_whitelist:
        con.print("[bold red]WARNING: focus.process_whitelist = '*': input is recorded in ANY window.[/]")

    free = disk_free_bytes(root)
    est_gbph = args.est_gbph
    speed = disk_write_speed(root, 128)
    hours = free / 1e9 / est_gbph
    con.print(f"Disk: {free / 1e9:.0f} GB free (~{hours:.0f} h at ~{est_gbph} GB/h), write {speed:.0f} MB/s")
    if hours < rc.min_free_hours:
        con.print(f"[bold red]Low disk space: less than {rc.min_free_hours} h of recording![/]")
    if speed < est_gbph * 1e3 / 3600 * 5:
        con.print("[bold yellow]Disk is slow relative to the bitrate: frames may be lost.[/]")

    sounds = Sounds(cfg.sound.enabled)
    rec = Recorder(cfg, tag=args.tag, note=args.note, sounds=sounds, allowed_pids=args.allow_pid)
    con.print(f"Starting: architecture {rc.architecture}, {rc.encoder} qp{rc.qp}, {rc.fps} fps. "
              f"{cfg.hotkeys.start_stop}: start/stop, {cfg.hotkeys.marker}: marker, "
              f"{cfg.hotkeys.pause}: pause, Ctrl+C: exit.")
    rec.launch()
    if args.autostart:
        rec.start_session()
    last_inputs, last_t = 0, time.monotonic()
    ips = 0.0
    free_gb = free / 1e9
    last_disk = 0.0
    try:
        with Live(render(StatusData(mode="idle")), console=con, refresh_per_second=4, transient=False) as live:
            while True:
                for _, action, t in rec.poll(0.1):
                    if action == "start_stop" and rec.session_dir is not None:
                        d = rec.session_dir
                        meta = rec.stop_session(t)
                        live.console.print(f"■ session {d.name}: {meta['stats']['frames']} frames, "
                                           f"{meta['stats']['input_events']} events, drop {meta['stats']['drop_pct']}%")
                        if args.exit_after_stop:
                            return 0
                    else:
                        rec.handle_hotkey(action, t)
                st = rec.state
                now = time.monotonic()
                if now - last_t >= 1.0:
                    ips = (st[S.INPUT_EVENTS] - last_inputs) / (now - last_t)
                    last_inputs, last_t = st[S.INPUT_EVENTS], now
                if now - last_disk > 5:
                    free_gb = disk_free_bytes(root) / 1e9
                    last_disk = now
                fed, dropped = st[S.FRAMES_FED], st[S.FRAMES_DROPPED]
                el = (W.now_ns() - rec.session_t0) / 1e9 if rec.session_dir else 0.0
                rec_s = fed / rc.fps
                gbph = (st[S.CHUNK_BYTES] / 1e9) / (rec_s / 3600) if rec_s > 30 and st[S.CHUNK_BYTES] else est_gbph
                live.update(render(StatusData(
                    mode=rec.mode(), elapsed_s=el,
                    fps=st[S.CAPTURE_FPS_X100] / 100 if rec.mode() == "rec" else 0.0,
                    target_fps=rc.fps, drop_pct=100 * dropped / max(1, fed + dropped),
                    enc_queue=st[S.ENC_QUEUE], inputs_per_s=ips, free_gb=free_gb,
                    hours_left=free_gb / gbph if gbph else None, chunk=st[S.CHUNK_IDX],
                    markers=rec.n_markers, error=rec.last_error)))
                if args.duration and rec.session_dir is not None and el >= args.duration:
                    rec.stop_session()
                    return 0
    except KeyboardInterrupt:
        con.print("Ctrl+C: finishing…")
    finally:
        rec.shutdown()
    return 0


# ------------------------------------------------------------------ info / repair
def cmd_info(args) -> int:
    from .integrity import check_session

    cfg = _cfg(args)
    d = _resolve_session(cfg, args.session)
    rep = check_session(d, cfg.record.ffprobe, deep=not args.fast)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return 0 if rep["ok"] else 1
    r = rep["recording"] or {}
    print(f"Session:  {d}")
    print(f"Status:   {rep['status']}   tag: {rep['tag']!r}   note: {rep['note']!r}")
    print(f"Record:   arch {r.get('architecture')}, {r.get('encoder')} qp{r.get('qp')}, {r.get('fps')} fps, "
          f"{r.get('capture_size')}")
    m, s = divmod(rep["recorded_seconds"], 60)
    print(f"Length:   {int(m)}:{s:05.2f} of recorded video, frames {rep['frames']}, dropped {rep['dropped']}")
    for c in rep["chunks"]:
        print(f"  chunk {c['chunk']:03d}: {c['frames']} frames (in MKV {c.get('video_frames')}), "
              f"{c['bytes'] / 1e6:.1f} MB, {c.get('codec')} {c.get('size')}")
    print(f"Input:    {rep['input_events']} events {rep['input_by_kind']}, synthetic {rep['input_synthetic']}, "
          f"injected {rep['input_injected']}")
    print(f"Focus:    recording intervals {rep['active_intervals']}, focus losses/pauses {rep['focus_lost_intervals']}")
    print(f"Markers:  {len(rep['markers'])}")
    for w in rep["warnings"]:
        print(f"  ⚠ {w}")
    if rep["ok"]:
        print("Integrity: OK")
    else:
        print("Integrity: PROBLEMS")
        for p in rep["problems"]:
            print(f"  ✗ {p}")
    return 0 if rep["ok"] else 1


def cmd_repair(args) -> int:
    from .session import finalize_session, find_unfinalized

    cfg = _cfg(args)
    targets = [_resolve_session(cfg, args.session)] if args.session else find_unfinalized(Path(cfg.record.data_dir))
    if not targets:
        print("nothing to repair")
    for d in targets:
        meta = finalize_session(d, cfg.record.ffprobe, {"recovered_after_crash": True})
        print(f"{d.name}: finalized, {meta['stats']}")
    return 0


# ------------------------------------------------------------------ view
def cmd_view(args) -> int:
    from .viewer import export, view

    cfg = _cfg(args)
    d = _resolve_session(cfg, args.session)
    if args.export:
        rng, out = args.export
        n = export(d, rng, Path(out), cfg.record.ffmpeg, args.width, args.offset_ms)
        print(f"exported {n} frames -> {out}")
        return 0
    view(d, cfg.record.ffmpeg, args.width, args.offset_ms)
    return 0


# ------------------------------------------------------------------ synctest / bench
def _reports(args) -> str:
    from .config import APP_HOME

    args.docs = args.docs or str(APP_HOME / "reports")
    return args.docs


def cmd_synctest(args) -> int:
    from . import synctest

    _reports(args)
    cfg = _cfg(args)
    cfg.sound.enabled = False
    print("synctest: a fullscreen test window will open. Do not touch the mouse or keyboard for ~1 min.")
    rep = synctest.run(cfg, n=args.n, docs_dir=Path(args.docs), seed=args.seed)
    lc = rep["lag_capture"] or {}
    lp = rep["lag_present"] or {}
    li = rep["input_timestamp_latency"] or {}
    print(f"presses: sent {rep['presses_sent']}, recorded {rep['presses_recorded']}, "
          f"reactions found {rep['reactions_found']}")
    print(f"offset (capture frame - input): median {lc.get('median_ms', 0):.1f} ms, std {lc.get('std_ms', 0):.1f} ms, "
          f"min {lc.get('min_ms', 0):.1f}, max {lc.get('max_ms', 0):.1f}  (frame interval {rep['frame_interval_ms']:.1f} ms)")
    if lp:
        print(f"offset (DXGI present - input): median {lp['median_ms']:.1f} ms, std {lp['std_ms']:.1f} ms, "
              f"min {lp['min_ms']:.1f}, max {lp['max_ms']:.1f}")
    if li:
        print(f"input timestamp latency (SendInput -> record): median {li['median_ms']:.2f} ms, max {li['max_ms']:.2f} ms")
    for k, v in rep["criteria"].items():
        print(f"  {'✓' if v else '✗'} {k}")
    print("PASS" if rep["pass"] else "FAIL", f"report: {args.docs}/synctest_{cfg.record.architecture}_{cfg.record.fps}fps.json/.png")
    return 0 if rep["pass"] else 1


def cmd_bench(args) -> int:
    from . import bench

    _reports(args)
    cfg = _cfg(args)
    cfg.sound.enabled = False
    if args.mode == "quality":
        bench.run_quality(cfg, seconds=args.duration or 5, scene=not args.no_scene,
                          ref=Path(args.ref) if args.ref else None, docs_dir=Path(args.docs))
    else:
        bench.run_perf(cfg, duration=args.duration or 60, quick=args.quick, scene=not args.no_scene,
                       keep=args.keep, docs_dir=Path(args.docs))
    print(f"results: {args.docs}/bench_{args.mode}.md")
    return 0


def cmd_config(args) -> int:
    import os

    from .config import APP_HOME, ensure_user_config

    p = ensure_user_config()
    print(f"Settings:   {p}")
    print(f"Recordings: {APP_HOME / 'sessions'}")
    if not args.no_open:
        os.startfile(p)  # opens in Notepad / the default editor
    return 0


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fortgeist", description="Fortgeist: gameplay recording (video + input). "
                                "No command = fortgeist record.")
    p.add_argument("--config", default=None, help="path to config.toml (default ~/Fortgeist/config.toml)")
    sub = p.add_subparsers(dest="cmd")

    def rec_overrides(sp):
        sp.add_argument("--arch", dest="architecture", choices=["A", "B"])
        sp.add_argument("--fps", type=int)
        sp.add_argument("--encoder", choices=["hevc_nvenc", "av1_nvenc", "h264_nvenc"])
        sp.add_argument("--qp", type=int)
        sp.add_argument("--downscale", help="e.g. 1920x1080; '' = native")

    r = sub.add_parser("record", help="record (F9 start/stop, F8 marker, F10 pause)")
    rec_overrides(r)
    r.add_argument("--tag", default="", help="session tag: island, regular, ...")
    r.add_argument("--note", default="", help="free-form note")
    r.add_argument("--autostart", action="store_true", help="start a session right away, without F9")
    r.add_argument("--duration", type=float, default=0, help="stop after N seconds and exit")
    r.add_argument("--exit-after-stop", action="store_true", help="exit after the session stops")
    r.add_argument("--est-gbph", type=float, default=15.0, help="GB/h estimate before the first measurements")
    r.add_argument("--allow-pid", type=int, action="append", default=[], help=argparse.SUPPRESS)  # for tests
    r.set_defaults(fn=cmd_record)

    v = sub.add_parser("view", help="view a session with an input overlay")
    v.add_argument("session", nargs="?", default="last")
    v.add_argument("--export", nargs=2, metavar=("MM:SS-MM:SS", "OUT.mp4"))
    v.add_argument("--width", type=int, default=1280)
    v.add_argument("--offset-ms", type=float, default=0.0, help="shift input relative to frames")
    v.set_defaults(fn=cmd_view)

    c = sub.add_parser("config", help="open settings (creates ~/Fortgeist/config.toml)")
    c.add_argument("--no-open", action="store_true", help="only create it and print the path")
    c.set_defaults(fn=cmd_config)

    i = sub.add_parser("info", help="session summary and integrity check")
    i.add_argument("session", nargs="?", default="last")
    i.add_argument("--json", action="store_true")
    i.add_argument("--fast", action="store_true", help="skip ffprobe")
    i.set_defaults(fn=cmd_info)

    rp = sub.add_parser("repair", help="finalize sessions after a crash")
    rp.add_argument("session", nargs="?")
    rp.set_defaults(fn=cmd_repair)

    s = sub.add_parser("synctest", help="frame/input sync test (own test window)")
    rec_overrides(s)
    s.add_argument("-n", type=int, default=60, help="number of presses")
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--docs", default=None, help="where to put the report (default ~/Fortgeist/reports)")
    s.set_defaults(fn=cmd_synctest)

    b = sub.add_parser("bench", help="benchmark recording configurations")
    b.add_argument("mode", nargs="?", choices=["perf", "quality"], default="perf")
    b.add_argument("--duration", type=float, default=0)
    b.add_argument("--quick", action="store_true", help="4 configurations instead of the full matrix")
    b.add_argument("--no-scene", action="store_true", help="record what is on screen (whitelisted window)")
    b.add_argument("--keep", action="store_true", help="do not delete recorded bench sessions")
    b.add_argument("--ref", help="quality: ready-made lossless reference clip")
    b.add_argument("--docs", default=None, help="where to put the report (default ~/Fortgeist/reports)")
    b.set_defaults(fn=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:  # double click on the shortcut / plain `fortgeist`
        args = parser.parse_args(argv + ["record"])
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # a clear error instead of a traceback for regular users
        if args.cmd == "record" and sys.stdin.isatty():
            print(f"\nError: {e}")
            input("Press Enter to close the window…")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
