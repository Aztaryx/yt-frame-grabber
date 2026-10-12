"""YT Frame Grabber: paste a YouTube link, pick a time range, get every frame.

    python main.py
    python main.py "https://youtu.be/XXXX" "1:27.250-1:28"
"""
import sys

import backend


def main() -> None:
    missing = backend.check_dependencies()
    if missing:
        print("Missing dependencies:\n  - " + "\n  - ".join(missing))
        print("\nSee README.md for setup.")
        sys.exit(1)

    from gui import App
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    rng = sys.argv[2] if len(sys.argv) > 2 else ""
    App(url, rng).run()


if __name__ == "__main__":
    main()
