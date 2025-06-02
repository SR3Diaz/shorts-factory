"""
Faceless YouTube Shorts Automation Script ─ 2025‑06‑02
=====================================================
Now bilingual by design (English default, Italian optional)
---------------------------------------------------------
* **openai‑python ≥ 1.0** interface (`OpenAI()`)
* `--auth` flag → prints refresh‑token once
* NEW: `--lang en|it` to pick script + TTS language

Usage
-----
```bash
# one‑time — get refresh token
auth$ python faceless_short_automation.py --auth

# render & upload daily short in English (default)
$ python faceless_short_automation.py

# render local short in Italian
$ python faceless_short_automation.py --lang it --no‑upload
```
Note → in GitHub Actions you can set `LANGUAGE=en` or `LANGUAGE=it` as an
*environment variable* rather than passing `--lang`.
"""
from __future__ import annotations
import os
import random
import textwrap
import tempfile
from pathlib import Path
from datetime import datetime
import argparse
import sys

import requests
from openai import OpenAI
from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeVideoClip,
    TextClip,
)
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY")

client = OpenAI(api_key=OPENAI_KEY)

WORKDIR = Path(tempfile.gettempdir()) / "short_builder"
WORKDIR.mkdir(exist_ok=True)

HEADERS_PEXELS = {"Authorization": PEXELS_API_KEY}
TARGET_DURATION = 18  # seconds
FONT = "Montserrat-Bold"
DEFAULT_LANG = os.getenv("LANGUAGE", "en")  # env var overrides default

VOICE_ID = {
    "en": "EXAVITQu4vr4xnSDxMaL",  # English / default voice
    "it": "TxGEqnHWrfWFTf9VQmLc",   # ElevenLabs Italian male voice
}

# ---------------------------------------------------------------------------
# OAuth helper (one‑time)
# ---------------------------------------------------------------------------

def get_refresh_token() -> None:
    if not Path("client_secret.json").exists():
        sys.exit("ERROR: client_secret.json missing")
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    print("\nREFRESH_TOKEN:\n" + creds.refresh_token + "\n")
    print("Paste this into GitHub secret YT_REFRESH_TOKEN")

# ---------------------------------------------------------------------------
# Step 1 ▸ Script generation
# ---------------------------------------------------------------------------

def generate_script(topic: str, lang: str) -> str:
    if lang == "it":
        prompt = (
            f"Scrivi un copione divertente in 3 fatti su {topic} in massimo 60 parole. "
            "Termina con una domanda per incoraggiare i commenti."
        )
    else:  # English default
        prompt = (
            f"Write a fun, 3‑fact script about {topic} in ≤60 words. "
            "End with a question to encourage comments."
        )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo-0125",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=90,
    )
    return resp.choices[0].message.content.strip()

# ---------------------------------------------------------------------------
# Step 2 ▸ Pexels vertical clip
# ---------------------------------------------------------------------------

def fetch_vertical_clip(query: str) -> Path:
    params = {"query": query, "orientation": "vertical", "per_page": 10}
    r = requests.get("https://api.pexels.com/videos/search", params=params, headers=HEADERS_PEXELS, timeout=20)
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No vertical clips for {query}")
    file_link = min(random.choice(videos)["video_files"], key=lambda f: f["width"])["link"]
    out = WORKDIR / f"{random.randint(10**6, 10**7)}.mp4"
    with requests.get(file_link, stream=True, timeout=60) as src, open(out, "wb") as dst:
        for chunk in src.iter_content(8192):
            dst.write(chunk)
    return out

# ---------------------------------------------------------------------------
# Step 3 ▸ AI voice‑over (ElevenLabs)
# ---------------------------------------------------------------------------

def generate_voiceover(text: str, lang: str) -> Path:
    voice_id = VOICE_ID.get(lang, VOICE_ID["en"])
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    out = WORKDIR / "voice.mp3"
    out.write_bytes(r.content)
    return out

# ---------------------------------------------------------------------------
# Step 4 ▸ Assemble video
# ---------------------------------------------------------------------------

def build_video(clips: list[Path], audio_path: Path, script: str, out_path: Path):
    raw = [VideoFileClip(str(p)) for p in clips]
    seg = TARGET_DURATION / len(raw)
    trimmed = [c.subclip(0, min(seg, c.duration)) for c in raw]
    base = concatenate_videoclips(trimmed, method="compose").set_audio(AudioFileClip(str(audio_path)))
    caption = TextClip(
        textwrap.fill(script, 30),
        fontsize=60,
        font=FONT,
        color="white",
        stroke_color="black",
        stroke_width=2,
        size=(base.w * 0.9, None),
        method="caption",
    ).set_position(("center", "bottom")).set_duration(base.duration)
    CompositeVideoClip([base, caption]).write_videofile(
        str(out_path), codec="libx264", audio_codec="aac", fps=30, preset="ultrafast", threads=4, logger=None
    )

# ---------------------------------------------------------------------------
# Step 5 ▸ (Optional) YouTube upload
# ---------------------------------------------------------------------------

def upload_short(video_path: Path, title: str, description: str):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("google‑api‑python‑client missing → skip upload")
        return
    creds = Credentials.from_authorized_user_info({"refresh_token": os.getenv("YT_REFRESH_TOKEN")})
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": title, "description": description, "categoryId": "27"}, "status": {"privacyStatus": "public"}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video_path), resumable=True))
    print("Uploading…", end="")
    while True:
        status, resp = req.next_chunk()
        if resp:
            print(" done →", resp.get("id")); break
        if status: print(f" {status.progress()*100:.1f}%", end="")

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def pick_topic(lang: str) -> str:
    # identical list for both langs; keywords stay English for search compatibility
    return random.choice([
        "quantum computing",
        "Mars colonization",
        "deep‑sea creatures",
        "ancient Egyptian tech",
        "AI art",
        "sustainable architecture",
    ])

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(lang: str, upload: bool):
    topic = pick_topic(lang)
    script = generate_script(topic, lang)
    print("SCRIPT:\n" + script)

    clips = [fetch_vertical_clip(k) for k in topic.split()[:3]]
    voice = generate_voiceover(script, lang)
    out_file = WORKDIR / f"short_{datetime.utcnow():%Y%m%d_%H%M%S}.mp4"
    build_video(clips, voice, script, out_file)
    print("Video saved →", out_file)
    if upload:
        upload_short(out_file, ("3 facts about " if lang == "en" else "3 fatti su ") + topic, script)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser("Faceless Shorts generator")
    ap.add_argument("--auth", action="store_true", help="Run OAuth only; no video")
    ap.add_argument("--lang", choices=["en", "it"], default=DEFAULT_LANG, help="Language for script & TTS")
    ap.add_argument("--no-upload", action="store_true", help="Render locally without uploading")
    args = ap.parse_args()

    if args.auth:
        get_refresh_token(); sys.exit()

    run_once(args.lang, upload=not args.no_upload)
