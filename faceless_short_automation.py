""" Faceless YouTube-Shorts Automation – OpenAI-v1 & bilingue (EN/IT) """

from __future__ import annotations
import os, random, textwrap, tempfile, argparse, sys
from pathlib import Path
from datetime import datetime

import requests
from openai import OpenAI                # ✅ nuova interfaccia client v1
from moviepy.editor import (
    VideoFileClip, AudioFileClip, concatenate_videoclips,
    CompositeVideoClip, TextClip
)
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

# ────────────────────────── CONFIG ──────────────────────────
load_dotenv()
client            = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY")
ELEVEN_KEY        = os.getenv("ELEVENLABS_API_KEY")
HEADERS_PEXELS    = {"Authorization": PEXELS_API_KEY}

WORKDIR           = Path(tempfile.gettempdir()) / "short_builder"
WORKDIR.mkdir(exist_ok=True)
TARGET_DURATION   = 18                # sec
FONT              = "Montserrat-Bold"
DEFAULT_LANG      = os.getenv("LANGUAGE", "en")   # en / it

VOICE_ID = {                         # ElevenLabs voices
    "en": "EXAVITQu4vr4xnSDxMaL",
    "it": "TxGEqnHWrfWFTf9VQmLc",
}

# ─────────── one-time OAuth helper to get refresh token ───────────
def get_refresh_token() -> None:
    if not Path("client_secret.json").exists():
        sys.exit("ERROR: client_secret.json missing")
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json",
        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds = flow.run_local_server(port=0, prompt="consent")
    print("\nREFRESH_TOKEN:\n" + creds.refresh_token + "\n")
    print("→ incolla questo token nel secret YT_REFRESH_TOKEN")

# ──────────────────────────── OPENAI ─────────────────────────────
def generate_script(topic: str, lang: str) -> str:
    prompt = (
        f"Scrivi un copione divertente in 3 fatti su {topic} "
        f"in massimo 60 parole. Termina con una domanda." if lang == "it" else
        f"Write a fun, 3-fact script about {topic} in ≤60 words. "
        f"End with a question."
    )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo-0125",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=90,
    )
    return resp.choices[0].message.content.strip()

# ─────────────────────── PEXELS STOCK CLIP ───────────────────────
def fetch_vertical_clip(query: str) -> Path:
    r = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": query, "orientation": "vertical", "per_page": 10},
        headers=HEADERS_PEXELS, timeout=20)
    r.raise_for_status()
    vids = r.json().get("videos", [])
    if not vids:
        raise RuntimeError(f"No vertical clips for {query!r}")
    file_link = min(random.choice(vids)["video_files"],
                    key=lambda f: f["width"])["link"]
    out = WORKDIR / f"{random.randint(1e6, 1e7):.0f}.mp4"
    with requests.get(file_link, stream=True, timeout=60) as src, open(out, "wb") as dst:
        for chunk in src.iter_content(8192):
            dst.write(chunk)
    return out

# ───────────────────────── ELEVENLABS TTS ────────────────────────
def generate_voiceover(text: str, lang: str) -> Path:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID.get(lang,'en')}"
    r = requests.post(
        url,
        headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=60)
    r.raise_for_status()
    out = WORKDIR / "voice.mp3"
    out.write_bytes(r.content)
    return out

# ──────────────────────── VIDEO ASSEMBLY ─────────────────────────
def build_video(clips: list[Path], audio: Path, script: str, out_path: Path):
    seg = TARGET_DURATION / len(clips)
    vid = concatenate_videoclips(
        [VideoFileClip(str(p)).subclip(0, seg) for p in clips], method="compose")
    vid = vid.set_audio(AudioFileClip(str(audio)))
    caption = TextClip(
        textwrap.fill(script, 30),
        fontsize=60, font=FONT,
        color="white", stroke_color="black", stroke_width=2,
        size=(vid.w * 0.9, None), method="caption")
    final = CompositeVideoClip([vid, caption.set_position(("center", "bottom")).set_duration(vid.duration)])
    final.write_videofile(
        str(out_path),
        codec="libx264", audio_codec="aac",
        fps=30, preset="ultrafast", threads=4, logger=None)

# ──────────────────────── (OPTIONAL) UPLOAD ──────────────────────
def upload_short(video: Path, title: str, description: str):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("google-api-python-client missing → skip upload"); return
    creds = Credentials.from_authorized_user_info({"refresh_token": os.getenv("YT_REFRESH_TOKEN")})
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": title, "description": description, "categoryId": "27"},
            "status": {"privacyStatus": "public"}}
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), resumable=True))
    print("Uploading…", end="")
    while True:
        status, resp = req.next_chunk()
        if resp:
            print(" done →", resp.get("id")); break
        if status: print(f" {status.progress()*100:.1f}%", end="")

# ────────────────────────── MAIN ROUTINE ─────────────────────────
def pick_topic() -> str:
    return random.choice([
        "quantum computing", "Mars colonization", "deep-sea creatures",
        "ancient Egyptian tech", "AI art", "sustainable architecture"
    ])

def run_once(lang: str, upload: bool):
    topic  = pick_topic()
    script = generate_script(topic, lang)
    print("SCRIPT:\n" + script)
    clips  = [fetch_vertical_clip(k) for k in topic.split()[:3]]
    voice  = generate_voiceover(script, lang)
    out    = WORKDIR / f"short_{datetime.utcnow():%Y%m%d_%H%M%S}.mp4"
    build_video(clips, voice, script, out)
    print("Video saved →", out)
    if upload:
        title = ("3 facts about " if lang == "en" else "3 fatti su ") + topic
        upload_short(out, title, script)

# ──────────────────────────── CLI ────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser("Faceless Shorts generator")
    ap.add_argument("--auth",      action="store_true", help="Run OAuth only")
    ap.add_argument("--lang",      choices=["en","it"], default=DEFAULT_LANG, help="Language")
    ap.add_argument("--no-upload", action="store_true", help="Render without uploading")
    args = ap.parse_args()

    if args.auth:
        get_refresh_token(); sys.exit()

    run_once(args.lang, upload=not args.no_upload)
