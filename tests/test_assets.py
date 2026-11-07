import struct
import unittest
from pathlib import Path

import vymax

ASSETS = Path(vymax.__file__).resolve().parent / "assets"


def png_size(path: Path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


class AssetTests(unittest.TestCase):
    def test_logo_and_icon_pngs_are_square(self):
        for name in ("logo.png", "icon.png"):
            width, height = png_size(ASSETS / name)
            self.assertEqual(width, height, name)
            self.assertGreaterEqual(width, 256, name)

    def test_window_icon_pngs_exist_in_several_sizes(self):
        for size in (16, 32, 48, 64, 128):
            self.assertEqual(png_size(ASSETS / f"icon_{size}.png"), (size, size))

    def test_windows_icon_has_multiple_sizes(self):
        data = (ASSETS / "icon.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))  # valid .ico header
        self.assertGreaterEqual(count, 4)

    def test_small_ico_frames_are_classic_bitmaps(self):
        """Only the 256px frame may be PNG; Tk on some Windows versions can't read PNG frames."""
        data = (ASSETS / "icon.ico").read_bytes()
        count = struct.unpack("<H", data[4:6])[0]
        for i in range(count):
            width, _h, _c, _r, _planes, _bpp, size, offset = struct.unpack(
                "<BBBBHHII", data[6 + 16 * i: 22 + 16 * i])
            frame = data[offset: offset + size]
            if width == 0:  # 0 means 256 pixels
                self.assertEqual(frame[:8], b"\x89PNG\r\n\x1a\n")
            else:
                self.assertEqual(struct.unpack("<I", frame[:4])[0], 40, f"{width}px frame is not a DIB")

    def test_macos_icon_header(self):
        self.assertEqual((ASSETS / "icon.icns").read_bytes()[:4], b"icns")

    def test_version_is_1_0_0(self):
        self.assertEqual(vymax.__version__, "1.0.0")


if __name__ == "__main__":
    unittest.main()
