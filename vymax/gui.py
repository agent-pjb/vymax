"""Tkinter front-end for the converter (standard library only)."""

from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import __version__
from .core import (
    FORMATS,
    QUALITY_LEVELS,
    RESOLUTIONS,
    SOFT_SUBTITLE_FORMATS,
    SPEED_PRESETS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ConversionOptions,
    ConversionResult,
    Converter,
    ffmpeg_version,
    find_executable,
    find_matching_subtitle,
    human_size,
    requires_reencode,
    scan_folder,
)
from .theme import DEFAULT_THEME, PALETTES

CONFIG_PATH = Path.home() / ".vymax.json"


def _find_assets_dir() -> Path:
    candidates = [Path(__file__).resolve().parent / "assets"]
    bundle = getattr(sys, "_MEIPASS", None)  # set when running as a PyInstaller executable
    if bundle:
        candidates += [Path(bundle) / "vymax" / "assets", Path(bundle) / "assets"]
    for folder in candidates:
        if (folder / "icon.png").is_file():
            return folder
    return candidates[0]


ASSETS_DIR = _find_assets_dir()

SUB_MODES = {"Burn into video": "burn", "Embed as track": "embed"}

STATUS_TEXT = {
    STATUS_OK: "Done",
    STATUS_SKIPPED: "Skipped",
    STATUS_FAILED: "Failed",
    STATUS_CANCELLED: "Cancelled",
}
# status -> palette key (see theme.py)
STATUS_COLORS = {
    STATUS_OK: "good",
    STATUS_SKIPPED: "warn",
    STATUS_FAILED: "bad",
    STATUS_CANCELLED: "muted",
}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"VYMAX {__version__}")
        root.minsize(800, 720)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.converter: Optional[Converter] = None
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.ffmpeg_path: Optional[str] = None
        self.items: dict[str, Path] = {}  # tree item id -> source file
        self.subtitles: dict[str, Path] = {}  # tree item id -> subtitle file

        self._idx = 0
        self._total = 0
        self._name = ""

        # Settings variables
        self._label_to_ext = {f.label: ext for ext, f in FORMATS.items()}
        self.out_dir = tk.StringVar()
        self.fmt_label = tk.StringVar(value=FORMATS["mp4"].label)
        self.mode = tk.StringVar(value="reencode")
        self.quality = tk.StringVar(value="Balanced")
        self.speed = tk.StringVar(value="medium")
        self.sub_mode_label = tk.StringVar(value="Burn into video")
        self.resolution = tk.StringVar(value="Original")
        self.overwrite = tk.BooleanVar(value=False)
        self.delete_src = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready")
        self._saved_ffmpeg: Optional[str] = None
        self.theme = DEFAULT_THEME

        self._build_ui()
        self._set_icon()
        self._load_config()
        self._apply_theme()
        self._detect_ffmpeg(self._saved_ffmpeg)
        self._refresh_controls()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll_events)

    def _set_icon(self) -> None:
        """Window / taskbar icon. The app works without it, but if it fails we say why."""
        files = [ASSETS_DIR / f"icon_{n}.png" for n in (16, 32, 48, 64, 128)] + [ASSETS_DIR / "icon.png"]
        try:
            # Keep references: Tk images vanish if Python garbage-collects them.
            self._icons = [tk.PhotoImage(file=str(f)) for f in files if f.is_file()]
            if not self._icons:
                raise FileNotFoundError(f"no icon files found in {ASSETS_DIR}")
            # iconphoto is the one call that behaves the same on Windows and Linux. (Don't
            # also call iconbitmap: on Windows the two override each other.)
            self.root.iconphoto(True, *self._icons)
        except (tk.TclError, OSError) as exc:
            self._write_log(f"Note: couldn't load the window icon ({exc})")

    # ------------------------------------------------------------------ UI -- #

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # FFmpeg status row
        row = ttk.Frame(main)
        row.pack(fill="x")
        ttk.Label(row, text="FFmpeg:", font=("TkDefaultFont", 9, "bold")).pack(side="left")
        self.ffmpeg_label = ttk.Label(row, text="checking...")
        self.ffmpeg_label.pack(side="left", padx=6)
        ttk.Button(row, text="Locate...", command=self._browse_ffmpeg).pack(side="right")
        self.theme_btn = ttk.Button(row, command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=(0, 6))

        # File list
        files_box = ttk.LabelFrame(main, text="Files to convert", padding=8)
        files_box.pack(fill="both", expand=True, pady=(8, 4))
        files_box.columnconfigure(0, weight=1)
        files_box.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(files_box, columns=("size", "subs", "status"),
                                 selectmode="extended", height=8)
        self.tree.heading("#0", text="File", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("subs", text="Subtitles", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("subs", width=170, anchor="w", stretch=False)
        self.tree.column("status", width=110, anchor="w", stretch=False)
        scroll = ttk.Scrollbar(files_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        btns = ttk.Frame(files_box)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.add_files_btn = ttk.Button(btns, text="Add files...", command=self._add_files)
        self.add_folder_btn = ttk.Button(btns, text="Add folder...", command=self._add_folder)
        self.remove_btn = ttk.Button(btns, text="Remove selected", command=self._remove_selected)
        self.clear_btn = ttk.Button(btns, text="Clear all", command=self._clear_all)
        self.attach_sub_btn = ttk.Button(btns, text="Attach subtitles...", command=self._attach_subtitles)
        self.detach_sub_btn = ttk.Button(btns, text="Remove subtitles", command=self._detach_subtitles)
        for button in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn):
            button.pack(side="left", padx=(0, 6))
        self.attach_sub_btn.pack(side="left", padx=(14, 6))
        self.detach_sub_btn.pack(side="left")

        # Settings
        box = ttk.LabelFrame(main, text="Settings", padding=8)
        box.pack(fill="x", pady=4)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Output folder:").grid(row=0, column=0, sticky="w", pady=2)
        self.out_entry = ttk.Entry(box, textvariable=self.out_dir)
        self.out_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6)
        self.out_btn = ttk.Button(box, text="Browse...", command=self._browse_output)
        self.out_btn.grid(row=0, column=4)
        ttk.Label(box, text="Leave empty to save next to each original file",
                  style="Muted.TLabel").grid(row=1, column=1, columnspan=3, sticky="w", padx=6)

        ttk.Label(box, text="Format:").grid(row=2, column=0, sticky="w", pady=4)
        self.fmt_cb = ttk.Combobox(box, textvariable=self.fmt_label, state="readonly", width=24,
                                   values=[f.label for f in FORMATS.values()])
        self.fmt_cb.grid(row=2, column=1, sticky="w", padx=6)
        self.fmt_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_controls())
        ttk.Label(box, text="Quality:").grid(row=2, column=2, sticky="e")
        self.quality_cb = ttk.Combobox(box, textvariable=self.quality, state="readonly",
                                       width=14, values=QUALITY_LEVELS)
        self.quality_cb.grid(row=2, column=3, sticky="w", padx=6)

        ttk.Label(box, text="Method:").grid(row=3, column=0, sticky="w", pady=2)
        method = ttk.Frame(box)
        method.grid(row=3, column=1, sticky="w", padx=6)
        self.reencode_radio = ttk.Radiobutton(method, text="Re-encode (most compatible)",
                                              variable=self.mode, value="reencode",
                                              command=self._refresh_controls)
        self.copy_radio = ttk.Radiobutton(method, text="Fast copy (no re-encode)",
                                          variable=self.mode, value="copy",
                                          command=self._refresh_controls)
        self.reencode_radio.pack(side="left")
        self.copy_radio.pack(side="left", padx=(10, 0))
        ttk.Label(box, text="Speed:").grid(row=3, column=2, sticky="e")
        self.speed_cb = ttk.Combobox(box, textvariable=self.speed, state="readonly",
                                     width=14, values=SPEED_PRESETS)
        self.speed_cb.grid(row=3, column=3, sticky="w", padx=6)

        ttk.Label(box, text="Subtitles:").grid(row=4, column=0, sticky="w", pady=4)
        self.sub_mode_cb = ttk.Combobox(box, textvariable=self.sub_mode_label, state="readonly",
                                        width=24, values=list(SUB_MODES))
        self.sub_mode_cb.grid(row=4, column=1, sticky="w", padx=6)
        self.sub_mode_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_controls())
        ttk.Label(box, text="Resolution:").grid(row=4, column=2, sticky="e")
        self.res_cb = ttk.Combobox(box, textvariable=self.resolution, state="readonly",
                                   width=14, values=list(RESOLUTIONS))
        self.res_cb.grid(row=4, column=3, sticky="w", padx=6)
        self.res_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_controls())
        ttk.Label(box, text="Subtitles are applied to files that have one in the list "
                            "(same-name .srt/.ass/.vtt files are matched automatically)",
                  style="Muted.TLabel").grid(row=5, column=1, columnspan=4, sticky="w", padx=6)

        opts_row = ttk.Frame(box)
        opts_row.grid(row=6, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.overwrite_cb = ttk.Checkbutton(opts_row, text="Overwrite existing files",
                                            variable=self.overwrite)
        self.delete_cb = ttk.Checkbutton(
            opts_row, text="Delete original after successful conversion", variable=self.delete_src)
        self.overwrite_cb.pack(side="left")
        self.delete_cb.pack(side="left", padx=(16, 0))

        # Progress
        prog = ttk.Frame(main)
        prog.pack(fill="x", pady=(8, 0))
        ttk.Label(prog, textvariable=self.status).pack(anchor="w")
        self.file_bar = ttk.Progressbar(prog, maximum=100)
        self.file_bar.pack(fill="x", pady=(4, 2))
        ttk.Label(prog, text="Overall", style="Muted.TLabel").pack(anchor="w")
        self.total_bar = ttk.Progressbar(prog, maximum=100)
        self.total_bar.pack(fill="x", pady=(2, 0))

        # Action buttons
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=8)
        self.start_btn = ttk.Button(actions, text="Convert", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        # Log
        log_box = ttk.LabelFrame(main, text="Log", padding=4)
        log_box.pack(fill="both", expand=True)
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log = tk.Text(log_box, height=7, wrap="word", state="disabled", font="TkFixedFont",
                           borderwidth=0, relief="flat", highlightthickness=1)
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _refresh_controls(self) -> None:
        """Enable/disable widgets depending on the run state and chosen options."""
        fmt = FORMATS[self._selected_ext()]
        audio_only = fmt.video_codec is None
        if fmt.extension not in SOFT_SUBTITLE_FORMATS and SUB_MODES.get(self.sub_mode_label.get()) == "embed":
            self.sub_mode_label.set("Burn into video")
            if not self.running:
                self.status.set(f".{fmt.extension} can't hold a subtitle track - using burn-in instead")
        # Resizing / burning subtitles changes the picture, so 'Fast copy' can't apply.
        needs_reencode = requires_reencode(self._collect_options(), bool(self.subtitles))
        if (audio_only or needs_reencode) and self.mode.get() == "copy":
            self.mode.set("reencode")
        copy = self.mode.get() == "copy"
        idle = not self.running

        def state(enabled: bool, readonly: bool = False) -> str:
            if not (enabled and idle):
                return "disabled"
            return "readonly" if readonly else "normal"

        for widget in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn,
                       self.out_btn, self.out_entry, self.overwrite_cb, self.delete_cb,
                       self.reencode_radio, self.attach_sub_btn, self.detach_sub_btn):
            widget.configure(state=state(True))
        self.fmt_cb.configure(state=state(True, readonly=True))
        self.copy_radio.configure(state=state(not audio_only and not needs_reencode))
        self.sub_mode_cb.configure(state=state(not audio_only, readonly=True))
        self.res_cb.configure(state=state(not audio_only, readonly=True))
        self.quality_cb.configure(state=state(not copy, readonly=True))
        self.speed_cb.configure(state=state(not copy and fmt.video_codec == "libx264", readonly=True))
        self.start_btn.configure(state="normal" if idle else "disabled")
        self.cancel_btn.configure(state="disabled" if idle else "normal")

    # --------------------------------------------------------- file handling -- #

    def _add_files(self) -> None:
        patterns = tuple(p for e in sorted(VIDEO_EXTENSIONS) for p in (f"*{e}", f"*{e.upper()}"))
        paths = filedialog.askopenfilenames(
            title="Select video files",
            filetypes=[("Video files", patterns), ("All files", "*.*")],
        )
        self._add_paths(Path(p) for p in paths)

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder of videos")
        if not folder:
            return
        found = scan_folder(Path(folder))
        if not found:
            messagebox.showinfo("No videos found", "That folder doesn't contain any supported video files.")
            return
        self._add_paths(found)

    def _add_paths(self, paths) -> None:
        existing = set(self.items.values())
        for path in paths:
            if path in existing or not path.is_file():
                continue
            try:
                size = human_size(path.stat().st_size)
            except OSError:
                size = "?"
            match = find_matching_subtitle(path)
            iid = self.tree.insert("", "end", text=path.name,
                                   values=(size, match.name if match else "-", "Queued"))
            self.items[iid] = path
            if match:
                self.subtitles[iid] = match
            existing.add(path)
        self._refresh_controls()

    def _remove_selected(self) -> None:
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.subtitles.pop(iid, None)
            self.tree.delete(iid)
        self._refresh_controls()

    def _clear_all(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.subtitles.clear()
        self._refresh_controls()

    def _attach_subtitles(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Select files", "Select one or more files in the list first, "
                                                "then attach a subtitle file to them.")
            return
        patterns = tuple(p for e in sorted(SUBTITLE_EXTENSIONS) for p in (f"*{e}", f"*{e.upper()}"))
        chosen = filedialog.askopenfilename(
            title="Select subtitle file",
            filetypes=[("Subtitle files", patterns), ("All files", "*.*")])
        if not chosen:
            return
        path = Path(chosen)
        for iid in selection:
            self.subtitles[iid] = path
            self.tree.set(iid, "subs", path.name)
        self._refresh_controls()

    def _detach_subtitles(self) -> None:
        for iid in self.tree.selection():
            if self.subtitles.pop(iid, None) is not None:
                self.tree.set(iid, "subs", "-")
        self._refresh_controls()

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.out_dir.set(folder)

    # ---------------------------------------------------------------- theme -- #

    def _toggle_theme(self) -> None:
        self.theme = "light" if self.theme == "dark" else "dark"
        self._apply_theme()
        self._save_config()

    def _apply_theme(self) -> None:
        """Restyle every widget from the current palette (dark or light)."""
        p = PALETTES[self.theme]
        style = ttk.Style()
        # 'clam' is the one built-in theme that honours custom colours on every OS.
        style.theme_use("clam")
        self.root.configure(bg=p["bg"])

        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["surface"],
                        troughcolor=p["surface"], bordercolor=p["border"], darkcolor=p["bg"],
                        lightcolor=p["bg"], insertcolor=p["fg"], focuscolor=p["accent"],
                        selectbackground=p["select_bg"], selectforeground=p["select_fg"])
        style.map(".", foreground=[("disabled", p["disabled"])])

        # Labels used for hints / ffmpeg status
        style.configure("Muted.TLabel", foreground=p["muted"])
        style.configure("Good.TLabel", foreground=p["good"])
        style.configure("Bad.TLabel", foreground=p["bad"])

        # Buttons: normal and the primary "Convert" button
        for name, base, hover, pressed, fg, off_bg, border in (
            ("TButton", p["button"], p["hover"], p["pressed"], p["fg"], p["bg"], p["border"]),
            ("Accent.TButton", p["accent"], p["accent_hover"], p["accent_dim"], "#ffffff",
             p["button"], p["accent"]),
        ):
            states = [("disabled", off_bg), ("pressed", pressed), ("active", hover)]
            style.configure(name, background=base, foreground=fg, bordercolor=border,
                            lightcolor=base, darkcolor=base, padding=(10, 4))
            style.map(name, background=states, lightcolor=states, darkcolor=states,
                      foreground=[("disabled", p["disabled"])])

        # Text boxes and drop-downs
        style.configure("TEntry", fieldbackground=p["surface"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                        insertcolor=p["fg"], padding=3)
        style.map("TEntry", fieldbackground=[("disabled", p["bg"])],
                  foreground=[("disabled", p["disabled"])],
                  bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])],
                  darkcolor=[("focus", p["accent"])])
        style.configure("TCombobox", fieldbackground=p["surface"], background=p["button"],
                        foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"], padding=3,
                        selectbackground=p["surface"], selectforeground=p["fg"])
        style.map("TCombobox",
                  fieldbackground=[("disabled", p["bg"]), ("readonly", p["surface"])],
                  background=[("disabled", p["bg"]), ("active", p["hover"])],
                  foreground=[("disabled", p["disabled"])],
                  arrowcolor=[("disabled", p["disabled"])],
                  selectbackground=[("readonly", p["surface"])],
                  selectforeground=[("readonly", p["fg"])],
                  bordercolor=[("focus", p["accent"])])

        # Check boxes and radio buttons
        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(name, background=p["bg"], foreground=p["fg"],
                            indicatorbackground=p["surface"], indicatorforeground="#ffffff",
                            upperbordercolor=p["border"], lowerbordercolor=p["border"])
            style.map(name, background=[("active", p["bg"])],
                      foreground=[("disabled", p["disabled"])],
                      indicatorbackground=[("disabled", p["bg"]), ("pressed", p["bg"]),
                                           ("selected", p["accent"])],
                      upperbordercolor=[("disabled", p["disabled"])],
                      lowerbordercolor=[("disabled", p["disabled"])])

        # Containers, progress bars, scrollbars
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        style.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface"],
                        bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"])
        scroll_states = [("active", p["hover"])]
        style.configure("TScrollbar", background=p["button"], troughcolor=p["bg"],
                        bordercolor=p["bg"], arrowcolor=p["fg"], lightcolor=p["button"],
                        darkcolor=p["button"])
        style.map("TScrollbar", background=scroll_states, lightcolor=scroll_states,
                  darkcolor=scroll_states)

        # File list
        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                        foreground=p["fg"], bordercolor=p["border"], lightcolor=p["surface"],
                        darkcolor=p["surface"], rowheight=24)
        style.map("Treeview", background=[("selected", p["select_bg"])],
                  foreground=[("selected", p["select_fg"])])
        style.configure("Treeview.Heading", background=p["button"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["button"], darkcolor=p["button"],
                        relief="flat", padding=(6, 3))
        style.map("Treeview.Heading", background=[("active", p["hover"])])
        for status, key in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=p[key])

        # Plain Tk widgets don't use ttk styles
        self.log.configure(bg=p["surface"], fg=p["fg"], insertbackground=p["fg"],
                           selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                           highlightbackground=p["border"], highlightcolor=p["accent"])
        for combo in (self.fmt_cb, self.quality_cb, self.speed_cb, self.sub_mode_cb, self.res_cb):
            self._style_dropdown_list(combo, p)

        self.theme_btn.configure(text="Light mode" if self.theme == "dark" else "Dark mode")
        self._set_titlebar_dark(self.theme == "dark")

    def _style_dropdown_list(self, combo, p: dict) -> None:
        """The pop-up list of a Combobox is a classic Tk listbox and needs colouring by hand."""
        try:
            popdown = self.root.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
            self.root.tk.call(f"{popdown}.f.l", "configure",
                              "-background", p["surface"], "-foreground", p["fg"],
                              "-selectbackground", p["select_bg"], "-selectforeground", p["select_fg"],
                              "-borderwidth", 0, "-highlightthickness", 0)
        except tk.TclError:
            pass

    def _set_titlebar_dark(self, dark: bool) -> None:
        """Windows 10/11 only: ask the OS for a dark title bar (best effort)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):  # 20 = Windows 10 20H1+/11, 19 = older Windows 10
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    # ---------------------------------------------------------------- ffmpeg -- #

    def _detect_ffmpeg(self, hint: Optional[str] = None) -> None:
        path = find_executable("ffmpeg", hint)
        version = ffmpeg_version(path) if path else None
        if path and version:
            self.ffmpeg_path = path
            self.ffmpeg_label.configure(text=f"{version}  ({path})", style="Good.TLabel")
        else:
            self.ffmpeg_path = None
            self.ffmpeg_label.configure(
                text="not found - install FFmpeg or click 'Locate...'", style="Bad.TLabel")

    def _browse_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(title="Select the ffmpeg executable")
        if path:
            self._detect_ffmpeg(path)
            if not self.ffmpeg_path:
                messagebox.showerror("Not a valid FFmpeg", "That file doesn't look like a working ffmpeg executable.")

    # ------------------------------------------------------------ run control -- #

    def _selected_ext(self) -> str:
        return self._label_to_ext.get(self.fmt_label.get(), "mp4")

    def _collect_options(self) -> ConversionOptions:
        out = self.out_dir.get().strip()
        return ConversionOptions(
            output_dir=Path(out).expanduser() if out else None,
            output_format=self._selected_ext(),
            mode=self.mode.get(),
            quality=self.quality.get(),
            speed=self.speed.get(),
            subtitle_mode=SUB_MODES.get(self.sub_mode_label.get(), "burn"),
            resolution=RESOLUTIONS.get(self.resolution.get()),
            overwrite=self.overwrite.get(),
            delete_source=self.delete_src.get(),
        )

    def _start(self) -> None:
        if self.running:
            return
        if not self.ffmpeg_path:
            messagebox.showerror(
                "FFmpeg not found",
                "FFmpeg is required but wasn't found on your PATH.\n\n"
                "Install it (see the README) or use 'Locate...' to point to the executable.")
            return
        if not self.items:
            messagebox.showinfo("Nothing to convert", "Add some video files first.")
            return
        opts = self._collect_options()
        if opts.delete_source and not messagebox.askyesno(
            "Delete originals?",
            "Original files will be PERMANENTLY deleted after each successful conversion.\n\nContinue?",
            icon="warning",
        ):
            return

        for iid in self.items:
            self.tree.item(iid, tags=())
            self.tree.set(iid, "status", "Queued")
        self.file_bar.configure(mode="determinate", value=0)
        self.total_bar.configure(value=0)
        self._write_log(f"--- Starting {len(self.items)} file(s) ---")
        self._save_config()

        self.converter = Converter(self.ffmpeg_path)
        self.running = True
        self._refresh_controls()
        subs = {self.items[iid]: path for iid, path in self.subtitles.items() if iid in self.items}
        self.worker = threading.Thread(
            target=self._run, args=(self.converter, list(self.items.values()), opts, subs), daemon=True)
        self.worker.start()

    def _run(self, converter: Converter, files, opts: ConversionOptions, subs) -> None:
        """Worker thread: never touches Tk directly, only posts events."""
        put = self.events.put
        try:
            results = converter.convert_batch(
                files, opts,
                on_file_start=lambda i, t, p: put(("file_start", i, t, p)),
                on_progress=lambda frac, speed: put(("progress", frac, speed)),
                on_log=lambda msg: put(("log", msg)),
                on_file_done=lambda i, t, r: put(("file_done", i, t, r)),
                subtitles=subs,
            )
        except Exception as exc:  # keep the UI from hanging on unexpected errors
            put(("log", f"Unexpected error: {exc}"))
            results = []
        put(("finished", results))

    def _cancel(self) -> None:
        if self.converter and self.running:
            self.converter.cancel()
            self.status.set("Cancelling...")
            self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------- event handling -- #

    def _poll_events(self) -> None:
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _iid_for(self, path: Path) -> Optional[str]:
        return next((iid for iid, p in self.items.items() if p == path), None)

    def _stop_indeterminate(self) -> None:
        if str(self.file_bar["mode"]) == "indeterminate":
            self.file_bar.stop()
            self.file_bar.configure(mode="determinate")

    def _on_log(self, message: str) -> None:
        self._write_log(message)

    def _on_file_start(self, index: int, total: int, path: Path) -> None:
        self._idx, self._total, self._name = index, total, path.name
        self._stop_indeterminate()
        self.file_bar["value"] = 0
        self.total_bar["value"] = index / total * 100
        iid = self._iid_for(path)
        if iid:
            self.tree.set(iid, "status", "Converting...")
            self.tree.see(iid)
        self.status.set(f"Converting {index + 1}/{total}: {path.name}")

    def _on_progress(self, fraction: Optional[float], speed: str) -> None:
        detail = ""
        if fraction is None:  # duration unknown -> show a busy bar instead
            if str(self.file_bar["mode"]) != "indeterminate":
                self.file_bar.configure(mode="indeterminate")
                self.file_bar.start(15)
        else:
            self._stop_indeterminate()
            self.file_bar["value"] = fraction * 100
            self.total_bar["value"] = (self._idx + fraction) / max(self._total, 1) * 100
            detail = f" - {fraction * 100:.0f}%"
        if speed and speed != "N/A":
            detail += f" ({speed})"
        self.status.set(f"Converting {self._idx + 1}/{self._total}: {self._name}{detail}")

    def _on_file_done(self, index: int, total: int, result: ConversionResult) -> None:
        self._stop_indeterminate()
        self.file_bar["value"] = 100 if result.status == STATUS_OK else 0
        self.total_bar["value"] = (index + 1) / total * 100
        iid = self._iid_for(result.source)
        if iid:
            self.tree.set(iid, "status", STATUS_TEXT.get(result.status, result.status))
            self.tree.item(iid, tags=(result.status,))
        self._write_log(f"[{STATUS_TEXT.get(result.status, result.status)}] "
                        f"{result.source.name}: {result.message}")

    def _on_finished(self, results: list) -> None:
        self.running = False
        self._stop_indeterminate()
        self._refresh_controls()
        counts = {s: sum(1 for r in results if r.status == s) for s in STATUS_TEXT}
        summary = (f"Finished - {counts[STATUS_OK]} converted, {counts[STATUS_SKIPPED]} skipped, "
                   f"{counts[STATUS_FAILED]} failed, {counts[STATUS_CANCELLED]} cancelled")
        self.status.set(summary)
        self._write_log(summary)
        if counts[STATUS_FAILED]:
            messagebox.showwarning("Finished with errors", summary + "\n\nCheck the log for details.")
        elif counts[STATUS_CANCELLED]:
            pass
        else:
            messagebox.showinfo("All done", summary)

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # --------------------------------------------------------------- config -- #

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("output_dir"), str):
            self.out_dir.set(data["output_dir"])
        if data.get("format") in FORMATS:
            self.fmt_label.set(FORMATS[data["format"]].label)
        if data.get("quality") in QUALITY_LEVELS:
            self.quality.set(data["quality"])
        if data.get("speed") in SPEED_PRESETS:
            self.speed.set(data["speed"])
        if data.get("mode") in ("reencode", "copy"):
            self.mode.set(data["mode"])
        for label, value in SUB_MODES.items():
            if data.get("subtitle_mode") == value:
                self.sub_mode_label.set(label)
        if data.get("resolution") in RESOLUTIONS:
            self.resolution.set(data["resolution"])
        if data.get("theme") in PALETTES:
            self.theme = data["theme"]
        self.overwrite.set(bool(data.get("overwrite", False)))
        if isinstance(data.get("ffmpeg"), str):
            self._saved_ffmpeg = data["ffmpeg"]
        # "delete original" is deliberately never remembered - it must be a conscious choice.

    def _save_config(self) -> None:
        data = {
            "output_dir": self.out_dir.get().strip(),
            "format": self._selected_ext(),
            "quality": self.quality.get(),
            "speed": self.speed.get(),
            "mode": self.mode.get(),
            "subtitle_mode": SUB_MODES.get(self.sub_mode_label.get(), "burn"),
            "resolution": self.resolution.get(),
            "theme": self.theme,
            "overwrite": self.overwrite.get(),
            "ffmpeg": self.ffmpeg_path or "",
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("Quit", "A conversion is still running. Cancel it and quit?"):
                return
            if self.converter:
                self.converter.cancel()
            if self.worker:
                self.worker.join(timeout=5)  # lets the worker clean up partial files
        self._save_config()
        self.root.destroy()


def _enable_hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _set_windows_app_id() -> None:
    """Make Windows show VYMAX's own icon in the taskbar instead of Python's."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vymax.app")
        except Exception:
            pass


def main() -> None:
    _set_windows_app_id()
    _enable_hidpi()
    root = tk.Tk()
    app = App(root)  # keep a reference for the lifetime of the window
    root.mainloop()


if __name__ == "__main__":
    main()
