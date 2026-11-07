import re
import unittest

from vymax.theme import DEFAULT_THEME, PALETTES

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _luminance(colour: str) -> float:
    """Relative luminance as defined by WCAG (includes the sRGB gamma step)."""
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(int(colour[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class PaletteTests(unittest.TestCase):
    def test_both_themes_define_the_same_keys(self):
        self.assertEqual(set(PALETTES["dark"]), set(PALETTES["light"]))

    def test_all_colours_are_valid_hex(self):
        for name, palette in PALETTES.items():
            for key, value in palette.items():
                self.assertRegex(value, HEX, f"{name}.{key}")

    def test_default_theme_exists_and_is_dark(self):
        self.assertIn(DEFAULT_THEME, PALETTES)
        self.assertEqual(DEFAULT_THEME, "dark")

    def test_text_is_readable_on_its_backgrounds(self):
        # WCAG AA for normal text is 4.5:1
        for name, p in PALETTES.items():
            pairs = [("fg", "bg"), ("fg", "surface"), ("select_fg", "select_bg")]
            pairs += [(c, b) for c in ("muted", "good", "warn", "bad") for b in ("bg", "surface")]
            for fg, bg in pairs:
                self.assertGreaterEqual(_contrast(p[fg], p[bg]), 4.5, f"{name}: {fg} on {bg}")
            self.assertGreaterEqual(_contrast("#ffffff", p["accent"]), 3.0, f"{name}: accent button")

    def test_dark_is_actually_dark(self):
        self.assertLess(_luminance(PALETTES["dark"]["bg"]), 0.2)
        self.assertGreater(_luminance(PALETTES["light"]["bg"]), 0.8)


if __name__ == "__main__":
    unittest.main()
