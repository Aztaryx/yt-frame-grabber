"""Turn human-typed timestamps and ranges into seconds.

Accepted timestamps (all down to the millisecond and beyond):
    87        87.5        87s
    1:27      1:27.250    0:01:27.250
Accepted range separators:  -   –   —   to
    87-88    1:27.250-1:28    87.5 to 88.25
"""
from __future__ import annotations

import re


class TimeParseError(ValueError):
    pass


_NUM = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
_RANGE_SEP = re.compile(r"\s*(?:-|–|—|\bto\b)\s*", re.IGNORECASE)


def parse_time(text: str) -> float:
    s = text.strip().lower()
    if s.endswith("s"):
        s = s[:-1].strip()
    if not s:
        raise TimeParseError("Missing a timestamp")

    parts = [p.strip() for p in s.split(":")]
    if len(parts) > 3 or not all(_NUM.match(p) for p in parts):
        raise TimeParseError(f"Can't read '{text.strip()}' as a time")

    nums = [float(p) for p in parts]
    if any(n >= 60 for n in nums[1:]):
        raise TimeParseError(f"Minutes/seconds must be under 60 in '{text.strip()}'")

    total = 0.0
    for n in nums:
        total = total * 60 + n
    return round(total, 6)


def parse_range(text: str) -> tuple[float, float]:
    parts = _RANGE_SEP.split(text.strip())
    if len(parts) != 2:
        raise TimeParseError("Use a range like 87-88.5 or 1:27.250-1:28")
    start, end = parse_time(parts[0]), parse_time(parts[1])
    if end <= start:
        raise TimeParseError("End must be after start")
    return start, end


def fmt_dur(seconds: float) -> str:
    """Seconds -> H:MM:SS / M:SS for display."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
