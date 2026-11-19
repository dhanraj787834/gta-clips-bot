"""Daily Shorts/Reels autoposter.

Takes the oldest clip in QUEUE_DIR, uploads it to YouTube Shorts and Instagram Reels,
then moves it to POSTED_DIR. Safe to re-run: if one platform fails, the next run
retries only the platform that failed (progress is kept in state.json).

Caption per clip (optional): put a .txt with the same name next to the video.
    line 1      -> title (YouTube title, max 100 chars)
    lines 2..n  -> caption/description (used on both platforms)
"""
import csv
import datetime as dt
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).parent
QUEUE = Path(os.getenv("QUEUE_DIR", ROOT / "queue")).expanduser()
POSTED = Path(os.getenv("POSTED_DIR", ROOT / "posted")).expanduser()
STATE_FILE = ROOT / "state.json"
LOG_CSV = ROOT / "posted_log.csv"
IG_TOKEN_FILE = ROOT / "ig_token.json"
VIDEO_EXT = {".mp4", ".mov"}
IG_API = "https://graph.instagram.com/v22.0"
FB_API = "https://graph.facebook.com/v22.0"


def ig_api(token):
    # Instagram-login tokens (IGAA...) use graph.instagram.com; Facebook/system-user tokens use graph.facebook.com
    return IG_API if token.startswith("IG") else FB_API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "autopost.log", encoding="utf-8")],
)
log = logging.getLogger("autopost")


