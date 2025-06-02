"""
Faceless YouTube Shorts Automation Script
========================================
This script assembles a 15–20‑second vertical video from:
  • A short AI‑generated fact script (OpenAI)
  • 3 royalty‑free vertical clips (Pexels)
  • An AI voice‑over track (ElevenLabs)
It then renders the final MP4 with hard‑burned captions and (optionally)
uploads it as a YouTube Short.

Before you run it
-----------------
1. Create a virtual environment and install dependencies:
   pip install openai moviepy requests python-dotenv google-api-python-client google-auth-oauthlib

2. Put your API keys in a .env file next to this script:
   OPENAI_API_KEY="sk-..."
   PEXELS_API_KEY="563492ad..."
   ELEVENLABS_API_KEY="..."
   YT_CLIENT_SECRET_FILE="client_secret.json"  # OAuth 2.0 file from Google Console
   YT_REFRESH_TOKEN="..."                       # Generate once via OAuth flow
   YT_CHANNEL_ID="UC..."                       # Your channel ID

3. Make sure **ffmpeg** is installed and on the PATH (MoviePy needs it).

4. The first run asks Google consent in a browser to store a refresh token;
   subsequent runs are headless.

Schedule
--------
Run daily via cron or the Windows Task Scheduler. For example (Linux):
   0 18 * * * /usr/bin/python /path/faceless_short_automation.py >> shorts.log 2>&1
"""
from __future__ import annotations
import os
import random
import textwrap
import json
import tempfile
from pathlib import Path
from datetime import datetime

import requests
import openai
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, TextClip
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration & helpers
# ---------------------------------------------------------------------------
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY")

WORKDIR = Path(tempfile.gettempdir()) / "short_builder"
WORKDIR.mkdir(exist_ok=True)

HEADERS_PEXELS = {"Authorization": PEXELS_API_KEY}

VERTICAL_RATIO = (9, 16)
TARGET_DURATION = 18  # seconds
FONT = "Montserrat-Bold"

# ---------------------------------------------------------------------------
# Step 1 – Generate a bite‑sized script
# ---------------------------------------------------------------------------

def generate_script(topic: str) -> str:
    prompt = (
        f"Write a fun, 3‑fact script about {topic} in **≤60 words**. "
        "End with a question to encourage comments."
    )
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo-0125",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=90,
    )
    return resp.choices[0].message.content.strip()

# ---------------------------------------------------------------------------
# Step 2 – Pull three vertical stock clips from Pexels
# ---------------------------------------------------------------------------

def fetch_vertical_clip(query: str) -> Path:
    url = "https://api.pexels.com/videos/search"
    params = {"query": query, "orientation": "vertical", "per_page": 10}
    r = requests.get(url, params=params, headers=HEADERS_PEXELS, timeout=20)
    r.raise_for_status()
    results = r.json().get("videos", [])
    if not results:
        raise RuntimeError(f"No clips found for {query!r}")
    choice = random.choice(results)
    # Pick the smallest vertical file to speed up download
    file_link = sorted(choice["video_files"], key=lambda f: f["width"])[0]["link"]
    out = WORKDIR / f"{choice['id']}.mp4"
    with requests.get(file_link, stream=True, timeout=60) as vid:
        vid.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in vid.iter_content(chunk_size=8192):
                fh.write(chunk)
    return out

# ---------------------------------------------------------------------------
# Step 3 – Make an AI voice‑over with ElevenLabs
# ---------------------------------------------------------------------------

def generate_voiceover(text: str) -> Path:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/EXAVITQu4vr4xnSDxMaL"
    headers = {
        "xi-api-key": ELEVEN_KEY,
        "Content-Type": "application/json",
    }
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    out = WORKDIR / "voice.mp3"
    with open(out, "wb") as fh:
        fh.write(r.content)
    return out

# ---------------------------------------------------------------------------
# Step 4 – Assemble video + burnt‑in captions
# ---------------------------------------------------------------------------

def build_video(clips: list[Path], audio_path: Path, script: str, out_path: Path) -> None:
    # Load clips and trim evenly to fit target duration
    raw_clips = [VideoFileClip(str(p)) for p in clips]
    slice_len = TARGET_DURATION / len(raw_clips)
    clipped = [c.subclip(0, min(slice_len, c.duration)) for c in raw_clips]
    video = concatenate_videoclips(clipped, method="compose")

    # Voice‑over
    voice = AudioFileClip(str(audio_path))
    video = video.set_audio(voice)

    # Add captions (simple centre‑bottom)
    caption = TextClip(
        textwrap.fill(script, 30),
        fontsize=60,
        font=FONT,
        color="white",
        stroke_color="black",
        stroke_width=2,
        size=(video.w * 0.9, None),
        method="caption",
    ).set_position(("center", "bottom")).set_duration(video.duration)

    final = CompositeVideoClip([video, caption])
    final.write_videofile(
        str(out_path),
        codec="libx264",
        audio_codec="aac",
        fps=30,
        preset="ultrafast",
        threads=4,
    )

# ---------------------------------------------------------------------------
# Step 5 – OPTIONAL: Upload to YouTube (Shorts upload = <60 s, 9:16)
# ---------------------------------------------------------------------------

def upload_short(video_path: Path, title: str, description: str) -> None:
    """Skeleton uploader. Fill in OAuth flow separately."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("google-api-python-client not installed; skipping upload.")
        return

    creds = Credentials.from_authorized_user_file(os.getenv("YT_REFRESH_TOKEN"))
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["shorts", "facts"],
            "categoryId": "27",  # Education
        },
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"\rUploading… {status.progress() * 100:.1f}%", end="")
    print("\nUpload complete →", response.get("id"))

# ---------------------------------------------------------------------------
# Main “one‑shot” routine
# ---------------------------------------------------------------------------

def pick_topic() -> str:
    trending = [
        "quantum computing",
        "Mars colonization",
        "deep‑sea creatures",
        "ancient Egyptian tech",
        "AI art",
        "sustainable architecture",
    ]
    return random.choice(trending)


def run_once():
    topic = pick_topic()
    script = generate_script(topic)
    print("SCRIPT:\n", script)

    # Pull three clips matching each fact keyword
    keywords = topic.split()[:3]
    clips = [fetch_vertical_clip(k) for k in keywords]
    voice = generate_voiceover(script)
    out_file = WORKDIR / f"short_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"

    build_video(clips, voice, script, out_file)
    print("Video rendered →", out_file)

    # Upload automatically (uncomment when creds are ready)
    # upload_short(out_file, f"3 facts about {topic} 🤯", script)


if __name__ == "__main__":
    run_once()
