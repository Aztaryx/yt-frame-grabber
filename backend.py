"""yt-dlp + ffmpeg plumbing. No GUI code in here."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"      # downloaded source videos (safe to delete)
OUTPUT_DIR = ROOT / "output"    # extracted frames land here

THUMB_MAX = 320                 # longest side of grid thumbnails (px)
PNG_COMPRESSION = 3             # 0-9: lower = faster + bigger files

# progress callback: (fraction 0..1 or None for "unknown", status text)
Progress = Callable[[Optional[float], str], None]


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class VideoInfo:
    url: str
    id: str
    title: str
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    format_id: str
    est_frames: int
    path: Optional[Path] = None          # local file once downloaded
    exact_frames: Optional[int] = None   # counted from the file once downloaded

    @property
    def codec_short(self) -> str:
        return (self.vcodec or "?").split(".")[0]


@dataclass
class Frame:
    index: int
    path: Path
    thumb: Path
    t: Optional[float]       # absolute timestamp in the video (seconds)
    selected: bool = False


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
def check_dependencies() -> list[str]:
    missing = []
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            missing.append(f"{exe} (install ffmpeg and put it on PATH)")
    for mod, pkg in (("yt_dlp", "yt-dlp"), ("pygame", "pygame-ce")):
        if find_spec(mod) is None:
            missing.append(f"{pkg} (pip install {pkg})")
    return missing


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
def fetch_info(url: str) -> VideoInfo:
    """Look up the best video-only stream without downloading anything."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": "bv/b",   # best video-only, fall back to best combined
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    fmt = (info.get("requested_formats") or [info])[0]
    fps = float(fmt.get("fps") or info.get("fps") or 0)
    duration = float(info.get("duration") or 0)
    return VideoInfo(
        url=url,
        id=info["id"],
        title=info.get("title") or info["id"],
        duration=duration,
        width=int(fmt.get("width") or 0),
        height=int(fmt.get("height") or 0),
        fps=fps,
        vcodec=fmt.get("vcodec") or "?",
        format_id=str(fmt["format_id"]),
        est_frames=round(fps * duration) if fps and duration else 0,
    )


def _find_cached(info: VideoInfo) -> Optional[Path]:
    for p in CACHE_DIR.glob(f"{info.id}.{info.format_id}.*"):
        if not any(x in p.name for x in (".part", ".ytdl", ".temp")):
            return p
    return None


