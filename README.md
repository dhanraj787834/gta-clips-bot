# GTA Clips Bot

Runs free on GitHub Actions every day: newest stream from `sources.txt` → finds viral moments (chat + audio + transcript, optional Claude AI) → 9:16 crop + captions → posts to YouTube Shorts + Instagram Reels.

- Edit `sources.txt` to add streamer channels.
- Secrets (Settings → Secrets and variables → Actions): YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, IG_ACCESS_TOKEN, IG_USER_ID, CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET, optional ANTHROPIC_API_KEY, YT_COOKIES.
- Variables (optional): POSTS_PER_DAY (2), CLIP_LEN (40), YT_PRIVACY (public).
- Run manually: Actions → Daily GTA clips → Run workflow.
