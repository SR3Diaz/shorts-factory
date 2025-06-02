"""
Faceless YouTube-Shorts Automation – OpenAI v1, bilingual (EN/IT)
================================================================
A self-contained script that:
1. Generates a short, fun 3-fact script with a question using OpenAI Chat Completions.
2. Downloads 3 vertical stock clips from Pexels.
3. Synthesises a voice-over with ElevenLabs.
4. Overlays caption + concatenates video & audio into an 18-second short.
5. (Optionally) uploads the short to YouTube.

Key robustness tweaks over the original version
----------------------------------------------
* **New OpenAI client v1** with exponential-backoff retry.
* **Strict length check** (<400 chars) for ElevenLabs TTS.
* **Guaranteed vertical clips** – filters out landscape video_files.
* **UUID filenames** – avoids collisions in `/tmp`.
* **Font fallback** to DejaVu if Montserrat unavailable on the runner.
* **Resource cleanup** – closes MoviePy clips after rendering.
* **Graceful error handling** for API/network hiccups.

Environment variables expected (e.g. in GitHub Actions secrets):
----------------------------------------------------------------
- `OPENAI_API_KEY`       (required)
- `PEXELS_API_KEY`       (required)
- `ELEVENLABS_API_KEY`   (required)
- `YT_REFRESH_TOKEN`     (optional, only if you plan to upload)
- `LANGUAGE`             (optional, default "en")
"""
from __future__ import annotations

import os
import random
import textwrap
import tempfile
import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import requests
from dotenv import load_dotenv
from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeVideoClip,
    TextClip,
)
import openai
from openai.error import RateLimitError, APIError

try:
    from tenacity import retry, wait_random_exponential, stop_after_attempt  # type: ignore
except ImportError:  # tenacity is optional – script still runs
    def retry(*_, **__) -> callable:  # type: ignore
        def deco(fn):
            return fn
        return deco

    wait_random_exponential = stop_after_attempt = None

# ─────────────────────────── CONFIG ────────────────────────────
load_dotenv()

# Verify that OPENAI_API_KEY is set
if not os.getenv("OPENAI_API_KEY"):
    print("ERROR: OPENAI_API_KEY is not set.")
    sys.exit(1)

openai.api_key = os.getenv("OPENAI_API_KEY")

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
if not PEXELS_API_KEY:
    print("WARNING: PEXELS_API_KEY is not set. fetch_vertical_clip will fail if called.")
HEADERS_PEXELS = {"Authorization": PEXELS_API_KEY or ""}

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY")
if not ELEVEN_KEY:
    print("WARNING: ELEVENLABS_API_KEY is not set. generate_voiceover will fail if called.")

WORKDIR = Path(tempfile.gettempdir()) / "short_builder"
WORKDIR.mkdir(exist_ok=True)
TARGET_DURATION = 18  # seconds – YouTube Shorts sweet-spot
DEFAULT_LANG = os.getenv("LANGUAGE", "en").lower()  # en / it
FONT_PREFERRED = "Montserrat-Bold"
FONT_FALLBACK = "DejaVu-Sans-Bold"  # usually available on Linux

VOICE_ID = {
    "en": "EXAVITQu4vr4xnSDxMaL",  # ElevenLabs Adam
    "it": "TxGEqnHWrfWFTf9VQmLc",  # ElevenLabs Eleonora
}

# ─────────────────── Helper: safe filename generation ──────────
def temp_file(ext: str) -> Path:
    return WORKDIR / f"{uuid.uuid4().hex}{ext}"

# ─────────────────────────── OPENAI ────────────────────────────
RETRY_EXCEPTIONS = (RateLimitError, APIError, TimeoutError)

@retry(wait=wait_random_exponential(min=2, max=20), stop=stop_after_attempt(4))
def generate_script(topic: str, lang: str) -> str:
    """Return ≤60-word, 3-fact script ending with a question."""
    prompt = (
        f"Scrivi un copione divertente in 3 fatti su {topic} in massimo 60 parole. Termina con una domanda."
        if lang == "it"
        else f"Write a fun, 3-fact script about {topic} in ≤60 words. End with a question."
    )

    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=90,
    )
    script = resp.choices[0].message.content.strip()

    # Crude length guard – ElevenLabs limit is 400 chars (~75 words)
    if len(script) > 390:
        script = " ".join(script.split()[:75])
    return script

# ──────────────────────── PEXELS VIDEO ─────────────────────────
def _vertical_files(video_dict: dict) -> List[dict]:
    return [f for f in video_dict.get("video_files", []) if f["width"] < f["height"]]

@retry(wait=wait_random_exponential(min=2, max=15), stop=stop_after_attempt(3))
def fetch_vertical_clip(query: str) -> Path:
    if not PEXELS_API_KEY:
        raise EnvironmentError("PEXELS_API_KEY is not set.")

    r = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": query, "orientation": "vertical", "per_page": 10},
        headers=HEADERS_PEXELS,
        timeout=20,
    )
    r.raise_for_status()
    vids = r.json().get("videos", [])
    if not vids:
        raise RuntimeError(f"No vertical clips found for '{query}'.")

    chosen = random.choice(vids)
    files = _vertical_files(chosen)
    if not files:
        raise RuntimeError("Chosen video has no vertical variants: retrying.")

    # Pick the smallest vertical variant
    file_link = min(files, key=lambda f: f["width"])["link"]
    out_path = temp_file(".mp4")

    with requests.get(file_link, stream=True, timeout=60) as src, open(out_path, "wb") as dst:
        for chunk in src.iter_content(chunk_size=8192):
            dst.write(chunk)

    return out_path

