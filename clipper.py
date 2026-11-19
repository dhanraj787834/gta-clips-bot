"""Find viral moments in a live-stream VOD, cut them, edit to 9:16 with captions.

Signals per second of the stream:
  - chat speed (YouTube live chat replay, if available)
  - audio loudness spikes (screaming, laughing, explosions)
  - hype words in the transcript ("no way", "bro", "what", "lol", ...)
Top non-overlapping windows become clips -> written to the queue with a .txt title.
Optional: ANTHROPIC_API_KEY set -> Claude picks the best moments and writes titles.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
WORK = ROOT / "work"
HYPE = ["no way", "bro", "what", "oh my god", "omg", "let's go", "lets go", "insane", "crazy",
        "lol", "lmao", "haha", "wtf", "clip it", "clip that", "noo", "yes", "run", "police"]


def sh(cmd):
    if cmd and cmd[0] == "yt-dlp":
        extra = ["--retries", "3", "--remote-components", "ejs:github",
                 "--sleep-requests", "1.5", "--sleep-interval", "5", "--max-sleep-interval", "15"]
        if (ROOT / "cookies.txt").exists():
            extra += ["--cookies", str(ROOT / "cookies.txt")]
        if os.getenv("YT_PROXY"):
            extra += ["--proxy", os.getenv("YT_PROXY").strip()]
        cmd = cmd[:1] + extra + cmd[1:]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-1500:]}")
    return r


# ---------------------------------------------------------------- sources
def latest_streams(channel_url, limit=3):
    url = channel_url.rstrip("/")
    if ("/@" in url or "/channel/" in url) and not url.endswith("/streams"):
        url += "/streams"
    out = sh(["yt-dlp", "--flat-playlist", "--playlist-end", str(limit), "-J", url]).stdout
    data = json.loads(out)
    if "entries" not in data:  # a single video / VOD link
        return [{"id": data["id"], "title": data.get("title", ""), "url": data.get("webpage_url") or url}]
    return [{"id": e["id"], "title": e.get("title", ""), "url": e.get("url") or e.get("webpage_url")}
            for e in data.get("entries", []) if e]


def download(url, vid_id, log=print):
    """Phase 1: grab only the AUDIO (+ chat replay) to find moments. Small and fast."""
    d = WORK / vid_id
    d.mkdir(parents=True, exist_ok=True)
    audio = next(iter(d.glob("audio.*")), None)
    if audio is None:
        log("Downloading stream audio (to find the best moments)…")
        sh(["yt-dlp", "-f", "ba[abr<=96]/ba/b", "-o", str(d / "audio.%(ext)s"), url])
        audio = next(iter(d.glob("audio.*")))
    try:  # YouTube chat replay
        sh(["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
            "-o", str(d / "chat"), url])
    except Exception:
        pass
    return audio


def download_section(url, start, end, out, log=print):
    """Phase 2: download ONLY the chosen seconds of video."""
    log(f"Downloading only the clip ({start}s-{end}s)…")
    sh(["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4", "-o", str(out), url])
    return out


# ---------------------------------------------------------------- signals
def duration(video):
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(video)]).stdout.strip())


def loudness_per_sec(video, n):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    a = a[: (len(a) // 8000) * 8000].reshape(-1, 8000)
    rms = np.sqrt((a ** 2).mean(axis=1) + 1e-9)
    out = np.zeros(n)
    out[: min(n, len(rms))] = rms[:n]
    return out


def chat_per_sec(folder, n):
    out = np.zeros(n)
    files = list(Path(folder).glob("chat*.live_chat.json"))
    if not files:
        return out
    for line in files[0].read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.search(r'"videoOffsetTimeMsec":\s*"(\d+)"', line)
        if m:
            s = int(m.group(1)) // 1000
            if 0 <= s < n:
                out[s] += 1
    return out


def z(x):
    x = np.convolve(x, np.ones(5) / 5, mode="same")
    return (x - x.mean()) / (x.std() + 1e-9)


def transcribe(video, start, end, model):
    segs, _ = model.transcribe(str(video), word_timestamps=True, vad_filter=True,
                               clip_timestamps=[start, end])
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append({"start": w.start - start, "end": w.end - start, "word": w.word.strip()})
    return words


# ---------------------------------------------------------------- picking
def candidates(video, clip_len=40, top=12):
    n = int(duration(video))
    score = 1.0 * z(loudness_per_sec(video, n)) + 1.5 * z(chat_per_sec(video.parent, n))
    win = np.convolve(score, np.ones(clip_len), mode="valid")
    picks, used = [], np.zeros(len(win), bool)
    for i in np.argsort(win)[::-1]:
        if used[i]:
            continue
        picks.append({"start": int(i), "end": int(i + clip_len), "score": float(win[i])})
        used[max(0, i - clip_len): i + clip_len] = True
        if len(picks) >= top:
            break
    return picks


def claude_rank(stream_title, cands, keep):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    text = "\n".join(f"#{i} [{c['start']}s] {' '.join(w['word'] for w in c['words'])}"
                     for i, c in enumerate(cands))
    msg = anthropic.Anthropic(api_key=key).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=1500,
        messages=[{"role": "user", "content":
                   f"GTA 6 live stream '{stream_title}'. Transcripts of candidate clips below. "
                   f"Pick the {keep} most viral/funny/shocking for Shorts. Reply ONLY JSON list: "
                   f'[{{"i": index, "title": "punchy title under 70 chars", '
                   f'"caption": "1-2 line caption + 5 hashtags"}}]\n\n{text}'}])
    m = re.search(r"\[.*\]", msg.content[0].text, re.S)
    return json.loads(m.group(0)) if m else None


# ---------------------------------------------------------------- editing
def ass_time(t):
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(words, path):
    head = ("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BorderStyle,Outline,Shadow,Alignment,MarginV,Bold\n"
            "Style: Cap,DejaVu Sans,84,&H0000FFFF,&H00000000,1,6,0,2,520,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Text\n")
    lines = []
    for i in range(0, len(words), 3):  # 3 words per caption pop
        chunk = words[i:i + 3]
        txt = " ".join(w["word"] for w in chunk).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ass_time(chunk[0]['start'])},{ass_time(chunk[-1]['end'])},Cap,{txt}")
    path.write_text(head + "\n".join(lines), encoding="utf-8")


def render(clip_src, words, out):
    ass = out.with_suffix(".ass")
    write_ass(words, ass)
    vf = ("crop=ih*9/16:ih,scale=1080:1920,"  # full-screen centre crop
          f"subtitles='{ass.as_posix()}'")
    sh(["ffmpeg", "-y", "-i", str(clip_src), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out)])
    ass.unlink(missing_ok=True)


# ---------------------------------------------------------------- pipeline
def process_stream(stream, out_dir, keep=3, clip_len=40, log=print):
    from faster_whisper import WhisperModel
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    video = download(stream["url"], stream["id"], log)
    log("Scanning chat + audio for hype moments…")
    cands = candidates(video, clip_len, top=keep * 4)
    log(f"Transcribing {len(cands)} candidates…")
    model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), compute_type="int8")
    for c in cands:
        c["words"] = transcribe(video, c["start"], c["end"], model)
        text = " ".join(w["word"] for w in c["words"]).lower()
        c["score"] += 3 * sum(text.count(h) for h in HYPE)

    ranked = claude_rank(stream["title"], cands, keep)
    if ranked:
        chosen = [(cands[r["i"]], r["title"], r.get("caption", r["title"]))
                  for r in ranked if 0 <= r.get("i", -1) < len(cands)][:keep]
    else:
        best = sorted(cands, key=lambda c: c["score"], reverse=True)[:keep]
        chosen = [(c, f"{stream['title'][:55]} 😱 #gta6 #shorts", "#gta6 #gtavi #gta6gameplay #gta #gaming #shorts") for c in best]

    made = []
    for n, (c, title, caption) in enumerate(chosen, 1):
        name = f"{stream['id']}_{c['start']}"
        out = out_dir / f"{name}.mp4"
        src = download_section(stream["url"], c["start"], c["end"], WORK / stream["id"] / f"raw_{c['start']}.mp4", log)
        log(f"Editing clip {n}/{len(chosen)} ({c['start']}s)…")
        render(src, c["words"], out)
        out.with_suffix(".txt").write_text(f"{title}\n{caption}", encoding="utf-8")
        made.append(out)
    return made
