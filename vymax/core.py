"""Conversion engine.

Nothing in this module touches the GUI, so it can be unit-tested and reused
from scripts or a future command-line interface.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VIDEO_EXTENSIONS = frozenset(
    {
        ".avi", ".mkv", ".mov", ".mp4", ".m4v", ".mpg", ".mpeg", ".ts",
        ".mts", ".m2ts", ".wmv", ".flv", ".webm", ".3gp", ".vob", ".ogv",
    }
)

QUALITY_LEVELS = ("High", "Balanced", "Small file")
SPEED_PRESETS = ("ultrafast", "veryfast", "fast", "medium", "slow")

SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt"})
# Output heights offered in the GUI (None = keep the original size).
RESOLUTIONS = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
# Containers that can hold a selectable (soft) subtitle track.
SOFT_SUBTITLE_FORMATS = frozenset({"mp4", "mov", "mkv", "webm"})

_CRF_H264 = {"High": 18, "Balanced": 23, "Small file": 28}
_VP9_CRF_OFFSET = 8  # VP9's CRF scale runs higher than x264's for similar quality
_AUDIO_KBPS = {"High": 256, "Balanced": 192, "Small file": 128}


@dataclass(frozen=True)
class OutputFormat:
    extension: str
    label: str
    video_codec: Optional[str]  # None means audio-only output
    audio_codec: str
    container_args: tuple = ()


FORMATS = {
    "mp4": OutputFormat("mp4", "MP4 (H.264 + AAC)", "libx264", "aac", ("-movflags", "+faststart")),
    "mkv": OutputFormat("mkv", "MKV (H.264 + AAC)", "libx264", "aac"),
    "mov": OutputFormat("mov", "MOV (H.264 + AAC)", "libx264", "aac", ("-movflags", "+faststart")),
    "webm": OutputFormat("webm", "WebM (VP9 + Opus)", "libvpx-vp9", "libopus"),
    "avi": OutputFormat("avi", "AVI (H.264 + MP3)", "libx264", "libmp3lame"),
    "mp3": OutputFormat("mp3", "MP3 (audio only)", None, "libmp3lame"),
}

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Keys ffmpeg prints with `-progress`; anything else is a normal log line.
_PROGRESS_LINE = re.compile(
    r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|"
    r"out_time|dup_frames|drop_frames|speed|progress)=(.*)$"
)

ProgressCallback = Callable[[Optional[float], str], None]
LogCallback = Callable[[str], None]


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class ConversionOptions:
    output_dir: Optional[Path] = None  # None -> next to each source file
    output_format: str = "mp4"
    mode: str = "reencode"  # "reencode" or "copy" (fast remux, no quality loss)
    quality: str = "Balanced"
    speed: str = "medium"
    subtitle_mode: str = "burn"  # "burn" (hard-coded into the picture) or "embed" (selectable track)
    resolution: Optional[int] = None  # target height in pixels, e.g. 720; None keeps the original
    overwrite: bool = False
    delete_source: bool = False


@dataclass
class ConversionResult:
    source: Path
    output: Optional[Path]
    status: str
    message: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _subprocess_kwargs() -> dict:
    """Stop a console window flashing up on Windows."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def find_executable(name: str, hint: Optional[str] = None) -> Optional[str]:
    """Return a usable path for *name*, preferring an explicit *hint* file."""
    if hint:
        candidate = Path(hint).expanduser()
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def ffmpeg_version(ffmpeg: str) -> Optional[str]:
    """Return e.g. 'ffmpeg version 6.1', or None if the binary doesn't work."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, **_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return " ".join(proc.stdout.splitlines()[0].split()[:3])


def _sibling_probe(ffmpeg: str) -> Optional[str]:
    path = Path(ffmpeg)
    candidate = path.with_name("ffprobe" + path.suffix)
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ffprobe")


def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:
    """True if this FFmpeg build includes the named filter (e.g. 'subtitles')."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, **_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


