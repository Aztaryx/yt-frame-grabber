# YT Frame Grabber

Paste a YouTube link, type one or more time ranges, get **every frame** in those ranges as
full-resolution PNGs. Pick which to keep in a simple grid.

## Setup

1. Install [ffmpeg](https://ffmpeg.org/download.html) (`ffmpeg` and `ffprobe` must be on your PATH)
2. `pip install -r requirements.txt`
3. `python main.py`

Optionally pre-fill: `python main.py "https://youtu.be/XXXX" "1:27.250-1:28"`

## Usage

1. Paste the link, press **Enter** (or **Fetch**). Shows the best available stream: resolution, fps, codec, duration, frame count.
2. Type one or more ranges and press **Enter** (or **Extract**). Separate ranges
   with commas, semicolons, or new lines. Anything works down to the millisecond:
   `87-88` · `87.5-88.25` · `1:27-1:28` · `1:27.250-1:28` ·
   `87-88, 100-101` · `0:01:27.250 to 0:01:28`
3. Frames appear in a grid.

| Action | How |
|---|---|
| Select / deselect | click, or click-drag to paint |
| Select a run | click one, **shift-click** another |
| Preview full-res | **right-click** (then ←/→, Space to select, Esc to close) |
| Remove or keep | **Delete selected** / **Keep selected** (deletes everything else) |
| Reopen a previous extraction | **History**, then click an entry (saved frames are reused without re-extracting) |
| Undo a removal | **Undo delete** (available for the current session) |
| Cancel a download/extraction | **Cancel** button or Esc |

Closing while an extraction is running, or while the current grid has missing image files,
shows a confirmation first. Completed extractions are already saved in the output directory.

Output goes to `output/<Video Title> [id]_<start>-<end>/` for one range, or a
`multiple-ranges` directory for multiple ranges. Files are named
`000000_87.000s.png` (index + exact timestamp in the video).

## Notes

- The best **video-only** stream is downloaded once into `cache/` and reused for any
  further ranges on the same video. Delete `cache/` to reclaim space.
- The range is `[start, end)`: a frame exactly at the end timestamp is not included.
- Frames are decoded straight from the original stream (no re-encode), and timestamps
  come from ffmpeg itself, so variable-frame-rate videos are labelled correctly.
- Large ranges at 4K = lots of big PNGs (see [Performance](#performance-and-large-ranges)).
  Thumbnails are generated in the same ffmpeg pass.

## Performance and large ranges

A range with thousands of frames is mostly a disk and CPU problem, not a UI one:
every frame is decoded and written as a full-resolution PNG.

**Expect, per frame (1080p; 4K is roughly 4x each):** ~3.5 MB on disk and ~0.2 s of
CPU, almost all of it PNG encoding (decoding is only ~15% of the time). That is
~10 GB and ~11 minutes of CPU for 3000 frames on one core. Those figures come from a
noisy synthetic test clip, so real footage is usually smaller, but budget disk space
before extracting thousands of frames.

What keeps big ranges manageable:

- **Parallel extraction.** PNG encoding is single-threaded inside one ffmpeg process, so
  large ranges are split into slices decoded by several ffmpeg processes at once
  (default: one per CPU core, max 8). Slices are never smaller than 150 frames, since each
  one pays a seek and a decode from the previous keyframe, so small ranges run exactly as
  before. Multiple ranges also run in parallel. Set `YTFG_WORKERS=1` to turn this off, or
  e.g. `YTFG_WORKERS=4` to cap it. Results are identical to a single process: same frames,
  same timestamps, same file names (covered by `test_backend.py`, including frames that
  land exactly on a slice boundary).
- **No temp-folder round trip.** Frames are written straight into the output folder
  instead of the system temp dir and then moved, which used to copy every PNG a second
  time when the two were on different drives.
- **Responsive grid.** Thumbnails are made at display size by ffmpeg (no resize on the UI
  thread), are decoded within a ~8 ms per-frame budget so fast scrolling never stalls,
  and are cached with a cap (LRU) instead of growing without bound. Grid labels are
  rendered once and reused. Delete/Keep move the PNGs into a hidden
  `output/.trash-<pid>` folder (a rename, never a cross-drive copy; it is removed when
  the app exits), and Undo is O(n) rather than O(n^2).

Measure it on your own machine:

```
python bench.py path/to/video.mp4 10 20 1 2 4 8     # seconds 10-20 with 1, 2, 4, 8 workers
```

For reference, on a single-core test machine slicing into two workers cost about 5%
extra (the repeated seek) and gained nothing, as expected; the speed-up needs more than
one core. The multi-core speed-up is not benchmarked in this repo yet, so numbers from
`bench.py` on real hardware are welcome.

## Tests

```
python test_timeparse.py
python test_backend.py      # needs ffmpeg on PATH; builds tiny clips and compares serial vs parallel
```

## Troubleshooting

- **Missing formats / lower quality than expected:** update yt-dlp
  (`pip install -U "yt-dlp[default]"`). YouTube support sometimes needs a JavaScript
  runtime; see the yt-dlp docs if fetching fails.
- **Odd colours on some HDR videos:** frames are converted without tone-mapping.

## Disclaimer

For personal use. You're responsible for respecting YouTube's Terms of Service and the
copyright of the videos you process.
