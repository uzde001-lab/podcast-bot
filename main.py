import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from google import genai
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=1)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
TELEGRAM_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
MAX_INPUT_MB = 500
MAX_DURATION_SECONDS = 60 * 60
YOUTUBE_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s]+|shorts/[^\s]+|live/[^\s]+)|youtu\.be/[^\s]+)", re.I)


def tg(method, payload):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN sozlanmagan")
    response = requests.post(f"{TELEGRAM_API}/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"Telegram {method} xatosi"))
    return data.get("result")


def send(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text})


def set_webhook():
    if BOT_TOKEN and PUBLIC_URL:
        try:
            tg("setWebhook", {"url": f"{PUBLIC_URL}/telegram/webhook", "drop_pending_updates": False})
            print(f"Telegram webhook set: {PUBLIC_URL}/telegram/webhook", flush=True)
        except Exception as exc:
            print(f"Webhook setup failed: {exc}", flush=True)


def youtube_url(text):
    match = YOUTUBE_RE.search(text or "")
    return match.group(0).rstrip("),.!?") if match else ""


def ffmpeg_path():
    local = shutil.which("ffmpeg")
    if local:
        return local
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(f"FFmpeg topilmadi: {exc}") from exc


def run_download(url, folder):
    output = str(Path(folder) / "audio.%(ext)s")
    # YouTube turli server IP/client kombinatsiyalariga turlicha javob beradi.
    client_profiles = ["web_safari", "mweb", "tv", "default"]
    errors = []
    for player_client in client_profiles:
        command = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
            "--retries", "2", "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "7", "-f", "bestaudio/best", "--remote-components", "ejs:github",
            "--js-runtimes", "deno", "--extractor-args", f"youtube:player_client={player_client}",
            "--ffmpeg-location", ffmpeg_path(), "--match-filter",
            f"duration <= {MAX_DURATION_SECONDS}", "-o", output, url,
        ]
        result = subprocess.run(command, cwd=folder, text=True, capture_output=True, timeout=900)
        files = list(Path(folder).glob("audio.*"))
        if result.returncode == 0 and files:
            return files[0]
        detail = (result.stderr or result.stdout or "no response")[-1000:]
        errors.append(f"{player_client}: {detail}")

    detail = errors[-1] if errors else "no response"
    if any("Sign in to confirm" in error or "Failed to extract any player response" in error for error in errors):
        raise RuntimeError("YouTube Render serveriga player javobini bermadi. Deno/EJS va alternativ clientlar ham sinandi. Audio/video faylni Telegramga yuboring yoki shu videoning subtitrli nusxasidan foydalaning.")
    raise RuntimeError(f"YouTube yuklash xatosi: {detail}")


def telegram_file(file_id, folder, filename="media.bin"):
    info = tg("getFile", {"file_id": file_id})
    path = info.get("file_path")
    if not path:
        raise RuntimeError("Telegram fayl manzili topilmadi")
    response = requests.get(f"{TELEGRAM_API}/file/bot{BOT_TOKEN}/{path}", stream=True, timeout=120)
    response.raise_for_status()
    target = Path(folder) / Path(filename).name
    size = 0
    with target.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_INPUT_MB * 1024 * 1024:
                raise RuntimeError("Fayl 500 MB dan katta")
            handle.write(chunk)
    return target


def normalize_audio(source, folder):
    output = Path(folder) / "audio.mp3"
    result = subprocess.run([
        ffmpeg_path(), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(output)
    ], capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"Video/audio o‘qilmadi: {(result.stderr or '')[-1200:]}")
    return output


def transcribe(audio_path):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY sozlanmagan")
    client = genai.Client(api_key=GEMINI_API_KEY)
    uploaded = client.files.upload(file=str(audio_path), config={"mime_type": "audio/mp3"})
    interaction = client.interactions.create(
        model="gemini-3.5-transcribe",
        input=[{"type": "audio", "uri": uploaded.uri, "mime_type": getattr(uploaded, "mime_type", None) or "audio/mp3"}],
        generation_config={"transcription_config": {"language_codes": []}},
    )
    text = (getattr(interaction, "output_text", "") or "").strip()
    if not text:
        raise RuntimeError("Nutq aniqlanmadi")
    return text


def make_pdf(text, title="Podcast transkripti"):
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleUz", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=23, textColor="#12304a", spaceAfter=8)
    body_style = ParagraphStyle("BodyUz", parent=styles["BodyText"], fontName="Helvetica", fontSize=10.5, leading=16, spaceAfter=8)
    story = [Paragraph(title, title_style), Paragraph("Gemini transkripsiyasi • Nemischa Podcast Bot", styles["Normal"]), Spacer(1, 8)]
    for paragraph in re.split(r"\n\s*\n", text.replace("&", "&amp;")):
        safe = paragraph.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
        story.append(Paragraph(safe, body_style))
    doc.build(story)
    return output.getvalue()


def send_pdf(chat_id, pdf):
    files = {"document": ("transkript.pdf", pdf, "application/pdf")}
    response = requests.post(f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendDocument", data={"chat_id": str(chat_id)}, files=files, timeout=120)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError(response.json().get("description", "PDF yuborilmadi"))


def process(chat_id, link=None, file_ref=None):
    folder = tempfile.mkdtemp(prefix="podcast-")
    try:
        send(chat_id, "⏳ Qayta ishlanmoqda. Audio ajratiladi, Gemini matnga aylantiradi va PDF tayyorlaydi.")
        if link:
            audio = run_download(link, folder)
        else:
            source = telegram_file(file_ref["file_id"], folder, file_ref.get("file_name", "media.bin"))
            audio = normalize_audio(source, folder)
        text = transcribe(audio)
        send_pdf(chat_id, make_pdf(text))
        send(chat_id, "✅ Tayyor. To‘liq transkript PDF fayl sifatida yuborildi.")
    except Exception as exc:
        print(f"job failed chat={chat_id}: {exc}", flush=True)
        try:
            send(chat_id, f"❌ Ishlov berilmadi:\n{exc}")
        except Exception as send_exc:
            print(f"error message failed: {send_exc}", flush=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def handle(update):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    text = (message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        send(chat_id, "Podcast Transcript Bot\n\nYouTube havolasini yoki video/audio faylni yuboring. Bot to‘liq matnni PDF qilib qaytaradi.")
        return
    link = youtube_url(text)
    file_ref = message.get("video") or message.get("audio") or message.get("document")
    if link:
        executor.submit(process, chat_id, link, None)
    elif file_ref:
        executor.submit(process, chat_id, None, file_ref)
    elif text:
        send(chat_id, "YouTube havolasini yoki video/audio faylni yuboring.")


@app.post("/telegram/webhook")
def webhook():
    update = request.get_json(silent=True) or {}
    threading.Thread(target=handle, args=(update,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/")
def home():
    return "Podcast Transcript Bot is running"


@app.get("/health")
def health():
    return jsonify(ok=True, telegram=bool(BOT_TOKEN), gemini=bool(GEMINI_API_KEY), max_input_mb=MAX_INPUT_MB, max_duration_minutes=60)


set_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
