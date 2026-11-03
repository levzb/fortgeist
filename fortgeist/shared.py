"""Shared state of the recorder processes: an int64 array in shared memory.

Each field has exactly one writer process, aligned 64-bit writes on x64
are atomic, so no locks are needed.
"""

from __future__ import annotations

from multiprocessing.sharedctypes import RawArray

# control flags (written by main)
SESSION_ON = 0  # 1 while a session is running
MANUAL_PAUSE = 1  # 1 = manual pause (F10)
SESSION_GEN = 2  # session number since program start
# written by the input process
FOCUS_OK = 3  # 1 = foreground window is whitelisted
INPUT_EVENTS = 4  # total input events recorded in the current session
INPUT_READY = 5
# written by the capture process
CAPTURE_READY = 6
FRAMES_FED = 7  # frames fed to the encoder in the current session
FRAMES_DROPPED = 8  # dropped slots in the current session
FRAMES_REPEAT = 9  # frames without a new present (static screen)
ENC_QUEUE = 10  # frames currently queued for the encoder
CHUNK_IDX = 11
CHUNK_BYTES = 12  # size of closed chunk files + the current one
QUEUE_LAT_US = 13  # average capture -> fed to encoder latency, us (EMA)
CAPTURE_ERROR = 14  # != 0: encoder/capture crashed
LAST_FRAME_NS = 15
CAPTURE_FPS_X100 = 16  # actual FPS over the last second * 100
ENC_INIT_MS = 17

SIZE = 32


def create():
    return RawArray("q", SIZE)


def recording_active(state) -> bool:
    """Are we recording right now: session running, no manual pause, game focused."""
    return bool(state[SESSION_ON]) and not state[MANUAL_PAUSE] and bool(state[FOCUS_OK])
