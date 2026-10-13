"""Run with:  python test_backend.py   (or pytest). Needs ffmpeg on PATH."""
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend


def make_clip(path: Path, fps: str, seconds: float = 6) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=320x180:rate={fps}", "-t", str(seconds),
         "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p", str(path)],
        check=True)


def ms(frames):
    """Timestamps at the precision used in filenames."""
    return [round(f.t, 3) for f in frames]


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytfg_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # force slicing even on these tiny clips
        p = mock.patch.object(backend, "MIN_CHUNK_FRAMES", 10)
        p.start()
        self.addCleanup(p.stop)

    def run_extract(self, src, ranges, workers, expected=0, tag=""):
        out = self.tmp / f"out{workers}{tag}"
        th = self.tmp / f"th{workers}{tag}"
        frames = backend.extract_ranges(src, ranges, out, th, expected,
                                        lambda *_: None, threading.Event(), workers)
        return frames, out, th

    def test_parallel_matches_serial(self):
        for fps in ("30000/1001", "24", "60", "25"):
            with self.subTest(fps=fps):
                src = self.tmp / f"clip_{fps.replace('/', '_')}.mp4"
                make_clip(src, fps)
                serial, _, _ = self.run_extract(src, [(0.5, 4.5)], 1, 100, fps[:2])
                par, out, th = self.run_extract(src, [(0.5, 4.5)], 4, 100, fps[:2])
                self.assertEqual(ms(par), ms(serial))
                self.assertEqual([f.index for f in par], list(range(len(par))))
                self.assertEqual(len(set(ms(par))), len(par))   # no dupes
                self.assertTrue(all(f.path.exists() and f.thumb.exists() for f in par))
                self.assertEqual(list(out.glob(".work_*")), [])
                self.assertEqual(list(th.glob(".work_*")), [])

    def test_multiple_ranges_keep_order(self):
        src = self.tmp / "clip.mp4"
        make_clip(src, "30")
        ranges = [(3.0, 4.0), (0.0, 1.0), (1.5, 2.5)]
        serial, _, _ = self.run_extract(src, ranges, 1, 90)
        par, _, _ = self.run_extract(src, ranges, 3, 90)
        self.assertEqual(ms(par), ms(serial))
        self.assertEqual(len(par), 90)

    def test_cancel_cleans_up(self):
        src = self.tmp / "clip.mp4"
        make_clip(src, "30", seconds=10)
        cancel = threading.Event()
        cancel.set()
        out, th = self.tmp / "o", self.tmp / "t"
        with self.assertRaises(backend.Cancelled):
            backend.extract_ranges(src, [(0, 8)], out, th, 240,
                                   lambda *_: None, cancel, 2)
        self.assertFalse(out.exists())
        self.assertFalse(th.exists())

    def test_range_past_end_raises(self):
        src = self.tmp / "clip.mp4"
        make_clip(src, "30", seconds=2)
        with self.assertRaises(RuntimeError):
            self.run_extract(src, [(50, 51)], 1)


if __name__ == "__main__":
    unittest.main()