# ─────────────────────── ELEVENLABS TTS ────────────────────────
@retry(wait=wait_random_exponential(min=2, max=10), stop=stop_after_attempt(3))
def generate_voiceover(text: str, lang: str) -> Path:
    if not ELEVEN_KEY:
        raise EnvironmentError("ELEVENLABS_API_KEY is not set.")
    if len(text) > 400:
        raise ValueError("Script too long for ElevenLabs TTS (400 char limit).")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID.get(lang, VOICE_ID['en'])}"
    r = requests.post(
        url,
        headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=60,
    )
    r.raise_for_status()

    out_path = temp_file(".mp3")
    out_path.write_bytes(r.content)
    return out_path

# ───────────────────────── VIDEO BUILD ─────────────────────────
def _choose_font() -> str:
    try:
        TextClip("test", font=FONT_PREFERRED)  # probe
        return FONT_PREFERRED
    except Exception:
        return FONT_FALLBACK

def build_video(clips: List[Path], audio: Path, script: str, out_path: Path) -> None:
    segment = TARGET_DURATION / float(len(clips))
    video_clips = [VideoFileClip(str(p)).subclip(0, segment) for p in clips]
    vid = concatenate_videoclips(video_clips, method="compose")
    vid = vid.set_audio(AudioFileClip(str(audio)))

    caption = TextClip(
        textwrap.fill(script, 30),
        fontsize=60,
        font=_choose_font(),
        color="white",
        stroke_color="black",
        stroke_width=2,
        size=(vid.w * 0.9, None),
        method="caption",
    )

    final = CompositeVideoClip(
        [vid, caption.set_position(("center", "bottom")).set_duration(vid.duration)]
    )

    final.write_videofile(
        str(out_path),
        codec="libx264",
        audio_codec="aac",
        fps=30,
        preset="ultrafast",
        threads=4,
        logger=None,
    )

    # Cleanup to avoid leaking file handles in CI runners
    final.close()
    caption.close()
    for c in video_clips:
        c.close()
    vid.close()

# ───────────────────────── YOUTUBE UPLOAD ──────────────────────
def upload_short(video: Path, title: str, description: str) -> None:
    try:
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.errors import HttpError  # type: ignore
    except ImportError:
        print("google-api-python-client not installed – skipping upload.")
        return

    token = os.getenv("YT_REFRESH_TOKEN")
    if not token:
        print("YT_REFRESH_TOKEN not set – skipping upload.")
        return

    creds = Credentials.from_authorized_user_info({"refresh_token": token})
    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "27",  # Education
        },
        "status": {"privacyStatus": "public"},
    }

    media = MediaFileUpload(str(video), resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    print("Uploading…", end="", flush=True)
    try:
        while True:
            status, response = request.next_chunk()
            if response:
                print(f" done → https://youtu.be/{response.get('id')}")
                break
            if status:
                print(f" {status.progress() * 100:.1f}%", end="", flush=True)
    except HttpError as e:
        print("\nYouTube upload failed:", e)

# ────────────────────────── MISC HELPERS ───────────────────────
TOPICS = [
    "quantum computing",
    "Mars colonization",
    "deep-sea creatures",
    "ancient Egyptian tech",
    "AI art",
    "sustainable architecture",
]

def pick_topic() -> str:
    return random.choice(TOPICS)

# ─────────────────────────── MAIN FLOW ─────────────────────────
def run_once(lang: str, upload: bool, dry_run: bool = False) -> None:
    topic = pick_topic()
    script = generate_script(topic, lang)
    print("[SCRIPT]\n" + script + "\n")

    if dry_run:
        print("Dry-run: skipping video/TTS/download/upload.")
        return

    # 1. Fetch 3 vertical clips (keywords = first 3 words of topic)
    keywords = topic.split()[:3]
    clips = [fetch_vertical_clip(k) for k in keywords]

    # 2. TTS
    voice = generate_voiceover(script, lang)

    # 3. Build final video
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outfile = WORKDIR / f"short_{timestamp}.mp4"
    build_video(clips, voice, script, outfile)
    print("Video saved →", outfile)

    # 4. Optional upload
    if upload:
        title = ("3 facts about " if lang == "en" else "3 fatti su ") + topic
        upload_short(outfile, title, script)

# ───────────────────────────── CLI ─────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser("Faceless Shorts generator")
    parser.add_argument(
        "--lang", choices=["en", "it"], default=DEFAULT_LANG, help="Language of the short"
    )
    parser.add_argument("--upload", action="store_true", help="Upload the resulting video to YouTube")
    parser.add_argument("--dry-run", action="store_true", help="Skip network-heavy steps (for CI smoke test)")
    args = parser.parse_args()

    try:
        run_once(args.lang, upload=args.upload, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("Aborted by user.")
    except Exception as exc:
        print("Fatal error:", exc)
        sys.exit(1)