def count_frames(path: Path) -> Optional[int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return int(json.loads(r.stdout)["streams"][0]["nb_read_packets"])
    except Exception:
        return None


def ensure_downloaded(info: VideoInfo, progress: Progress,
                      cancel: threading.Event) -> Path:
    """Download the video-only stream into cache/ (reused on later runs)."""
    import yt_dlp

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _find_cached(info)

    if path is None:
        def hook(d):
            if cancel.is_set():
                raise Cancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                frac = (d.get("downloaded_bytes", 0) / total) if total else None
                pct = f"{frac * 100:.0f}%" if frac is not None else ""
                speed = (d.get("_speed_str") or "").strip()
                progress(frac, f"Downloading source video... {pct} {speed}".strip())

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": info.format_id,
            "outtmpl": str(CACHE_DIR / f"{info.id}.{info.format_id}.%(ext)s"),
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([info.url])

        path = _find_cached(info)
        if path is None:
            raise RuntimeError("Download finished but the file wasn't found in cache/")

    info.path = path
    if info.exact_frames is None:
        progress(None, "Counting frames...")
        info.exact_frames = count_frames(path)
    return path


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _fps_mode_args() -> tuple[str, ...]:
    """'-fps_mode passthrough' on new ffmpeg, '-vsync 0' on old (<5.1)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "full"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        ).stdout
        if "-fps_mode" not in out:
            return ("-vsync", "0")
    except Exception:
        pass
    return ("-fps_mode", "passthrough")


_SHOWINFO = re.compile(r"n:\s*(\d+)\s+pts:\s*\S+\s+pts_time:\s*(\S+)")


def _to_float(s: str) -> Optional[float]:
    try:
        v = float(s)
        return v if v == v else None   # filter NaN
    except ValueError:
        return None


def _safe_name(s: str, limit: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].rstrip(" .") or "video"


def make_output_dir(info: VideoInfo, start: float, end: float) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_name(info.title)} [{info.id}]_{start:.3f}-{end:.3f}"
    d, n = OUTPUT_DIR / base, 2
    while d.exists():
        d = OUTPUT_DIR / f"{base} ({n})"
        n += 1
    return d


def make_multi_output_dir(info: VideoInfo) -> Path:
    """Create a distinct output directory for an extraction with many ranges."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_name(info.title)} [{info.id}]_multiple-ranges"
    d, n = OUTPUT_DIR / base, 2
    while d.exists():
        d = OUTPUT_DIR / f"{base} ({n})"
        n += 1
    return d


def extract_frames(src: Path, start: float, end: float, out_dir: Path,
                   thumb_dir: Path, expected: int, progress: Progress,
                   cancel: threading.Event) -> list[Frame]:
    """Decode every frame with timestamp in [start, end).

    One ffmpeg pass writes full-res PNGs to out_dir and small JPG thumbnails
    to thumb_dir. The showinfo filter reports each frame's real timestamp, so
    variable-frame-rate videos are labelled correctly.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    mode = list(_fps_mode_args())
    scale = (f"scale={THUMB_MAX}:{THUMB_MAX}:force_original_aspect_ratio=decrease"
             ":force_divisible_by=2")

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y",
        # -ss before -i: fast seek, then ffmpeg decodes up to the exact frame
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
        # output 1: full-res PNGs
        "-map", "0:v:0", "-an", "-sn", *mode, "-start_number", "0",
        "-compression_level", str(PNG_COMPRESSION), str(out_dir / "%06d.png"),
        # output 2: thumbnails (showinfo prints one line per frame)
        "-map", "0:v:0", "-an", "-sn", *mode, "-start_number", "0",
        "-vf", f"showinfo,{scale}", "-q:v", "4", str(thumb_dir / "%06d.jpg"),
    ]

    def cleanup():
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    times: list[Optional[float]] = []
    tail: deque[str] = deque(maxlen=6)
    try:
        for line in proc.stderr:
            if cancel.is_set():
                raise Cancelled()
            m = _SHOWINFO.search(line)
            if m:
                times.append(_to_float(m.group(2)))
                frac = min(len(times) / expected, 1.0) if expected else None
                progress(frac, f"Extracting frames... {len(times)}")
            elif line.strip():
                tail.append(line.rstrip())
        rc = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        cleanup()
        raise

    if rc != 0:
        cleanup()
        raise RuntimeError("ffmpeg failed: " + " | ".join(tail))

    pngs = sorted(out_dir.glob("*.png"))
    thumbs = sorted(thumb_dir.glob("*.jpg"))
    if not pngs:
        cleanup()
        raise RuntimeError("No frames in that range (past the end of the video?)")

    have_times = len(times) == len(pngs)
    frames: list[Frame] = []
    for i in range(min(len(pngs), len(thumbs))):
        t = None
        path = pngs[i]
        if have_times and times[i] is not None:
            t = start + times[i]
            named = out_dir / f"{i:06d}_{t:.3f}s.png"
            path.rename(named)
            path = named
        frames.append(Frame(index=i, path=path, thumb=thumbs[i], t=t))
    return frames


def extract_ranges(src: Path, ranges: list[tuple[float, float]], out_dir: Path,
                   thumb_dir: Path, expected: int, progress: Progress,
                   cancel: threading.Event) -> list[Frame]:
    """Extract several ranges into one ordered frame collection."""
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    total_duration = sum(end - start for start, end in ranges)
    total_expected = expected
    all_frames: list[Frame] = []
    try:
        for range_index, (start, end) in enumerate(ranges, 1):
            if cancel.is_set():
                raise Cancelled()
            segment_out = Path(tempfile.mkdtemp(prefix="frames_", dir=thumb_dir))
            segment_thumb = Path(tempfile.mkdtemp(prefix="thumbs_", dir=thumb_dir))
            segment_expected = (
                round(expected * (end - start) / total_duration)
                if expected and total_duration else 0
            )

            def segment_progress(frac, text, offset=len(all_frames)):
                if frac is None or not total_expected:
                    progress(None, f"Range {range_index}/{len(ranges)}: {text}")
                else:
                    progress(
                        min((offset + frac * segment_expected) / total_expected, 1.0),
                        f"Range {range_index}/{len(ranges)}: {text}",
                    )

            frames = extract_frames(
                src, start, end, segment_out, segment_thumb,
                segment_expected, segment_progress, cancel,
            )
            for frame in frames:
                index = len(all_frames)
                suffix = f"_{frame.t:.3f}s" if frame.t is not None else ""
                path = out_dir / f"{index:06d}{suffix}.png"
                thumb = thumb_dir / f"{index:06d}.jpg"
                shutil.move(str(frame.path), path)
                shutil.move(str(frame.thumb), thumb)
                all_frames.append(Frame(index, path, thumb, frame.t))
            shutil.rmtree(segment_out, ignore_errors=True)
            shutil.rmtree(segment_thumb, ignore_errors=True)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)
        raise

    if not all_frames:
        raise RuntimeError("No frames in the requested ranges")
    return all_frames


def delete_frames(frames: list[Frame]) -> None:
    for f in frames:
        for p in (f.path, f.thumb):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
