"""GitHub Actions daily run: newest unprocessed stream -> viral clips -> post.

Config lives in sources.txt (one channel URL or stream link per line) and GitHub secrets/variables.
processed_streams.json is committed back so streams are never clipped twice.
If a stream can't be downloaded, the next candidate is tried.
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

    candidates = []
    for src in sources:
        try:
            for s in clipper.latest_streams(src, limit=3):
                if s["id"] not in done and s["id"] not in {c["id"] for c in candidates}:
                    candidates.append(s)
        except Exception as e:
            print(f"Source failed {src}: {e}")

    made = []
    for stream in candidates[:6]:
        print(f"Clipping: {stream['title']} ({stream['url']})")
        try:
            made = clipper.process_stream(stream, QUEUE, keep=per_day, clip_len=clip_len)
        except Exception as e:
            print(f"  -> skipped, could not process: {e}")
            continue
        done.add(stream["id"])
        DONE.write_text(json.dumps(sorted(done), indent=1))
        if made:
            break

    if not made:
        print("No clips made today (no new streams, or all downloads were blocked).")
        return 1

    os.environ["POSTS_PER_RUN"] = str(per_day)
    return subprocess.run([sys.executable, "autopost.py"], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