def find_matching_subtitle(video: Path) -> Optional[Path]:
    """Find a subtitle next to *video*: 'movie.srt' or a language-tagged 'movie.en.srt'."""
    video = Path(video)
    try:
        entries = sorted(video.parent.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None
    stem = video.stem.lower()
    tagged = None
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        other = path.stem.lower()
        if other == stem:
            return path
        if tagged is None and other.startswith(stem + "."):
            tagged = path
    return tagged


def requires_reencode(opts: ConversionOptions, has_subtitle: bool = False) -> bool:
    """Resizing and burning subtitles change the picture, so 'Fast copy' can't be used."""
    if FORMATS[opts.output_format].video_codec is None:
        return False
    return bool(opts.resolution) or (has_subtitle and opts.subtitle_mode == "burn")


def _soft_subtitle_codec(output_format: str, subtitle: Path) -> Optional[str]:
    if output_format in ("mp4", "mov"):
        return "mov_text"
    if output_format == "webm":
        return "webvtt"
    if output_format == "mkv":
        return "ass" if Path(subtitle).suffix.lower() in (".ass", ".ssa") else "srt"
    return None


def scan_folder(folder: Path) -> list[Path]:
    """Video files directly inside *folder*, sorted by name."""
    files = (
        p for p in Path(folder).iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    return sorted(files, key=lambda p: p.name.lower())


def human_size(num: float) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def output_path_for(src: Path, opts: ConversionOptions) -> Path:
    folder = Path(opts.output_dir) if opts.output_dir else Path(src).parent
    return folder / f"{Path(src).stem}.{opts.output_format}"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return False


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _parse_clock(value: str) -> Optional[float]:
    """'00:01:02.500000' -> 62.5 (None for 'N/A' or garbage)."""
    try:
        hours, minutes, seconds = value.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def build_command(
    ffmpeg: str, src: Path, dst: Path, opts: ConversionOptions, subtitle: Optional[Path] = None,
) -> list[str]:
    """Assemble the ffmpeg command line for one file.

    When burning subtitles, ffmpeg must run with the subtitle file in its working
    directory: the filter then only needs the bare file name, which avoids the
    notoriously fiddly path escaping (Windows drive letters, spaces, quotes...).
    """
    fmt = FORMATS[opts.output_format]
    audio_only = fmt.video_codec is None
    burn = subtitle is not None and opts.subtitle_mode == "burn" and not audio_only
    embed = subtitle is not None and opts.subtitle_mode == "embed" and not audio_only

    filters: list[str] = []
    if burn:
        filters.append(f"subtitles={Path(subtitle).name}")
    if opts.resolution and not audio_only:
        # -2 keeps the width even; min() means small videos are never upscaled.
        filters.append(f"scale=-2:min({int(opts.resolution)}\\,ih)")
    copy = opts.mode == "copy" and not filters

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y", "-i", str(src)]
    if embed:
        cmd += ["-i", str(subtitle)]

    if audio_only:
        cmd += ["-vn", "-c:a", fmt.audio_codec, "-b:a", f"{_AUDIO_KBPS[opts.quality]}k"]
    else:
        cmd += ["-map", "0:v?", "-map", "0:a?"]
        if embed:
            cmd += ["-map", "1:0"]
        if copy:
            cmd += ["-c", "copy"]
        else:
            cmd += ["-c:v", fmt.video_codec]
            if filters:
                cmd += ["-vf", ",".join(filters)]
            if fmt.video_codec == "libx264":
                cmd += ["-crf", str(_CRF_H264[opts.quality]), "-preset", opts.speed,
                        "-pix_fmt", "yuv420p"]
            elif fmt.video_codec == "libvpx-vp9":
                cmd += ["-crf", str(_CRF_H264[opts.quality] + _VP9_CRF_OFFSET),
                        "-b:v", "0", "-row-mt", "1"]
            cmd += ["-c:a", fmt.audio_codec, "-b:a", f"{_AUDIO_KBPS[opts.quality]}k"]
        if embed:
            cmd += ["-c:s", _soft_subtitle_codec(opts.output_format, subtitle) or "copy"]

    cmd += list(fmt.container_args)
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #

class Converter:
    """Runs ffmpeg for one file or a batch. Create a new instance per run."""

    def __init__(self, ffmpeg: str, ffprobe: Optional[str] = None):
        # Absolute, because burning subtitles runs ffmpeg from a different working directory.
        self.ffmpeg = os.path.abspath(ffmpeg) if os.path.dirname(ffmpeg) else ffmpeg
        self.ffprobe = ffprobe or _sibling_probe(self.ffmpeg)
        self._burn_supported: Optional[bool] = None
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # -- control ----------------------------------------------------------- #

    def cancel(self) -> None:
        """Stop the current file and skip everything still queued."""
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def supports_subtitle_burn(self) -> bool:
        if self._burn_supported is None:
            self._burn_supported = ffmpeg_has_filter(self.ffmpeg, "subtitles")
        return self._burn_supported

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- probing ----------------------------------------------------------- #

    def probe_duration(self, src: Path) -> Optional[float]:
        if not self.ffprobe:
            return None
        try:
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, **_subprocess_kwargs(),
            )
            return float(proc.stdout.strip()) if proc.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    # -- conversion -------------------------------------------------------- #

    def convert_file(
        self,
        src: Path,
        opts: ConversionOptions,
        on_progress: Optional[ProgressCallback] = None,
        on_log: Optional[LogCallback] = None,
        subtitle: Optional[Path] = None,
    ) -> ConversionResult:
        src = Path(src)
        log = on_log or (lambda _msg: None)
        progress = on_progress or (lambda _frac, _speed: None)
        dst = output_path_for(src, opts)

        if not src.is_file():
            return ConversionResult(src, None, STATUS_FAILED, "Source file not found")
        if _same_file(src, dst):
            return ConversionResult(src, None, STATUS_SKIPPED,
                                    "Input and output are the same file - pick another folder or format")
        if dst.exists() and not opts.overwrite:
            return ConversionResult(src, dst, STATUS_SKIPPED,
                                    "Output already exists (enable 'Overwrite' to replace it)")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ConversionResult(src, None, STATUS_FAILED, f"Cannot create output folder: {exc}")

        fmt = FORMATS[opts.output_format]
        use_subtitle = subtitle is not None and fmt.video_codec is not None
        if subtitle is not None and not Path(subtitle).is_file():
            return ConversionResult(src, None, STATUS_FAILED, f"Subtitle file not found: {subtitle}")
        if use_subtitle and opts.subtitle_mode == "embed" and opts.output_format not in SOFT_SUBTITLE_FORMATS:
            return ConversionResult(
                src, None, STATUS_FAILED,
                f".{opts.output_format} can't hold a subtitle track - use 'Burn into video' or MP4/MKV/MOV/WebM")
        if use_subtitle and opts.subtitle_mode == "burn" and not self.supports_subtitle_burn():
            return ConversionResult(
                src, None, STATUS_FAILED,
                "This FFmpeg build has no 'subtitles' filter (libass). Install a full FFmpeg build "
                "or use 'Embed as track'")
        if opts.mode == "copy" and requires_reencode(opts, use_subtitle):
            log("Note: Fast copy skipped - resizing / burning subtitles needs re-encoding")

        # Encode to a temp name first so a crash/cancel never leaves a broken file
        # that looks like a finished conversion.
        tmp = dst.with_name(f"{dst.stem}.converting{dst.suffix}")
        duration = self.probe_duration(src)

        workdir: Optional[Path] = None
        run_subtitle = Path(os.path.abspath(subtitle)) if use_subtitle else None
        if use_subtitle and opts.subtitle_mode == "burn":
            # Copy to a scratch folder under a plain name; ffmpeg runs from there.
            try:
                workdir = Path(tempfile.mkdtemp(prefix="vymax_sub_"))
                run_subtitle = workdir / f"subtitle{Path(subtitle).suffix.lower()}"
                shutil.copyfile(subtitle, run_subtitle)
            except OSError as exc:
                if workdir:
                    shutil.rmtree(workdir, ignore_errors=True)
                return ConversionResult(src, None, STATUS_FAILED, f"Could not prepare subtitle file: {exc}")

        cmd = build_command(
            self.ffmpeg, Path(os.path.abspath(src)), Path(os.path.abspath(tmp)), opts, run_subtitle)
        log("$ " + shlex.join(cmd))

        returncode = -1
        try:
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(workdir) if workdir else None, **_subprocess_kwargs(),
            ) as proc:
                with self._lock:
                    self._proc = proc
                if self._cancel.is_set():  # cancel() raced with process start-up
                    proc.terminate()

                seconds: Optional[float] = None
                speed = ""
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    match = _PROGRESS_LINE.match(line)
                    if not match:
                        log(line)
                        continue
                    key, value = match.groups()
                    if key == "out_time":
                        seconds = _parse_clock(value)
                    elif key == "speed":
                        speed = value.strip()
                    elif key == "progress":  # end of one progress block
                        fraction = None
                        if seconds is not None and duration:
                            fraction = min(max(seconds / duration, 0.0), 1.0)
                        progress(fraction, speed)
                returncode = proc.wait()
        except OSError as exc:
            _remove_quietly(tmp)
            return ConversionResult(src, None, STATUS_FAILED, f"Could not start ffmpeg: {exc}")
        finally:
            with self._lock:
                self._proc = None
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

        if self._cancel.is_set():
            _remove_quietly(tmp)
            return ConversionResult(src, None, STATUS_CANCELLED, "Cancelled")
        if returncode != 0:
            _remove_quietly(tmp)
            return ConversionResult(src, None, STATUS_FAILED,
                                    f"ffmpeg exited with code {returncode} (see log)")
        try:
            if not tmp.exists() or tmp.stat().st_size == 0:
                _remove_quietly(tmp)
                return ConversionResult(src, None, STATUS_FAILED, "ffmpeg produced an empty file")
            os.replace(tmp, dst)
        except OSError as exc:
            _remove_quietly(tmp)
            return ConversionResult(src, None, STATUS_FAILED, f"Could not save output: {exc}")

        message = "Converted"
        if use_subtitle:
            message += ", subtitles burned in" if opts.subtitle_mode == "burn" else ", subtitles embedded"
        if opts.delete_source:  # only reached after a verified successful conversion
            try:
                src.unlink()
                message += " - original deleted"
            except OSError as exc:
                message += f" - could not delete original ({exc})"
        return ConversionResult(src, dst, STATUS_OK, message)

    def convert_batch(
        self,
        files: Sequence[Path],
        opts: ConversionOptions,
        on_file_start: Optional[Callable[[int, int, Path], None]] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_log: Optional[LogCallback] = None,
        on_file_done: Optional[Callable[[int, int, ConversionResult], None]] = None,
        subtitles: Optional[dict] = None,  # {video Path: subtitle Path}
    ) -> list[ConversionResult]:
        results: list[ConversionResult] = []
        total = len(files)
        for index, path in enumerate(files):
            path = Path(path)
            if self._cancel.is_set():
                result = ConversionResult(path, None, STATUS_CANCELLED, "Cancelled")
            else:
                if on_file_start:
                    on_file_start(index, total, path)
                result = self.convert_file(path, opts, on_progress, on_log, (subtitles or {}).get(path))
            results.append(result)
            if on_file_done:
                on_file_done(index, total, result)
        return results
