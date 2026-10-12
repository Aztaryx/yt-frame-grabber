# YT Frame Grabber

Paste a YouTube link, type a time range, get **every frame** in that range as
full-resolution PNGs. Pick which to keep in a simple grid.

## Setup

1. Install [ffmpeg](https://ffmpeg.org/download.html) (`ffmpeg` and `ffprobe` must be on your PATH)
2. `pip install -r requirements.txt`
3. `python main.py`

Optionally pre-fill: `python main.py "https://youtu.be/XXXX" "1:27.250-1:28"`

## Usage

1. Paste the link, press **Enter** (or **Fetch**). Shows the best available stream: resolution, fps, codec, duration, frame count.
2. Type a range and press **Enter** (or **Extract**). Anything works down to the millisecond:
   `87-88` · `87.5-88.25` · `1:27-1:28` · `1:27.250-1:28` · `0:01:27.250 to 0:01:28`
3. Frames appear in a grid.

| Action | How |
|---|---|
| Select / deselect | click, or click-drag to paint |
| Select a run | click one, **shift-click** another |
| Preview full-res | **right-click** (then ←/→, Space to select, Esc to close) |
| Remove or keep | **Delete selected** / **Keep selected** (deletes everything else) |
| Cancel a download/extraction | **Cancel** button or Esc |

Output goes to `output/<Video Title> [id]_<start>-<end>/`, files named
`000000_87.000s.png` (index + exact timestamp in the video).

## Notes

- The best **video-only** stream is downloaded once into `cache/` and reused for any
  further ranges on the same video. Delete `cache/` to reclaim space.
- The range is `[start, end)`: a frame exactly at the end timestamp is not included.
- Frames are decoded straight from the original stream (no re-encode), and timestamps
  come from ffmpeg itself, so variable-frame-rate videos are labelled correctly.
- Large ranges at 4K = lots of big PNGs. Thumbnails are generated in the same ffmpeg pass.

## Troubleshooting

- **Missing formats / lower quality than expected:** update yt-dlp
  (`pip install -U "yt-dlp[default]"`). YouTube support sometimes needs a JavaScript
  runtime; see the yt-dlp docs if fetching fails.
- **Odd colours on some HDR videos:** frames are converted without tone-mapping.

## Disclaimer

For personal use. You're responsible for respecting YouTube's Terms of Service and the
copyright of the videos you process.
