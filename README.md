# Fortgeist

Records how you play Fortnite: screen video plus keyboard and mouse input.

## Install

You need Windows 10/11 and an NVIDIA graphics card.

1. Press **Start**, type `PowerShell` and open it.
2. Paste this line and press **Enter**:

```
irm https://raw.githubusercontent.com/levzb/fortgeist/main/install.ps1 | iex
```

A **Fortgeist** shortcut will appear on your desktop.

## How to record

1. Open the **Fortgeist** shortcut, then start Fortnite.
2. **F9**: start recording. **F9** again: stop.
3. **F8**: marker. **F10**: pause.

Recordings are saved to `%USERPROFILE%\Fortgeist\sessions`.

## What is recorded

Only while Fortnite is focused and recording is on: screen, keyboard, mouse, plus your key bindings and
sensitivity from the game settings. Everything stays on your computer. Nothing is sent
to the game, and the program never touches the game process.
