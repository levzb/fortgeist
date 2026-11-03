"""Loads config.toml into dataclasses with defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "config.toml"
# Everything is stored here: config.toml, sessions/, reports/. Can be overridden with FORTGEIST_HOME.
APP_HOME = Path(os.environ.get("FORTGEIST_HOME") or (Path.home() / "Fortgeist"))
DEFAULT_TEMPLATE = Path(__file__).with_name("config_default.toml")


@dataclass
class RecordConfig:
    fps: int = 30
    output_idx: int = 0
    architecture: str = "A"  # "A" (dxcam -> pipe -> ffmpeg) | "B" (ffmpeg ddagrab)
    encoder: str = "hevc_nvenc"  # hevc_nvenc | av1_nvenc | h264_nvenc
    qp: int = 24  # constant QP (NVENC -rc constqp)
    preset: str = "p4"
    downscale: str = ""  # "" = native resolution, otherwise "1920x1080"
    gop_seconds: float = 2.0
    chunk_minutes: float = 10.0
    ring_slots: int = 8  # frames in the ring buffer (architecture A)
    data_dir: str = str(APP_HOME / "sessions")
    min_free_hours: float = 4.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass
class FocusConfig:
    # "*" = any window (tests only: turns the recorder into a plain keylogger!)
    process_whitelist: list[str] = field(
        default_factory=lambda: ["FortniteClient-Win64-Shipping.exe"]
    )
    poll_ms: int = 250


@dataclass
class HotkeyConfig:
    start_stop: str = "F9"
    marker: str = "F8"
    pause: str = "F10"


@dataclass
class SoundConfig:
    enabled: bool = True


@dataclass
class ShareConfig:
    zip_after_stop: bool = True  # pack each finished session into one .zip, ready to send
    dir: str = str(APP_HOME / "ready_to_send")  # where the zips go
    open_folder: bool = True  # show the zip in Explorer (only when the game is not in focus)


@dataclass
class PlayerProfile:
    """Player profile: without it mouse counts cannot be converted into camera degrees."""

    mouse_dpi: int = 800
    mouse_polling_hz: int = 1000
    windows_pointer_speed: int = 10  # 1..20; does not affect Raw Input, but we record it
    sens_x: float = 0.0  # Fortnite: X-Axis Sensitivity, %
    sens_y: float = 0.0  # Fortnite: Y-Axis Sensitivity, %
    targeting_sens: float = 0.0  # Targeting Sensitivity (ADS), %
    scope_sens: float = 0.0  # Scope Sensitivity, %
    invert_y: bool = False
    game_resolution: str = "1920x1080"
    window_mode: str = "windowed_fullscreen"  # fullscreen | windowed_fullscreen | windowed
    rendering_mode: str = ""  # DirectX 11 | DirectX 12 | Performance
    frame_rate_limit: int = 0
    visualize_sound_effects: bool = False
    keybinds_note: str = ""  # free text, if bindings are non-standard


@dataclass
class Config:
    record: RecordConfig = field(default_factory=RecordConfig)
    focus: FocusConfig = field(default_factory=FocusConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    share: ShareConfig = field(default_factory=ShareConfig)
    player: PlayerProfile = field(default_factory=PlayerProfile)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fill(dc_type: type, data: dict[str, Any], path: str) -> Any:
    obj = dc_type()
    known = {f.name: f for f in fields(dc_type)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"config: unknown key {path}.{key}")
        cur = getattr(obj, key)
        if is_dataclass(cur):
            if not isinstance(value, dict):
                raise ValueError(f"config: {path}.{key} must be a table")
            value = _fill(type(cur), value, f"{path}.{key}")
        elif isinstance(cur, bool) and not isinstance(value, bool):
            raise ValueError(f"config: {path}.{key} must be a bool")
        elif isinstance(cur, float) and isinstance(value, int):
            value = float(value)
        setattr(obj, key, value)
    return obj


def find_config() -> Path | None:
    for p in (Path(DEFAULT_CONFIG_NAME), APP_HOME / DEFAULT_CONFIG_NAME):
        if p.exists():
            return p
    return None


def ensure_user_config() -> Path:
    """Creates ~/Fortgeist/config.toml from the template if it does not exist yet."""
    p = APP_HOME / DEFAULT_CONFIG_NAME
    if not p.exists():
        APP_HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def load_config(path: str | Path | None = None) -> Config:
    """Explicit path -> ./config.toml -> ~/Fortgeist/config.toml -> built-in defaults."""
    p = Path(path) if path is not None else find_config()
    if p is None:
        cfg = Config()
        validate(cfg)
        return cfg
    with p.open("rb") as f:
        data = tomllib.load(f)
    cfg = _fill(Config, data, "")
    # a relative data_dir is relative to the config folder
    dd = Path(cfg.record.data_dir).expanduser()
    if not dd.is_absolute():
        dd = p.resolve().parent / dd
    cfg.record.data_dir = str(dd)
    sd = Path(cfg.share.dir).expanduser()
    if not sd.is_absolute():
        sd = p.resolve().parent / sd
    cfg.share.dir = str(sd)
    validate(cfg)
    return cfg


def parse_size(s: str) -> tuple[int, int] | None:
    if not s:
        return None
    w, h = s.lower().split("x")
    return int(w), int(h)


def validate(cfg: Config) -> None:
    r = cfg.record
    if not 1 <= r.fps <= 120:
        raise ValueError("record.fps out of range 1..120 (spec: 20..60)")
    if r.architecture not in ("A", "B"):
        raise ValueError("record.architecture must be 'A' or 'B'")
    if r.encoder not in ("hevc_nvenc", "av1_nvenc", "h264_nvenc"):
        raise ValueError("record.encoder: hevc_nvenc | av1_nvenc | h264_nvenc")
    if not 0 <= r.qp <= 51:
        raise ValueError("record.qp out of 0..51")
    if r.ring_slots < 3:
        raise ValueError("record.ring_slots >= 3")
    sz = parse_size(r.downscale)
    if sz and (sz[0] % 2 or sz[1] % 2):
        raise ValueError("record.downscale: width and height must be even")
    if not cfg.focus.process_whitelist:
        raise ValueError("focus.process_whitelist is empty")
