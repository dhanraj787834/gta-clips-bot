"""GitHub Actions daily run: newest unprocessed stream -> viral clips -> post.

Config lives in sources.txt (one channel URL per line) and GitHub secrets/variables.
processed_streams.json is committed back so streams are never clipped twice.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import clipper

ROOT = Path(__file__).parent
DONE = ROOT / "processed_streams.json"
QUEUE = ROOT / "queue"


def main():
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    sources = [l.strip() for l in (ROOT / "sources.txt").read_text().splitlines()
               if l.strip() and not l.startswith("#")]
    per_day = int(os.getenv("POSTS_PER_DAY", "2"))
    clip_len = int(os.getenv("CLIP_LEN", "40"))

    stream = None
    for src in sources:
        try:
            for s in clipper.latest_streams(src, limit=5):
                if s["id"] not in done:
                    stream = s
                    break
        except Exception as e:
            print(f"Source failed {src}: {e}")
        if stream:
            break
    if not stream:
        print("No new streams today.")
        return 0

    print(f"Clipping: {stream['title']}")
    clipper.process_stream(stream, QUEUE, keep=per_day, clip_len=clip_len)
    done.add(stream["id"])
    DONE.write_text(json.dumps(sorted(done), indent=1))

    os.environ["POSTS_PER_RUN"] = str(per_day)
    return subprocess.run([sys.executable, "autopost.py"], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
