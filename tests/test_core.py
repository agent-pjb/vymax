import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from vymax.core import (
    STATUS_CANCELLED, STATUS_FAILED, STATUS_OK, STATUS_SKIPPED,
    ConversionOptions, Converter, _parse_clock, build_command, find_matching_subtitle,
    human_size, output_path_for, requires_reencode, scan_folder,
)


class CommandTests(unittest.TestCase):
    def test_mp4_reencode(self):
        cmd = build_command("ffmpeg", Path("a.avi"), Path("a.mp4"), ConversionOptions())
        self.assertIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[-1], "a.mp4")

    def test_copy_mode_has_no_encoder(self):
        cmd = build_command("ffmpeg", Path("a.ts"), Path("a.mkv"),
                            ConversionOptions(output_format="mkv", mode="copy"))
        self.assertIn("copy", cmd)
        self.assertNotIn("libx264", cmd)

    def test_audio_only(self):
        cmd = build_command("ffmpeg", Path("a.avi"), Path("a.mp3"),
                            ConversionOptions(output_format="mp3"))
        self.assertIn("-vn", cmd)
        self.assertIn("libmp3lame", cmd)

    def test_webm_uses_vp9_crf_offset(self):
        cmd = build_command("ffmpeg", Path("a.avi"), Path("a.webm"),
                            ConversionOptions(output_format="webm"))
        self.assertIn("libvpx-vp9", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "31")


class SubtitleCommandTests(unittest.TestCase):
    def test_burn_in_with_720p_matches_original_recipe(self):
        opts = ConversionOptions(resolution=720)
        cmd = build_command("ffmpeg", Path("a.mp4"), Path("b.mp4"), opts, Path("/x/subtitle.srt"))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertEqual(vf, "subtitles=subtitle.srt,scale=-2:min(720\\,ih)")
        self.assertIn("libx264", cmd)

    def test_burn_in_without_resize_has_no_scale(self):
        cmd = build_command("ffmpeg", Path("a.mp4"), Path("b.mp4"), ConversionOptions(), Path("s.srt"))
        self.assertEqual(cmd[cmd.index("-vf") + 1], "subtitles=s.srt")

    def test_embed_adds_second_input_and_codec(self):
        opts = ConversionOptions(subtitle_mode="embed")
        cmd = build_command("ffmpeg", Path("a.avi"), Path("b.mp4"), opts, Path("s.srt"))
        self.assertEqual(cmd.count("-i"), 2)
        self.assertIn("1:0", cmd)
        self.assertEqual(cmd[cmd.index("-c:s") + 1], "mov_text")
        self.assertNotIn("-vf", cmd)

    def test_embed_codec_per_container(self):
        for fmt, codec in (("mkv", "srt"), ("webm", "webvtt")):
            cmd = build_command("ffmpeg", Path("a"), Path("b"),
                                ConversionOptions(output_format=fmt, subtitle_mode="embed"), Path("s.srt"))
            self.assertEqual(cmd[cmd.index("-c:s") + 1], codec)
        cmd = build_command("ffmpeg", Path("a"), Path("b"),
                            ConversionOptions(output_format="mkv", subtitle_mode="embed"), Path("s.ass"))
        self.assertEqual(cmd[cmd.index("-c:s") + 1], "ass")

    def test_embed_still_allows_fast_copy(self):
        opts = ConversionOptions(mode="copy", subtitle_mode="embed")
        cmd = build_command("ffmpeg", Path("a.ts"), Path("b.mp4"), opts, Path("s.srt"))
        self.assertIn("copy", cmd)
        self.assertNotIn("libx264", cmd)

    def test_burn_or_resize_overrides_fast_copy(self):
        opts = ConversionOptions(mode="copy", resolution=480)
        cmd = build_command("ffmpeg", Path("a.ts"), Path("b.mp4"), opts)
        self.assertIn("libx264", cmd)
        self.assertTrue(requires_reencode(opts))
        self.assertTrue(requires_reencode(ConversionOptions(mode="copy"), has_subtitle=True))
        self.assertFalse(requires_reencode(ConversionOptions(mode="copy", subtitle_mode="embed"), True))

    def test_audio_only_ignores_subtitles_and_resize(self):
        opts = ConversionOptions(output_format="mp3", resolution=720)
        cmd = build_command("ffmpeg", Path("a.mp4"), Path("b.mp3"), opts, Path("s.srt"))
        self.assertNotIn("-vf", cmd)
        self.assertEqual(cmd.count("-i"), 1)

    def test_find_matching_subtitle(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "Movie.mkv").write_bytes(b"x")
            self.assertIsNone(find_matching_subtitle(d / "Movie.mkv"))
            (d / "Movie.en.srt").write_text("x")
            self.assertEqual(find_matching_subtitle(d / "Movie.mkv").name, "Movie.en.srt")
            (d / "movie.SRT").write_text("x")  # exact (case-insensitive) name wins over language-tagged
            self.assertEqual(find_matching_subtitle(d / "Movie.mkv").name.lower(), "movie.srt")
            (d / "Other Movie.srt").write_text("x")
            self.assertIsNone(find_matching_subtitle(d / "Other.mkv"))


