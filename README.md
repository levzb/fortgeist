# Fortgeist

Records how you play Fortnite: screen video plus keyboard and mouse input, on one precise clock.

It's an open research project. Recordings from real players are used to teach an AI to move and play
like a human, **only on its own private Creative island**. It never plays in public matches, and the
project has no aimbot or aim assist of any kind. Fortgeist is the first step: a recorder, like OBS
that also writes down your key presses.

## Install

You need Windows 10/11 and an NVIDIA graphics card.

1. Press **Start**, type `PowerShell` and open it.
2. Paste this line and press **Enter**:

```
irm https://raw.githubusercontent.com/levzb/fortgeist/main/install.ps1 | iex
```

A **Fortgeist** shortcut will appear on your desktop.

The script installs [uv](https://github.com/astral-sh/uv) (Python tool manager) and
[FFmpeg](https://ffmpeg.org) (video encoder) if you don't have them, then installs Fortgeist from
this repository. You can [read it here](install.ps1): it's 15 lines.

## How to record

1. Open the **Fortgeist** shortcut, then start Fortnite.
2. **F9**: start recording. **F9** again: stop.
3. **F8**: marker (press it when something interesting happens). **F10**: pause.

## How to send a recording

After you press **F9** to stop, Fortgeist packs the recording into one file:

```
%USERPROFILE%\Fortgeist\ready_to_send\fortgeist_<date>_<time>.zip
```

When you leave the game, the folder opens with that file selected. Upload the zip to Google Drive,
Mega or Dropbox and share the link. Don't unpack or rename anything inside.

Video takes about **12 GB per hour** of play. Once you've sent a zip, you can delete it and its
folder in `%USERPROFILE%\Fortgeist\sessions`.

## What is recorded

Only while **Fortnite is the active window** and recording is on (after F9). If you alt-tab to your
browser or Discord, recording pauses on its own and nothing from other windows is saved.

| What | Why |
|---|---|
| Screen video | what you saw |
| Keyboard and mouse: which key, when, how far the mouse moved | what you did |
| Your Fortnite key bindings and sensitivity | so recordings from players with different binds can be compared |
| Monitor size, recording quality, frame timing | to line video up with input exactly |

The video shows your whole screen, including your name and chat if they are visible.

## What it does NOT do

- **Sends nothing anywhere.** No internet connection, no telemetry. Files stay on your PC until you
  share them yourself.
- **Doesn't touch the game.** No injection, no memory reading, no changes to game files, nothing is
  sent into the game. It sees the screen the same way OBS or Discord screen share does.
- **Doesn't change your settings.** It only *reads* one Fortnite settings file
  (`%LOCALAPPDATA%\FortniteGame\Saved\Config\ClientSettings.Sav`) to get your binds and
  sensitivity. It skips your account ID and island history.
- **Isn't a keylogger.** Input outside Fortnite is dropped right away and never written to disk.
  Window titles of other programs are never saved.

All the code is here, and you can read it.

## Uninstall

```
uv tool uninstall fortgeist
```

Then delete the `%USERPROFILE%\Fortgeist` folder and the desktop shortcut.
