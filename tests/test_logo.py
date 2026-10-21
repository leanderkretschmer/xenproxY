"""The marks are cells, not glyphs - and every shipped file holds up."""
import glob
import os
import re
import sys
import unittest

os.environ["COLORTERM"] = "truecolor"
os.environ["TERM"] = "xterm-256color"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xenproxy.theme import Theme  # noqa: E402
from xenproxy.ui import logo  # noqa: E402
from xenproxy.ui.term import vlen  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ArtFiles(unittest.TestCase):
    def test_every_file_is_a_rectangle_of_known_inks(self):
        files = glob.glob(os.path.join(logo.ART_DIR, "*.art"))
        self.assertTrue(files)
        for path in files:
            name, width = os.path.basename(path)[:-4].rsplit("-", 1)
            art = logo._load(name, int(width))
            self.assertIsNotNone(art, path)
            palette, grid = art
            self.assertEqual({len(r) for r in grid}, {int(width)}, path)
            inks = {ch for row in grid for ch in row if ch != "."}
            self.assertTrue(all(0 <= logo.DIGITS.find(c) < len(palette)
                                for c in inks), path)
            self.assertTrue(inks, path)


class Rendering(unittest.TestCase):
    th = Theme("tokyo-night")

    def test_marks_are_background_only(self):
        for name in ("proxmox", "xen", "incus", "xenproxy"):
            for w in logo.VARIANTS:
                rows = logo.render(name, self.th, w)
                if not rows:
                    continue
                for row in rows:
                    self.assertEqual(set(ANSI.sub("", row)), {" "}, name)
                    self.assertEqual(vlen(row), w, (name, w))

    def test_banner_fits_and_is_glyph_free(self):
        for width in (320, 200, 160, 112, 80):
            rows = logo.banner(self.th, width, 14)
            self.assertTrue(rows, width)
            for row in rows:
                self.assertLessEqual(vlen(row), width)
                self.assertEqual(set(ANSI.sub("", row)) - {" "}, set())

    def test_runs_share_an_escape(self):
        rows = logo.render("proxmox", self.th, 48)
        # two inks and nothing: a row is a handful of runs, not 48 cells
        self.assertLess(max(row.count("\x1b[48") for row in rows), 16)

    def test_monochrome_falls_back_to_shades(self):
        th = Theme("tokyo-night")
        th.depth = 0
        rows = logo.render("proxmox", th, 24)
        self.assertTrue(any(set(r) - {" "} for r in rows))
        self.assertFalse(any("\x1b" in r for r in rows))


if __name__ == "__main__":
    unittest.main()
