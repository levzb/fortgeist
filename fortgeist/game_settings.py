"""Fortnite bindings and settings from files on disk (read-only).

Sources (%LOCALAPPDATA%\\FortniteGame\\Saved\\Config):
  * ClientSettings.Sav: UE5 save (tagged properties): bindings
    (FortActionKeyMapping: ActionName, KeyBind1/2.KeyName, group, subgame) and
    numeric/boolean settings (sensitivity etc.);
  * WindowsClient/GameUserSettings.ini: whitelisted keys only
    (resolution, window mode, DirectX, FPS); island history and the rest are not read.

Safety: files are only read whole into memory, nothing is modified,
the game process is not touched. String fields (except action/key names),
the account ID and the cloud (encrypted) file are not read.

The file may be older than the current game version (the up-to-date copy of the settings lives
in the Epic cloud in encrypted form), so file and game versions are written side by side, flag
`possibly_stale`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import struct
from pathlib import Path

SAVED = Path(os.environ.get("LOCALAPPDATA", "")) / "FortniteGame" / "Saved"
CLIENT_SETTINGS = SAVED / "Config" / "ClientSettings.Sav"
GAME_USER_SETTINGS = SAVED / "Config" / "WindowsClient" / "GameUserSettings.ini"
GAME_LOG = SAVED / "Logs" / "FortniteGame.log"

INI_WHITELIST = {
    "ResolutionSizeX", "ResolutionSizeY", "PreferredFullscreenMode", "LastConfirmedFullscreenMode",
    "FrameRateLimit", "FrontendFrameRateLimit", "bDisableMouseAcceleration", "PreferredRHI",
    "bUseDynamicResolution", "bUseVSync", "sg.ResolutionQuality", "TemporalSuperResolutionQuality",
    "DisplayGamma", "bShowGrass", "bMotionBlur", "bShowFPS",
}

_VERSION_RE = re.compile(rb"\+\+Fortnite\+Release-(\d+\.\d+)")
# FString property name, then the FString type of a scalar property with no type parameters
_SCALAR_RE = re.compile(
    rb"([\x02-\x50])\x00\x00\x00([A-Za-z_][A-Za-z0-9_.]{0,79})\x00"
    rb"([\x0c-\x0e])\x00\x00\x00(BoolProperty|FloatProperty|IntProperty|DoubleProperty)\x00"
    rb"\x00\x00\x00\x00(....)(.)", re.S)


class _Reader:
    def __init__(self, b: bytes):
        self.b = b

    def fstring(self, p: int) -> tuple[str, int]:
        n = struct.unpack_from("<i", self.b, p)[0]
        p += 4
        if n == 0:
            return "", p
        if n < 0:  # UTF-16
            raw = self.b[p:p - 2 * n]
            return raw[:-2].decode("utf-16-le", errors="replace"), p - 2 * n
        if n > 1024:
            raise ValueError("bad fstring")
        return self.b[p:p + n - 1].decode("latin-1"), p + n

    def name_value_after(self, field_end: int) -> str:
        """NameProperty value: type 'NameProperty', 0 parameters, size, flags, FString."""
        t, p = self.fstring(field_end)
        if t != "NameProperty":
            raise ValueError(t)
        p += 4 + 4 + 1
        v, _ = self.fstring(p)
        return v


def _tag(name: str) -> bytes:
    return struct.pack("<i", len(name) + 1) + name.encode() + b"\x00"


_ARRAY_RE = re.compile(rb"([\x02-\x50])\x00\x00\x00([A-Za-z_][A-Za-z0-9_]{0,79})\x00\x0e\x00\x00\x00ArrayProperty\x00")


def parse_bindings(b: bytes) -> list[dict]:
    """All FortActionKeyMapping records. The file has several sets (default,
    custom, per subgame, gamepad); the array field says which one it came from."""
    import bisect

    arrays = [(m.start(), m.group(2).decode()) for m in _ARRAY_RE.finditer(b) if m.group(1)[0] == len(m.group(2)) + 1]
    arr_pos = [a[0] for a in arrays]
    r = _Reader(b)
    tag_action, tag_kb1, tag_kb2, tag_key = _tag("ActionName"), _tag("KeyBind1"), _tag("KeyBind2"), _tag("KeyName")
    starts = [m.start() for m in re.finditer(re.escape(tag_action), b)]
    out = []
    seen = set()
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(b), s + 4000)
        seg_end = min(end, s + 4000)
        try:
            action = r.name_value_after(s + len(tag_action))
            keys = []
            for kb in (tag_kb1, tag_kb2):
                k = b.find(kb, s, seg_end)
                if k < 0:
                    keys.append(None)
                    continue
                kn = b.find(tag_key, k, seg_end)
                v = r.name_value_after(kn + len(tag_key)) if kn >= 0 else None
                keys.append(None if v in (None, "None", "") else v)
        except (ValueError, struct.error):
            continue
        seg = b[s:seg_end]
        g = re.search(rb"EFortInputActionGroup::(\w+)\x00", seg)
        sg = re.search(rb"ESubGame::(\w+)\x00", seg)
        ai = bisect.bisect_right(arr_pos, s) - 1
        rec = {"array": arrays[ai][1] if ai >= 0 else None, "order": i, "action": action,
               "group": g.group(1).decode() if g else None,
               "subgame": sg.group(1).decode() if sg else None,
               "key1": keys[0], "key2": keys[1]}
        sc = seg.find(_tag("InputScale"))
        if sc >= 0:
            try:
                t, p = r.fstring(s + sc + len(_tag("InputScale")))
                if t == "FloatProperty":
                    rec["input_scale"] = round(struct.unpack_from("<f", b, p + 9)[0], 4)
            except (ValueError, struct.error):
                pass
        key = tuple(sorted((k, v) for k, v in rec.items() if k != "order"))
        if key not in seen:
            seen.add(key)
            out.append(rec)
    return out


def parse_scalars(b: bytes) -> dict:
    """All numeric/boolean properties (name -> value; a list on repeats)."""
    out: dict = {}
    for m in _SCALAR_RE.finditer(b):
        name = m.group(2)
        if m.group(1)[0] != len(name) + 1 or m.group(3)[0] != len(m.group(4)) + 1:
            continue
        typ = m.group(4)
        p = m.end()
        size = struct.unpack("<i", m.group(5))[0]
        flags = m.group(6)[0]
        try:
            if typ == b"BoolProperty":
                v = bool(flags & 0x10)
            elif typ == b"FloatProperty" and size == 4:
                v = round(struct.unpack_from("<f", b, p)[0], 6)
            elif typ == b"DoubleProperty" and size == 8:
                v = struct.unpack_from("<d", b, p)[0]
            elif typ == b"IntProperty" and size == 4:
                v = struct.unpack_from("<i", b, p)[0]
            else:
                continue
        except struct.error:
            continue
        k = name.decode()
        if k in out:
            prev = out[k]
            out[k] = (prev if isinstance(prev, list) else [prev]) + [v]
        else:
            out[k] = v
    return out


def parse_ini(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k in INI_WHITELIST and k not in out:
                out[k] = v.strip()
    return out


def _mtime(p: Path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def current_game_version() -> str | None:
    try:
        with open(GAME_LOG, "rb") as f:
            head = f.read(64 * 1024)
        m = _VERSION_RE.search(head)
        return m.group(1).decode() if m else None
    except OSError:
        return None


def read_game_settings(client_settings: Path = CLIENT_SETTINGS, ini: Path = GAME_USER_SETTINGS) -> dict:
    """Never raises: on error returns {'error': ...}."""
    res: dict = {"read_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        b = client_settings.read_bytes()
        m = _VERSION_RE.search(b)
        res["client_settings"] = {"file_modified": _mtime(client_settings),
                                  "file_game_version": m.group(1).decode() if m else None}
        res["bindings"] = parse_bindings(b)
        res["settings"] = parse_scalars(b)
    except OSError as e:
        res["client_settings"] = {"error": f"not read: {e.strerror or e}"}
    except Exception as e:  # format changed; recording continues anyway
        res["client_settings"] = {"error": f"not parsed: {e!r}"}
    try:
        res["game_user_settings"] = parse_ini(ini.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        res["game_user_settings"] = {"error": f"not read: {e.strerror or e}"}
    cur = current_game_version()
    res["current_game_version"] = cur
    fv = (res.get("client_settings") or {}).get("file_game_version")
    res["possibly_stale"] = bool(cur and fv and cur != fv)
    return res


def summary(gs: dict) -> dict:
    s = gs.get("settings") or {}
    pick = ("MouseSensitivityX", "MouseSensitivityY", "UpgradedMouseSensitivityX", "UpgradedMouseSensitivityY",
            "ScopedSlow", "MouseSensitivity")
    return {
        "bindings": len(gs.get("bindings") or []),
        "settings": len(s),
        "mouse": {k: s[k] for k in pick if k in s},
        "file_game_version": (gs.get("client_settings") or {}).get("file_game_version"),
        "file_modified": (gs.get("client_settings") or {}).get("file_modified"),
        "current_game_version": gs.get("current_game_version"),
        "possibly_stale": gs.get("possibly_stale"),
        "error": (gs.get("client_settings") or {}).get("error"),
    }
