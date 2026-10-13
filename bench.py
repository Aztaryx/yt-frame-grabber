"""Time frame extraction with different worker counts.

    python bench.py VIDEO.mp4 START END [WORKERS ...]
    python bench.py clip.mp4 10 20 1 2 4 8

START/END are seconds. Frames are written to a temp folder and deleted
afterwards. Default worker counts: 1 and the auto-detected value.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import backend


def probe_fps(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=avg_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    num, _, den = json.loads(out)["streams"][0]["avg_frame_rate"].partition("/")
    return float(num) / float(den or 1)


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    src, start, end = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    counts = [int(x) for x in sys.argv[4:]] or sorted({1, backend._worker_count()})
    expected = round(probe_fps(src) * (end - start))
    print(f"{src.name}: {start}-{end}s, ~{expected} frames, "
          f"slices no smaller than {backend.MIN_CHUNK_FRAMES} frames")
    base = None
    for w in counts:
        tmp = Path(tempfile.mkdtemp(prefix="ytfg_bench_"))
        try:
            t0 = time.monotonic()
            frames = backend.extract_ranges(
                src, [(start, end)], tmp / "png", tmp / "thumbs", expected,
                lambda *_: None, threading.Event(), workers=w)
            dt = time.monotonic() - t0
            size = sum(f.path.stat().st_size for f in frames)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        base = base or dt
        print(f"  workers={w:<2}  {dt:6.1f}s  {len(frames) / dt:6.1f} frames/s  "
              f"{size / 1e6:7.0f} MB  x{base / dt:.2f}")


if __name__ == "__main__":
    main()