class HelperTests(unittest.TestCase):
    def test_output_path(self):
        self.assertEqual(output_path_for(Path("/v/a.avi"), ConversionOptions()), Path("/v/a.mp4"))
        self.assertEqual(
            output_path_for(Path("/v/a.avi"), ConversionOptions(output_dir=Path("/out"), output_format="mkv")),
            Path("/out/a.mkv"))

    def test_parse_clock(self):
        self.assertEqual(_parse_clock("00:01:02.500000"), 62.5)
        self.assertIsNone(_parse_clock("N/A"))

    def test_human_size(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1536), "1.5 KB")

    def test_scan_folder(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("b.AVI", "a.mp4", "notes.txt"):
                (Path(d) / name).write_bytes(b"x")
            self.assertEqual([p.name for p in scan_folder(Path(d))], ["a.mp4", "b.AVI"])


@unittest.skipIf(os.name == "nt", "uses a POSIX shell script as a fake ffmpeg")
class FakeFfmpegTests(unittest.TestCase):
    """Exercises the conversion workflow without needing a real FFmpeg."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "clip.avi"
        self.src.write_bytes(b"fake video")

    def _converter(self, exit_code=0):
        script = self.tmp / "fake_ffmpeg.sh"
        script.write_text(
            "#!/bin/sh\n"
            'for last; do :; done\n'
            "echo out_time=00:00:01.000000\necho speed=2.0x\necho progress=end\n"
            f'if [ {exit_code} -eq 0 ]; then echo data > "$last"; else echo boom >&2; fi\n'
            f"exit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        conv = Converter(str(script))
        conv.ffprobe = None  # no duration -> progress fraction is None
        return conv

    def test_success_keeps_original_by_default(self):
        result = self._converter().convert_file(self.src, ConversionOptions())
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue((self.tmp / "clip.mp4").exists())
        self.assertTrue(self.src.exists())
        self.assertFalse((self.tmp / "clip.converting.mp4").exists())

    def test_delete_source_after_success(self):
        result = self._converter().convert_file(self.src, ConversionOptions(delete_source=True))
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(self.src.exists())

    def test_failure_never_deletes_source(self):
        result = self._converter(exit_code=1).convert_file(
            self.src, ConversionOptions(delete_source=True))
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(self.src.exists())
        self.assertFalse((self.tmp / "clip.mp4").exists())
        self.assertFalse((self.tmp / "clip.converting.mp4").exists())

    def test_existing_output_is_skipped_unless_overwrite(self):
        (self.tmp / "clip.mp4").write_bytes(b"old")
        conv = self._converter()
        self.assertEqual(conv.convert_file(self.src, ConversionOptions()).status, STATUS_SKIPPED)
        self.assertEqual((self.tmp / "clip.mp4").read_bytes(), b"old")
        self.assertEqual(conv.convert_file(self.src, ConversionOptions(overwrite=True)).status, STATUS_OK)

    def test_same_input_and_output_is_skipped(self):
        mp4 = self.tmp / "same.mp4"
        mp4.write_bytes(b"x")
        result = self._converter().convert_file(mp4, ConversionOptions(delete_source=True))
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(mp4.exists())

    def test_progress_and_log_callbacks(self):
        seen = []
        self._converter().convert_file(self.src, ConversionOptions(),
                                       on_progress=lambda f, s: seen.append((f, s)))
        self.assertEqual(seen, [(None, "2.0x")])

    def test_missing_subtitle_file_fails_without_touching_source(self):
        result = self._converter().convert_file(
            self.src, ConversionOptions(delete_source=True), subtitle=self.tmp / "nope.srt")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(self.src.exists())

    def test_embed_into_avi_is_rejected(self):
        sub = self.tmp / "s.srt"
        sub.write_text("x")
        mkv = self.tmp / "clip2.mkv"
        mkv.write_bytes(b"fake video")
        result = self._converter().convert_file(
            mkv, ConversionOptions(output_format="avi", subtitle_mode="embed"), subtitle=sub)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("subtitle track", result.message)

    def test_burn_with_fake_ffmpeg_cleans_up_scratch_folder(self):
        import glob, tempfile as tf
        sub = self.tmp / "s.srt"
        sub.write_text("x")
        conv = self._converter()
        conv._burn_supported = True  # the fake binary has no real filter list
        before = set(glob.glob(os.path.join(tf.gettempdir(), "vymax_sub_*")))
        result = conv.convert_file(self.src, ConversionOptions(resolution=720), subtitle=sub)
        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("subtitles burned in", result.message)
        self.assertEqual(set(glob.glob(os.path.join(tf.gettempdir(), "vymax_sub_*"))), before)

    def test_burn_without_libass_gives_clear_error(self):
        sub = self.tmp / "s.srt"
        sub.write_text("x")
        conv = self._converter()
        conv._burn_supported = False
        result = conv.convert_file(self.src, ConversionOptions(), subtitle=sub)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("libass", result.message)

    def test_cancelled_batch_skips_everything(self):
        conv = self._converter()
        conv.cancel()
        results = conv.convert_batch([self.src], ConversionOptions())
        self.assertEqual(results[0].status, STATUS_CANCELLED)
        self.assertTrue(self.src.exists())


@unittest.skipUnless(shutil.which("ffmpeg"), "real ffmpeg not installed")
class RealFfmpegTests(unittest.TestCase):
    def test_real_conversion_with_progress(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.avi"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
                 "-f", "lavfi", "-i", "sine=duration=2", "-shortest", str(src)], check=True)
            fractions = []
            conv = Converter(shutil.which("ffmpeg"))
            result = conv.convert_file(src, ConversionOptions(output_dir=Path(d) / "out", speed="ultrafast"),
                                       on_progress=lambda f, s: fractions.append(f))
            self.assertEqual(result.status, STATUS_OK, result.message)
            self.assertGreater(result.output.stat().st_size, 0)
            self.assertTrue(fractions and fractions[-1] is not None)


def _has_filter(name):
    from vymax.core import ffmpeg_has_filter
    return bool(shutil.which("ffmpeg")) and ffmpeg_has_filter(shutil.which("ffmpeg"), name)


@unittest.skipUnless(_has_filter("subtitles"), "ffmpeg with libass not installed")
class RealSubtitleTests(unittest.TestCase):
    def test_burn_in_and_resize_with_awkward_filename(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "in.avi"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=duration=1:size=640x480:rate=15", "-an", str(src)], check=True)
            sub = d / "it's [a], v2: final.srt"
            sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            opts = ConversionOptions(output_dir=d / "out", resolution=360, speed="ultrafast")
            result = Converter(shutil.which("ffmpeg")).convert_file(src, opts, subtitle=sub)
            self.assertEqual(result.status, STATUS_OK, result.message)
            height = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                 "-of", "csv=p=0", str(result.output)], capture_output=True, text=True).stdout.strip()
            self.assertEqual(height, "360")


if __name__ == "__main__":
    unittest.main()
