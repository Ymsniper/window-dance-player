# 🎵 Window Dance Player

```
╔══════════════════════════════════════════════╗
║   🎵  WINDOW DANCE PLAYER  🎵                ║
║  Inspired by Rhythm Doctor's Window Dance    ║
║  🚗  Now with YARIS MODE  🚗                 ║
╚══════════════════════════════════════════════╝
```



https://github.com/user-attachments/assets/74b4d266-2e1a-4cf9-ba64-93825e91643f

https://github.com/user-attachments/assets/6ec28f4c-0f50-47d0-9fd7-eb4135a27d30




A terminal music player that **moves and resizes your terminal window in sync
with the beat** of whatever you're playing. Six choreographed patterns plus a
physics-based Toyota Yaris lowrider bounce (YARIS MODE). Supports Hyprland,
Sway, KDE Plasma Wayland, and X11 out of the box.

> Inspired by the [Window Dance](https://store.steampowered.com/app/1303230/Rhythm_Doctor/)
> mechanic in Rhythm Doctor.

---

## ✨ Features

- **Beat-accurate window animation** — librosa's beat tracker feeds a
  sub-millisecond polling loop; lead-time compensation keeps the visual
  effect aligned with the audio beat even under compositor latency
- **Six dance patterns** — Circle, Corner Bounce, Zigzag, Figure-8,
  Random Jump, and the physics-based 🚗 YARIS MODE
- **YARIS MODE physics engine** — underdamped spring simulation with
  gravity, launch velocity, landing squash, and horizontal wobble;
  runs at display refresh rate (up to 360 Hz)
- **🖼️ Image overlay** — display any image (PNG, JPEG, WEBP, GIF, …)
  as a full-terminal background that warps in real-time with the Yaris
  physics; bilinear numpy warp fills the canvas edge-to-edge with zero
  black bars; beat flash brightens the image on every detected beat;
  load via `--image` at startup or toggle live with `I`
- **Zero-subprocess hot path** — Hyprland IPC via raw Unix socket,
  Sway via i3 binary protocol, X11 via libX11 ctypes; no process
  fork per frame
- **Auto-pattern rotation** — cycles through non-Yaris patterns every
  32 beats so the dance stays interesting on long tracks
- **Curses TUI** — live beat visualiser, progress bar, playlist, BPM
  display, and beat flash indicator
- **Playlist support** — pass any number of audio files; auto-advances
  at track end

---

## 🖥️ Compatibility

### Tested on

| Distro | Compositor | Status |
|--------|-----------|--------|
| **CachyOS** (Arch-based) | Hyprland | ✅ Fully working — primary dev environment |

### Expected to work

| Distro / OS | Compositor | Notes |
|-------------|-----------|-------|
| Arch Linux | Hyprland | Same as CachyOS |
| Arch Linux | Sway | Via i3 IPC socket |
| Arch Linux | KDE Plasma 6 Wayland | Via KWin scripting (slower, 4 fps) |
| Arch Linux / any | X11 (any WM) | Requires `xdotool`; libX11 ctypes used as fast path |
| Manjaro | Hyprland / X11 | Should work identically to Arch |
| EndeavourOS | Hyprland / X11 | Should work identically to Arch |
| Ubuntu 22.04+ | X11 / Sway | `xdotool` from apt; `swaymsg` bundled with Sway |
| Fedora 38+ | X11 / Sway / KDE | `xdotool` from dnf |
| openSUSE Tumbleweed | X11 / KDE | `xdotool` from zypper |
| macOS | — | Window move only (no resize); uses `osascript` |
| Windows 10/11 | — | Basic move via `user32.SetWindowPos` |

### Compositor support matrix

| Compositor | Backend | FPS cap | Notes |
|-----------|---------|---------|-------|
| **Hyprland** | Direct Unix IPC `[[BATCH]]` | Display Hz | Best experience; `hyprctl` for queries |
| **Sway** | i3-protocol Unix socket | Display Hz | Window must be floating |
| **KDE Plasma Wayland** | KWin JS scripting via qdbus | ~4 fps | Requires qdbus6 or qdbus |
| **X11 (any)** | libX11 ctypes → xdotool fallback | Display Hz | `xdotool` only needed if libX11 unavailable |
| **GNOME Wayland** | ❌ Not supported | — | No standard API for window control |

### ⚠️ Wayland: window must be floating

Tiling window managers lock window geometry. Before running, make your
terminal window float:

| Compositor | How to float |
|-----------|--------------|
| Hyprland | `Super+V` (or `windowrule float` in config) |
| Sway | `$mod+Shift+Space` |
| KDE Plasma | Right-click titlebar → More Actions → Float |

---

## 📦 Requirements

### Python packages

```
librosa >= 0.10.0
pygame  >= 2.5.0
numpy   >= 1.24.0
Pillow  >= 9.0.0    # optional — required for image overlay (--image / I key)
```

### System packages

| Tool | Used for | How to install |
|------|----------|---------------|
| `xdotool` | X11 window control (fallback) | `sudo pacman -S xdotool` / `sudo apt install xdotool` |
| `hyprctl` | Hyprland window queries | Bundled with Hyprland |
| `swaymsg` | Sway window control | Bundled with Sway |
| `qdbus6` / `qdbus` | KDE KWin scripting | Pre-installed with KDE Plasma |
| `xrandr` | Screen-size detection on X11 | Usually pre-installed |

---

## 🚀 Installation

### CachyOS / Arch Linux (recommended)

```bash
# 1. Clone
git clone https://github.com/Ymsniper/window-dance-player.git
cd window-dance-player

# 2. System packages (pygame + numpy are faster from pacman)
sudo pacman -S python-pygame python-numpy

# 3. librosa (not in pacman, install via pip)
pip install librosa --break-system-packages

# 4. X11 users only
sudo pacman -S xdotool

# 5. Optional — image overlay feature (--image flag / I key)
sudo pacman -S python-pillow
```

### Ubuntu / Debian

```bash
git clone https://github.com/Ymsniper/window-dance-player.git
cd window-dance-player

sudo apt install python3-pip xdotool
pip install -r requirements.txt --break-system-packages
```

### Fedora

```bash
git clone https://github.com/Ymsniper/window-dance-player.git
cd window-dance-player

sudo dnf install xdotool
pip install -r requirements.txt --break-system-packages
```

### Pip install (all distros)

```bash
git clone https://github.com/Ymsniper/window-dance-player.git
cd window-dance-player
pip install . --break-system-packages
```

After `pip install`, the `window-dance-player` command is available globally.

---

## 🎮 Usage

```bash
# Single file
python -m window_dance_player song.mp3

# Playlist (auto-advances)
python -m window_dance_player *.mp3
python -m window_dance_player path/to/playlist/

# With image overlay (warps with Yaris physics)
python -m window_dance_player --image cover.jpg song.mp3

# Or, after pip install:
window-dance-player song.mp3 song2.flac song3.wav
window-dance-player --image cover.png *.flac
```

### Controls

| Key | Action |
|-----|--------|
| `SPACE` | Play / Pause |
| `N` | Next track |
| `P` | Previous track |
| `D` | Toggle window dance on/off |
| `1` | Pattern: Circle |
| `2` | Pattern: Corner Bounce |
| `3` | Pattern: Zigzag |
| `4` | Pattern: Figure-8 |
| `5` | Pattern: Random Jump |
| `6` | 🚗 YARIS MODE |
| `I` | Add / remove image overlay |
| `Q` | Quit |

---

## 🚗 YARIS MODE explained

Pattern 6 is a physics simulation of the Toyota Yaris lowrider bounce meme.

On every detected beat:
1. The window **snaps to the floor** (base Y position)
2. An **instantaneous squash** is applied (`scale_y = 0.64`, window goes wide and flat)
3. An **underdamped spring kick** (`vel_scale_y = 3.0`) causes the spring to overshoot
   past `scale_y = 1.0` all the way into **stretch territory** (`scale_y > 1.0`)
4. Simultaneously, **gravity launches** the window upward (`vel_y = -380 px/s`)
5. The window **arcs upward**, stretching as it rises
6. **Gravity pulls it back**, it lands, squashes proportional to impact speed, repeats

The key physics constraint is that the scale spring must be **underdamped**:
`SCALE_DAMP < 2 × sqrt(SCALE_SPRING)` → `5 < 2 × sqrt(80) ≈ 17.9` ✅

If overdamped, the spring only slowly returns to 1.0 — no overshoot, no stretch,
no meme. The damping ratio ζ ≈ 0.28 gives a satisfying bouncy oscillation.

---

## 🏗️ Architecture & Code Analysis

The project is split into focused modules for easy auditing:

```
window_dance_player/
├── __init__.py          Version metadata
├── __main__.py          Enables python -m window_dance_player
├── logger.py            Centralised DBG/WARN/ERR → wdp_debug.log
├── main.py              Argument parsing, startup checks, curses launch
├── player.py            Audio playback + beat-watcher thread
├── ui.py                Curses TUI + flicker-free image overlay mode (~25 FPS)
│
├── image/
│   ├── overlay.py       ImageOverlay: bilinear numpy warp, half-block renderer,
│   │                    beat flash, Synchronized Output (DEC 2026), single
│   │                    atomic os.write() per frame — zero tearing
│   └── warp.py          ImageWarp: earlier mesh-transform prototype (reference)
│
├── platform/
│   ├── detect.py        PLATFORM / SESSION_TYPE / COMPOSITOR constants
│   ├── x11.py           libX11 ctypes fast path (_init_x11_fast)
│   ├── hyprland.py      Hyprland Unix IPC (_hyprland_batch, _hyprland_query)
│   ├── sway.py          Sway i3-protocol socket (_sway_command)
│   ├── kde.py           KDE KWin scripting via qdbus (_kwin_script)
│   └── window.py        Dispatch: get_window_id / get_screen_size / move_window
│
├── audio/
│   └── analysis.py      analyze_audio() → (duration, bpm, beat_times[])
│
└── dance/
    ├── patterns.py      PATTERNS list + PATTERN_LABELS dict
    ├── physics.py       YarisPhysics class + _sim_yaris_frames()
    └── dancer.py        WindowDancer: on_beat(), _physics_loop(), patterns
```

### How beat detection works

```
Audio file
    └─► librosa.load()            (background thread, mono, native SR)
    └─► librosa.beat.beat_track() → tempo (BPM) + beat_frames[]
    └─► librosa.frames_to_time()  → beat_times[] in seconds

Beat watcher thread (2 ms poll):
    pos = time.now() - start_time
    while beat_times[i] <= pos + LEAD_TIME:
        dancer.on_beat()
        i++
```

The `LEAD_TIME` constant compensates for IPC latency:
- KDE: 175 ms (KWin scripting round-trip)
- X11: 45 ms (xdotool fork + X compositing)
- Hyprland/Sway: 25 ms (fast socket + one Wayland frame)

### How window control works

The hot path in `WindowDancer._physics_loop()` pre-resolves all
compositor handles **before** entering the loop so there is zero
conditional overhead or module attribute lookup per frame:

```python
# Pre-cache once:
_hsock = _hypr._HYPR_SOCK
_htmpl = "[[BATCH]]dispatch resizewindowpixel exact {w} {h},address:..."

# Hot loop (runs at 60–360 Hz):
payload = _htmpl.format(w=cw, h=ch, x=x, y=y).encode()
with socket(AF_UNIX) as s:
    s.connect(_hsock)
    s.sendall(payload)   # one connect + one send per frame
```

### Threading model

| Thread | Purpose | Runs at |
|--------|---------|---------|
| Main | pygame audio + curses UI | ~25 FPS |
| `_analyze` | librosa beat detection | Once per track |
| `_watch` | Beat timing poll → `on_beat()` | 2 ms intervals |
| `_physics_loop` | Yaris physics → IPC | Display Hz (60–360) |

---

## 🐛 Debugging

A debug log is written to `wdp_debug.log` in the working directory on
every run. It contains:
- Startup platform/compositor detection results
- IPC socket paths resolved
- Every beat fired (first 5, then every 20th)
- Physics loop first-frame diagnostics
- Any IPC errors

```bash
tail -f wdp_debug.log   # live debug output while running
```

---

## 🤝 Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/my-feature`
3. Make your changes with clear, focused commits
4. Open a pull request

The module split is intentional — each subdirectory is a self-contained
feature package. Adding a new compositor means adding one file in
`platform/` and a branch in `platform/window.py`. The `image/` package
follows the same pattern: `image/overlay.py` is the active renderer and
can be swapped or extended independently of the rest of the UI.

---

## 📄 License

Copyright (C) 2024 Ymsniper

This program is free software: you can redistribute it and/or modify
it under the terms of the **GNU General Public License** as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

See [LICENSE](LICENSE) for the full text.
