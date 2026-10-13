"""yt-dlp + ffmpeg plumbing. No GUI code in here."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"      # downloaded source videos (safe to delete)
OUTPUT_DIR = ROOT / "output"    # extracted frames land here

THUMB_W, THUMB_H = 192, 108     # grid thumbnails are made at display size
MAX_WORKERS = 8                # cap on parallel ffmpeg processes
MIN_CHUNK_FRAMES = 150         # never slice a range finer than this
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


# --------------------------------------------------------------------------
# Extraction (parallel)
# --------------------------------------------------------------------------
def _worker_count() -> int:
    """How many ffmpeg processes to run at once (override: YTFG_WORKERS)."""
    env = os.environ.get("YTFG_WORKERS", "").strip()
    if env.isdigit() and int(env) >= 1:
        return int(env)
    return max(1, min(os.cpu_count() or 1, MAX_WORKERS))


@dataclass
class _Job:
    """One ffmpeg run: a slice of one requested range, in integer ms."""
    range_idx: int
    piece: int           # position within its range (0 = first)
    start_ms: int
    end_ms: int
    png_dir: Path
    thumb_dir: Path
    times: Optional[list] = None   # per-frame pts_time (relative to start)


def _plan_jobs(ranges: list[tuple[float, float]], expected: int, workers: int,
               png_root: Path, thumb_root: Path) -> list[_Job]:
    """Cut the ranges into slices so every worker has something to do.

    PNG encoding is single-threaded inside one ffmpeg process, so a single
    big range would otherwise use one core. Slices are never smaller than
    MIN_CHUNK_FRAMES, because each slice pays a seek + decode-from-keyframe
    cost. If the frame rate is unknown (expected == 0) ranges aren't split.
    """
    total_dur = sum(e - s for s, e in ranges) or 1.0
    target = max(MIN_CHUNK_FRAMES, -(-expected // workers)) if expected else 0
    jobs: list[_Job] = []
    for ri, (s, e) in enumerate(ranges):
        s_ms, e_ms = round(s * 1000), round(e * 1000)
        est = expected * (e - s) / total_dur if expected else 0
        pieces = max(1, round(est / target)) if target else 1
        pieces = min(pieces, max(1, e_ms - s_ms))   # no empty slices
        for k in range(pieces):
            a = s_ms + (e_ms - s_ms) * k // pieces
            b = s_ms + (e_ms - s_ms) * (k + 1) // pieces
            n = len(jobs)
            jobs.append(_Job(ri, k, a, b,
                             png_root / f".work_{n:04d}",
                             thumb_root / f".work_{n:04d}"))
    return jobs


def _ffmpeg_cmd(src: Path, job: _Job, decoder_threads: int) -> list[str]:
    scale = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease"
             ":force_divisible_by=2:flags=area")
    mode = list(_fps_mode_args())
    pre = ["-threads", str(decoder_threads)] if decoder_threads else []
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y",
        # -ss before -i: fast seek, then ffmpeg decodes up to the exact frame
        *pre, "-ss", f"{job.start_ms / 1000:.3f}",
        "-t", f"{(job.end_ms - job.start_ms) / 1000:.3f}", "-i", str(src),
        # output 1: full-res PNGs
        "-map", "0:v:0", "-an", "-sn", *mode, "-start_number", "0",
        "-compression_level", str(PNG_COMPRESSION),
        str(job.png_dir / "%06d.png"),
        # output 2: thumbnails (showinfo prints one line per frame)
        "-map", "0:v:0", "-an", "-sn", *mode, "-start_number", "0",
        "-vf", f"showinfo,{scale}", "-q:v", "4", str(job.thumb_dir / "%06d.jpg"),
    ]


def extract_ranges(src: Path, ranges: list[tuple[float, float]], out_dir: Path,
                   thumb_dir: Path, expected: int, progress: Progress,
                   cancel: threading.Event,
                   workers: Optional[int] = None) -> list[Frame]:
    """Decode every frame with timestamp in [start, end) for each range.

    Ranges (and big ranges, split into slices) are decoded by several ffmpeg
    processes at once. Each process writes full-res PNGs straight into a work
    folder inside out_dir (same filesystem, so the final rename is free) and
    small JPG thumbnails into thumb_dir. showinfo reports each frame's real
    timestamp, so variable-frame-rate videos are labelled correctly.
    """
    workers = workers or _worker_count()
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    jobs = _plan_jobs(ranges, expected, workers, out_dir, thumb_dir)
    pool_size = min(workers, len(jobs))
    # don't let N processes each spawn a full set of decoder threads
    decoder_threads = max(1, (os.cpu_count() or 1) // pool_size) if pool_size > 1 else 0

    abort = threading.Event()
    lock = threading.Lock()
    procs: set[subprocess.Popen] = set()
    done = [0]
    last_report = [0.0]

    def report(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_report[0] < 0.05:   # <= 20 updates/s
            return
        last_report[0] = now
        frac = min(done[0] / expected, 1.0) if expected else None
        who = f" ({pool_size} workers)" if pool_size > 1 else ""
        progress(frac, f"Extracting frames... {done[0]}"
                       + (f" / ~{expected}" if expected else "") + who)

    def run(job: _Job) -> None:
        if abort.is_set():
            raise Cancelled()
        job.png_dir.mkdir(parents=True, exist_ok=True)
        job.thumb_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            _ffmpeg_cmd(src, job, decoder_threads), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        with lock:
            procs.add(proc)
        times: list[Optional[float]] = []
        tail: deque[str] = deque(maxlen=6)
        try:
            for line in proc.stderr:
                m = _SHOWINFO.search(line)
                if m:
                    times.append(_to_float(m.group(2)))
                    with lock:
                        done[0] += 1
                        report()
                elif line.strip():
                    tail.append(line.rstrip())
            rc = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            with lock:
                procs.discard(proc)
            proc.stderr.close()
        if abort.is_set():
            raise Cancelled()
        if rc != 0:
            raise RuntimeError("ffmpeg failed: " + " | ".join(tail))
        job.times = times

    def cleanup() -> None:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)

    pool = ThreadPoolExecutor(max_workers=pool_size)
    futures = [pool.submit(run, j) for j in jobs]
    try:
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            if cancel.is_set():
                raise Cancelled()
            for f in finished:
                f.result()          # re-raises the first ffmpeg failure
        with lock:
            report(force=True)
    except BaseException:
        abort.set()
        with lock:
            for p in procs:         # unblocks every reader immediately
                p.kill()
        for f in futures:
            f.cancel()
        pool.shutdown(wait=True)
        cleanup()
        raise
    pool.shutdown()

    try:
        frames = _assemble(jobs, out_dir, thumb_dir)
    except BaseException:
        cleanup()
        raise
    if not frames:
        cleanup()
        raise RuntimeError("No frames in the requested ranges "
                           "(past the end of the video?)")
    return frames


def _assemble(jobs: list[_Job], out_dir: Path, thumb_dir: Path) -> list[Frame]:
    """Move each slice's frames into final, globally numbered names."""
    frames: list[Frame] = []
    prev_last_t: Optional[float] = None
    for job in jobs:
        pngs = sorted(job.png_dir.glob("*.png"))
        thumbs = sorted(job.thumb_dir.glob("*.jpg"))
        times = job.times or []
        have_times = len(times) == len(pngs)
        first_in_slice = True
        for i in range(min(len(pngs), len(thumbs))):
            t = None
            if have_times and times[i] is not None:
                t = round(job.start_ms / 1000 + times[i], 6)
            # A frame sitting exactly on a slice boundary could be emitted by
            # both neighbours; keep it once. (pts_time is printed with 6
            # decimals relative to each slice, so allow 0.1 ms of slack;
            # real frames are always several ms apart.)
            if (first_in_slice and job.piece > 0 and t is not None
                    and prev_last_t is not None and t <= prev_last_t + 1e-4):
                first_in_slice = False
                continue
            first_in_slice = False
            index = len(frames)
            suffix = f"_{t:.3f}s" if t is not None else ""
            path = out_dir / f"{index:06d}{suffix}.png"
            thumb = thumb_dir / f"{index:06d}.jpg"
            os.replace(pngs[i], path)
            os.replace(thumbs[i], thumb)
            frames.append(Frame(index, path, thumb, t))
            if t is not None:
                prev_last_t = t
        shutil.rmtree(job.png_dir, ignore_errors=True)
        shutil.rmtree(job.thumb_dir, ignore_errors=True)
    return frames


def extract_frames(src: Path, start: float, end: float, out_dir: Path,
                   thumb_dir: Path, expected: int, progress: Progress,
                   cancel: threading.Event,
                   workers: Optional[int] = None) -> list[Frame]:
    """Single-range convenience wrapper around extract_ranges."""
    return extract_ranges(src, [(start, end)], out_dir, thumb_dir, expected,
                          progress, cancel, workers)


def delete_frames(frames: list[Frame]) -> None:
    for f in frames:
        for p in (f.path, f.thumb):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
