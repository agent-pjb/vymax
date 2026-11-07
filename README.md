<div align="center">

<img src="vymax/assets/logo.png" alt="VYMAX logo" width="170">

# VYMAX

**Convert videos, add subtitles and resize — in a clean dark desktop app powered by FFmpeg.**

[![CI](https://github.com/agent-pjb/vymax/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-pjb/vymax/actions) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

</div>

**VYMAX** is a simple, safe desktop app for batch-converting videos (AVI, TS, MKV, MOV, …) to MP4 and other formats using [FFmpeg](https://ffmpeg.org/) — with **subtitle support** (burn-in or embedded tracks) and **resizing** (e.g. 720p). Built with Python and Tkinter — **no third-party Python packages required**.

It started life as two short FFmpeg scripts (one to convert, one to add subtitles and resize). It's now a full GUI with a progress bar, batch queue, and safeguards against losing your files.

<!-- Add a screenshot (dark mode looks great): save it as docs/screenshot.png and uncomment the line below -->
[Screenshot](docs/screenshot.png)

## ✨ Features

- **Batch conversion** — add individual files or a whole folder
- **Live progress** — per-file and overall progress bars with encoding speed
- **Formats** — MP4, MKV, MOV, WebM (VP9), AVI, and MP3 (audio extraction)
- **Two methods** — *Re-encode* (most compatible) or *Fast copy* (re-wraps streams instantly, no quality loss)
- **Subtitles** — *burn them into the picture* (hard-coded, works everywhere) or *embed them as a selectable track*; supports `.srt`, `.ass`, `.ssa`, `.vtt`
- **Auto-matching** — `Movie.mkv` automatically picks up `Movie.srt` or `Movie.en.srt` from the same folder; or attach any subtitle file by hand
- **Resize** — Original, 1080p, 720p, 480p or 360p (aspect ratio kept, small videos are never upscaled)
- **Quality & speed controls** — High / Balanced / Small file, plus H.264 speed presets
- **Safe "delete original"** — optional, off by default, asks for confirmation, and only happens after a *verified successful* conversion
- **No half-finished files** — output is written to a temp name and only renamed on success, so cancelling or a crash never leaves a broken `.mp4`
- **Cancel anytime** — stops the current file and skips the rest
- **Remembers your settings** between sessions (the delete option is deliberately never remembered)
- **Dark mode** — easy on the eyes, on by default, with a one-click switch to light mode (your choice is remembered)
- **Responsive UI** — conversion runs in a background thread
- **Cross-platform** — Windows, macOS, Linux

## 📋 Requirements

| Requirement | Notes |
|---|---|
| Python 3.9+ | Includes Tkinter on Windows/macOS installers |
| FFmpeg | Must be on your `PATH` (or select it in-app with **Locate…**) |

### Installing FFmpeg

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt install ffmpeg python3-tk` |
| Fedora | `sudo dnf install ffmpeg python3-tkinter` |

> On Linux, Tkinter is packaged separately (`python3-tk`). On Windows/macOS it ships with Python.

## 🚀 Usage

```bash
git clone https://github.com/agent-pjb/vymax.git
cd vymax
python run.py
```

Or install it as a command:

```bash
pip install .
vymax
```

1. Click **Add files…** or **Add folder…**
2. Pick an output folder (leave empty to save next to the originals) and a format
3. Click **Convert**

### Dark / light mode

The app opens in **dark mode**. Click **Light mode** (top-right) to switch, and **Dark mode** to switch back — your choice is saved for next time.

- Colours are defined in one place, `vymax/theme.py`, so it's easy to tweak the look or add your own palette.
- On Windows 10/11 the title bar goes dark too. Operating-system dialogs (file pickers, confirmation pop-ups) are drawn by your OS and follow its own theme.

### Adding subtitles

Subtitles are applied **per file**, and you can see what each file will use in the *Subtitles* column.

- **Automatic:** when you add `Episode 1.mp4`, the app looks for `Episode 1.srt` (or `.ass`/`.ssa`/`.vtt`, or a language-tagged name like `Episode 1.en.srt`) in the same folder.
- **Manual:** select one or more files in the list → **Attach subtitles…** → choose a subtitle file. Use **Remove subtitles** to detach it.
- Files without a subtitle are simply converted normally.

Then choose how they're added:

| Mode | What it does | Best for |
|---|---|---|
| **Burn into video** | Draws the text permanently onto the picture (hard-coded) | Players/TVs/social platforms that ignore subtitle tracks. Requires re-encoding. |
| **Embed as track** | Stores the subtitles as a separate, switchable track | Normal media players. Fast, no quality loss, works with *Fast copy*. MP4/MOV/MKV/WebM only. |

> Tip: for 720p with burned-in subtitles, set **Subtitles → Burn into video** and **Resolution → 720p**.

Notes:
- Burning subtitles or resizing changes the picture, so the app automatically switches from *Fast copy* to *Re-encode*.
- Subtitle files should be **UTF-8** encoded. If accented characters look garbled, re-save the `.srt` as UTF-8 in any text editor.
- Burn-in needs an FFmpeg build with the `subtitles` filter (libass). Standard builds from the install commands above include it; if yours doesn't, the app tells you and you can use *Embed as track* instead.

### Which method should I use?

| Method | Speed | Quality | Use when |
|---|---|---|---|
| **Re-encode** | Slower | Slight loss (tunable) | Always works; best compatibility |
| **Fast copy** | Near-instant | Lossless | Source is already H.264/AAC (e.g. many `.ts`/`.mkv` files → `.mp4`) |

Fast copy only works when the source codecs are allowed in the target container. If it fails, the log explains why — just switch to *Re-encode*.

## 🔧 How it works

For each file the app runs FFmpeg roughly like this:

```bash
ffmpeg -i input.avi -map 0:v? -map 0:a? -c:v libx264 -crf 23 -preset medium \
       -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart \
       -progress pipe:1 output.converting.mp4
```

The duration is read with `ffprobe`, and FFmpeg's `-progress` output is parsed to drive the progress bars. When FFmpeg exits successfully the temp file is renamed to the final name.

Subtitles and resizing use FFmpeg's video filter chain. A 720p burn-in becomes:

```bash
ffmpeg -i input.mp4 -map 0:v? -map 0:a? \
       -c:v libx264 -vf "subtitles=subtitle.srt,scale=-2:min(720\,ih)" \
       -crf 23 -c:a aac output.mp4
```

`scale=-2:…` keeps the aspect ratio (and an even width), and `min(720,ih)` stops small videos being blown up. To avoid FFmpeg's tricky path-escaping rules (spaces, apostrophes, `C:\` drive letters…), the app copies the subtitle file to a temporary folder under a plain name and runs FFmpeg from there — so **any file name works**. The temp folder is removed afterwards.

Embedding instead adds the subtitle as a second input mapped as its own stream (`mov_text` in MP4/MOV, SRT/ASS in MKV, WebVTT in WebM).

### Improvements over the original scripts

| Original script | This project |
|---|---|
| Hard-coded input/output paths | File/folder pickers, batch queue |
| Deleted the source if *an output file existed* — even if FFmpeg had failed midway | Deletes only after a verified successful conversion (and only if you opt in) |
| No progress or error feedback | Progress bars, log pane, clear status per file |
| Blocks until finished | Background thread, cancellable |
| Single format | Six output formats, quality/speed options |
| Subtitle script used the `ffmpeg-python` package, a hard-coded `.srt` path and `concat` | Pure standard library (no `pip install`), per-file subtitles with auto-matching, burn-in *or* embedded track, any filename |
| 720p scaling meant editing the script | One dropdown: 1080p / 720p / 480p / 360p, never upscales |

## 🗂️ Project structure

```
vymax/
├── vymax/
│   ├── core.py        # FFmpeg engine: conversion, subtitles, resizing (no GUI code)
│   ├── gui.py         # Tkinter interface
│   ├── theme.py       # Dark / light colour palettes
│   ├── assets/        # Logo and app icons (PNG, ICO, ICNS)
│   └── __main__.py    # python -m vymax
├── tests/             # Unit + integration tests
├── run.py             # Simple launcher
├── requirements-build.txt  # PyInstaller (only needed to build an .exe/app)
├── pyproject.toml
├── LICENSE
└── .github/workflows/ci.yml
```

## 🧪 Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover command building, skip/overwrite logic, failure handling (the original is never deleted on error), subtitle burn-in/embedding and resizing (including awkward file names), and — if FFmpeg is installed — real conversions. CI runs on Windows, macOS and Linux.

## 📦 Build a standalone executable (optional)

**Windows**

```bash
pip install -r requirements-build.txt
pyinstaller --noconsole --onefile --name VYMAX --icon vymax/assets/icon.ico --add-data "vymax/assets;vymax/assets" run.py
```

**macOS / Linux**

```bash
pip install -r requirements-build.txt
pyinstaller --noconsole --onefile --name VYMAX --icon vymax/assets/icon.icns --add-data "vymax/assets:vymax/assets" run.py
```

`requirements-build.txt` just contains PyInstaller — VYMAX itself needs no `pip install` to run.

(On Linux the `--icon` option is ignored; the window icon is still set by the app itself.) The executable appears in `dist/`. Users still need FFmpeg installed (or can point to it with **Locate…**).

## 🗺️ Roadmap ideas

- Drag-and-drop support
- Follow the operating system's light/dark setting automatically
- Subtitle styling options (font size, colour, position) and encoding picker
- Extract existing subtitle tracks from videos
- Recursive folder scanning
- Custom FFmpeg arguments field
- Save/load presets
- Bundle FFmpeg in release builds

## 🤝 Contributing

Issues and pull requests are welcome. Please run the tests before submitting.

## 📄 License

VYMAX is released under the [MIT License](LICENSE) © Pj B.

VYMAX doesn't include or modify FFmpeg — it simply runs the `ffmpeg` program installed on your computer, which is licensed separately (LGPL or GPL, depending on how your build was made). If you distribute your own builds of VYMAX *with* FFmpeg bundled inside, follow FFmpeg's [license terms](https://ffmpeg.org/legal.html).

---
*Powered by [FFmpeg](https://ffmpeg.org/). This project is not affiliated with the FFmpeg developers.*