def env_bool(name, default="true"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def next_clips(n):
    clips = sorted((p for p in QUEUE.iterdir() if p.suffix.lower() in VIDEO_EXT),
                   key=lambda p: p.stat().st_mtime)
    return clips[:n]


def build_text(clip: Path):
    nice = clip.stem.replace("_", " ").replace("-", " ").strip()
    txt = clip.with_suffix(".txt")
    if txt.exists():
        lines = txt.read_text(encoding="utf-8").strip().splitlines()
        title = lines[0].strip() if lines else nice
        caption = "\n".join(lines[1:]).strip() or title
    else:
        title = os.getenv("DEFAULT_TITLE", "{title} #shorts").format(title=nice)
        caption = os.getenv("DEFAULT_CAPTION", "{title}").format(title=nice).replace("\\n", "\n")
    return title[:100], caption[:2200]


# ---------------------------------------------------------------- YouTube
def youtube_upload(clip: Path, title: str, description: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    if os.getenv("YT_REFRESH_TOKEN"):  # cloud mode (GitHub Actions secrets)
        creds = Credentials(None, refresh_token=os.getenv("YT_REFRESH_TOKEN"),
                            token_uri="https://oauth2.googleapis.com/token",
                            client_id=os.getenv("YT_CLIENT_ID"),
                            client_secret=os.getenv("YT_CLIENT_SECRET"))
        creds.refresh(Request())
    else:
        token_file = ROOT / os.getenv("YT_TOKEN_FILE", "yt_token.json")
        creds = Credentials.from_authorized_user_file(str(token_file))
        if not creds.valid:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())

    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [t.strip() for t in os.getenv("YT_TAGS", "gta 6,gta vi,gta 6 gameplay,gta,gaming,shorts").split(",") if t.strip()],
            "categoryId": os.getenv("YT_CATEGORY_ID", "20"),  # 20 = Gaming
        },
        "status": {
            "privacyStatus": os.getenv("YT_PRIVACY", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(clip), chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            log.info("YouTube upload %d%%", int(status.progress() * 100))
    return resp["id"]


# ---------------------------------------------------------------- Instagram
def ig_token():
    """Return a valid long-lived token, refreshing it if older than ~50 days."""
    saved = load_json(IG_TOKEN_FILE, {})
    token = (saved.get("token") or os.getenv("IG_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN missing in .env")
    refreshed_at = saved.get("refreshed_at", 0)
    if token.startswith("IG") and time.time() - refreshed_at > 50 * 86400:
        r = requests.get(f"https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30)
        if r.ok:
            token = r.json()["access_token"]
            save_json(IG_TOKEN_FILE, {"token": token, "refreshed_at": time.time()})
            log.info("Instagram token refreshed")
        else:
            log.warning("Instagram token refresh failed: %s", r.text)
    return token


def host_temporarily(clip: Path):
    """Instagram pulls the video from a public URL, so park it on Cloudinary."""
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
                      api_key=os.getenv("CLOUDINARY_API_KEY"),
                      api_secret=os.getenv("CLOUDINARY_API_SECRET"), secure=True)
    res = cloudinary.uploader.upload_large(str(clip), resource_type="video",
                                           folder="ig_autopost", chunk_size=20_000_000)
    return res["secure_url"], res["public_id"]


def delete_hosted(public_id):
    import cloudinary.uploader
    try:
        cloudinary.uploader.destroy(public_id, resource_type="video")
    except Exception as e:  # cleanup only
        log.warning("Cloudinary cleanup failed: %s", e)


def instagram_upload(clip: Path, caption: str) -> str:
    """Upload the video file straight to Instagram (resumable upload, no hosting needed)."""
    token = ig_token()
    user_id = os.getenv("IG_USER_ID")
    api = ig_api(token)
    r = requests.post(f"{api}/{user_id}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": token}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram container failed: {r.text}")
    j = r.json()
    container = j["id"]
    upload_uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v22.0/{container}"
    data = clip.read_bytes()
    up = requests.post(upload_uri, data=data, timeout=600, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    if not up.ok:
        raise RuntimeError(f"Instagram upload failed: {up.text}")

    for _ in range(60):  # wait up to ~10 min for Instagram to process
        st = requests.get(f"{api}/{container}",
                          params={"fields": "status_code,status", "access_token": token}, timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram processing failed: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")

    r = requests.post(f"{api}/{user_id}/media_publish",
                      data={"creation_id": container, "access_token": token}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram publish failed: {r.text}")
    return r.json()["id"]


# ---------------------------------------------------------------- main
def post_clip(clip: Path, state: dict):
    rec = state.setdefault(clip.name, {})
    title, caption = build_text(clip)
    log.info("Posting %s | %s", clip.name, title)

    if env_bool("ENABLE_YOUTUBE") and not rec.get("youtube_id"):
        try:
            rec["youtube_id"] = youtube_upload(clip, title, caption)
            log.info("YouTube OK: https://youtube.com/shorts/%s", rec["youtube_id"])
        except Exception as e:
            log.error("YouTube failed: %s", e)
        save_json(STATE_FILE, state)

    if env_bool("ENABLE_INSTAGRAM") and not rec.get("instagram_id"):
        try:
            rec["instagram_id"] = instagram_upload(clip, caption)
            log.info("Instagram OK: media id %s", rec["instagram_id"])
        except Exception as e:
            log.error("Instagram failed: %s", e)
        save_json(STATE_FILE, state)

    done = ((not env_bool("ENABLE_YOUTUBE") or rec.get("youtube_id")) and
            (not env_bool("ENABLE_INSTAGRAM") or rec.get("instagram_id")))
    if done:
        POSTED.mkdir(parents=True, exist_ok=True)
        shutil.move(str(clip), POSTED / clip.name)
        txt = clip.with_suffix(".txt")
        if txt.exists():
            shutil.move(str(txt), POSTED / txt.name)
        new = not LOG_CSV.exists()
        with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["posted_at", "file", "title", "youtube_id", "instagram_id"])
            w.writerow([dt.datetime.now().isoformat(timespec="seconds"), clip.name, title,
                        rec.get("youtube_id", ""), rec.get("instagram_id", "")])
        state.pop(clip.name, None)
        save_json(STATE_FILE, state)
    return bool(done)


def main():
    if not QUEUE.exists():
        log.error("Queue folder not found: %s", QUEUE)
        return 1
    clips = next_clips(int(os.getenv("POSTS_PER_RUN", "1")))
    if not clips:
        log.warning("Queue is empty — nothing to post. Add clips to %s", QUEUE)
        return 0
    state = load_json(STATE_FILE, {})
    ok = all([post_clip(c, state) for c in clips])
    left = len([p for p in QUEUE.iterdir() if p.suffix.lower() in VIDEO_EXT])
    log.info("Done. %d clip(s) left in queue (%d days of posts).", left, left)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
