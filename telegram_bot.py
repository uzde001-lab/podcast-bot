"""
YouTube / audio -> matn -> PDF | DOCX | TXT | SRT   (Telegram bot, Render uchun tayyor)

Muhit o'zgaruvchilari (Render -> Environment):
  BOT_TOKEN       (majburiy)  @BotFather tokeni
  GROQ_API_KEY    (majburiy)  console.groq.com
  ADMIN_IDS       (ixtiyoriy) "123,456"  - limitsiz + /stats
  YT_PROXY        (ixtiyoriy) "http://user:pass@host:port"  - YouTube IP bloklasa
  YT_COOKIES_B64  (ixtiyoriy) cookies.txt ning base64 ko'rinishi
  YT_COOKIES      (ixtiyoriy) cookies.txt yo'li (masalan /etc/secrets/cookies.txt)
"""
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import textwrap
import time
import urllib.request
from datetime import date
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from docx import Document
from groq import BadRequestError, Groq, RateLimitError
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

# ====================== SOZLAMALAR ======================
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
YT_PROXY = os.getenv("YT_PROXY") or None
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(("podcast-bot:" + BOT_TOKEN).encode()).hexdigest()

CHUNK_SECONDS = 600
PARALLEL_CHUNKS = 5
MAX_VIDEO_MINUTES = 180
MAX_PARALLEL_JOBS = 2
DAILY_LIMIT = 8
WHISPER_MODEL = "whisper-large-v3-turbo"   # Audio -> matn modeli.
LLM_MODEL = "openai/gpt-oss-20b"            # Tezkor Groq model; jonli model ro'yxatidan tekshirildi.
LLM_PART_CHARS = 8000
TG_FILE_LIMIT = 20 * 1024 * 1024           # Telegram bot API yuklab olish chegarasi
DB_PATH = os.getenv("DB_PATH", "bot.db")
FONT_DIR = Path(__file__).parent
# ========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise SystemExit("BOT_TOKEN va GROQ_API_KEY o'rnatilmagan! (Render -> Environment)")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
groq = Groq(api_key=GROQ_API_KEY, max_retries=0)
job_sem = asyncio.Semaphore(MAX_PARALLEL_JOBS)
chunk_sem = asyncio.Semaphore(PARALLEL_CHUNKS)
active: set[int] = set()
pending: dict[str, dict] = {}

ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")
SENT_END = re.compile(r"[.!?…»\"”]$")
LANGS = {"auto": "🌐 Avto", "uz": "🇺🇿 O'zbek", "ru": "🇷🇺 Rus", "en": "🇬🇧 Ingliz", "tr": "🇹🇷 Turk"}
CLIENT_ATTEMPTS = [None, ["tv"], ["android_vr"], ["web_safari"], ["mweb"]]
FATAL_YT = ("Private video", "Video unavailable", "has been removed", "copyright", "members-only",
            "age-restricted", "This live event")

COOKIE_FILE = None


def setup_cookies() -> None:
    """Cookie faylni yozish mumkin bo'lgan joyga ko'chiradi (yt-dlp unga yozishi mumkin)."""
    global COOKIE_FILE
    dst = "/tmp/yt_cookies.txt"
    b64 = os.getenv("YT_COOKIES_B64")
    src = os.getenv("YT_COOKIES")
    try:
        if b64:
            Path(dst).write_bytes(base64.b64decode(b64))
            COOKIE_FILE = dst
        elif src and os.path.exists(src):
            shutil.copy(src, dst)
            COOKIE_FILE = dst
    except Exception as e:
        log.warning("Cookie sozlanmadi: %s", e)
    log.info("Cookie: %s | Proxy: %s | ffmpeg: %s | deno: %s", bool(COOKIE_FILE), bool(YT_PROXY),
             bool(shutil.which("ffmpeg")), bool(shutil.which("deno")))


# ---------------------- BAZA ----------------------
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, lang TEXT DEFAULT 'auto', joined TEXT);
CREATE TABLE IF NOT EXISTS usage(user_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS cache(video_id TEXT, lang TEXT, title TEXT, duration INTEGER,
                                 segments TEXT, summary TEXT, PRIMARY KEY(video_id, lang));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                                video_id TEXT, fmt TEXT, minutes REAL, day TEXT);
"""


def _connect_database():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.executescript(DB_SCHEMA)
    connection.commit()
    return connection


db = _connect_database()


def reinitialize_process_state() -> None:
    """Reopen process-bound clients after Gunicorn forks a WSGI worker."""
    global db, bot, groq, job_sem, chunk_sem, active, pending
    try:
        db.close()
    except sqlite3.Error:
        pass
    db = _connect_database()
    bot = Bot(BOT_TOKEN)
    groq = Groq(api_key=GROQ_API_KEY, max_retries=0)
    job_sem = asyncio.Semaphore(MAX_PARALLEL_JOBS)
    chunk_sem = asyncio.Semaphore(PARALLEL_CHUNKS)
    active = set()
    pending = {}


def touch_user(u) -> None:
    db.execute("INSERT OR IGNORE INTO users(id,name,joined) VALUES(?,?,?)",
               (u.id, u.full_name, date.today().isoformat()))
    db.commit()


def get_lang(uid: int) -> str:
    r = db.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return r[0] if r else "auto"


def used_today(uid: int) -> int:
    r = db.execute("SELECT n FROM usage WHERE user_id=? AND day=?", (uid, date.today().isoformat())).fetchone()
    return r[0] if r else 0


def over_limit(uid: int) -> bool:
    return uid not in ADMIN_IDS and used_today(uid) >= DAILY_LIMIT


def add_usage(uid: int, vid: str, fmt: str, minutes: float) -> None:
    day = date.today().isoformat()
    db.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET n=n+1", (uid, day))
    db.execute("INSERT INTO jobs(user_id,video_id,fmt,minutes,day) VALUES(?,?,?,?,?)", (uid, vid, fmt, minutes, day))
    db.commit()


def cache_get(vid: str, lang: str):
    r = db.execute("SELECT title,duration,segments,summary FROM cache WHERE video_id=? AND lang=?",
                   (vid, lang)).fetchone()
    return (r[0], r[1], json.loads(r[2]), r[3]) if r else None


def cache_put(vid, lang, title, duration, segs) -> None:
    db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,NULL)",
               (vid, lang, title, duration, json.dumps(segs, ensure_ascii=False)))
    db.commit()


def cache_set_summary(vid, lang, summary) -> None:
    db.execute("UPDATE cache SET summary=? WHERE video_id=? AND lang=?", (summary, vid, lang))
    db.commit()


# ---------------------- YORDAMCHI ----------------------
def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def srt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, r = divmod(ms, 3600000)
    m, r = divmod(r, 60000)
    s, ms = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-. ]", "", s, flags=re.U).strip()[:60] or "video"


def friendly_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "sign in to confirm" in low or "not a bot" in low or "http error 403" in low:
        return ("YouTube serverni vaqtincha blokladi.\n"
                "Admin: YT_PROXY yoki cookies (YT_COOKIES_B64) sozlashi kerak. "
                "Hozircha videoning audiosini shu yerga fayl qilib yuborishingiz mumkin (20 MB gacha).")
    if "failed to extract any player response" in low or "unable to extract initial player response" in low:
        return ("YouTube bu videoni bot serveriga ochib bermadi; bu ko‘pincha YouTube cheklovi yoki "
                "video ruxsatiga bog‘liq. Bot egasi Render → Environment’da ruxsatli YT_PROXY yoki "
                "YT_COOKIES_B64 sozlashi kerak. Cookie maxfiy: chatga yubormang, akkauntga xavf tug‘dirishi mumkin. "
                "Hozircha audio/video faylni Telegram orqali yuboring (20 MB gacha).")
    if "private video" in low:
        return "Bu video yopiq (private)."
    if "unavailable" in low or "removed" in low:
        return "Video mavjud emas yoki o'chirilgan."
    if "age" in low and "restrict" in low:
        return "Videoga yosh cheklovi qo'yilgan."
    if "429" in s or "rate limit" in low:
        return "Xizmat band (limit). Bir necha daqiqadan keyin urinib ko'ring."
    return s[:300]


def ensure_fonts() -> None:
    base = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/"
    candidates = [FONT_DIR, Path("/usr/share/fonts/truetype/dejavu")]
    for fname, name in (("DejaVuSans.ttf", "DejaVu"), ("DejaVuSans-Bold.ttf", "DejaVu-Bold")):
        path = next((d / fname for d in candidates if (d / fname).exists()), None)
        if path is None:
            path = FONT_DIR / fname
            log.info("Shrift yuklanmoqda: %s", fname)
            urllib.request.urlretrieve(base + fname, path)
        pdfmetrics.registerFont(TTFont(name, str(path)))


def _opts(clients=None, extra=None) -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "retries": 5, "socket_timeout": 30}
    if YT_PROXY:
        o["proxy"] = YT_PROXY
    if COOKIE_FILE:
        o["cookiefile"] = COOKIE_FILE
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    if extra:
        o.update(extra)
    return o


def get_info(url: str) -> dict:
    last = None
    for clients in CLIENT_ATTEMPTS:
        try:
            with yt_dlp.YoutubeDL(_opts(clients)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last = e
            log.warning("get_info (%s) xato: %s", clients, str(e)[:150])
            if any(k in str(e) for k in FATAL_YT):
                break
    raise last


def download_audio(url: str, out_dir: str) -> str:
    last = None
    for clients in CLIENT_ATTEMPTS:
        for f in os.listdir(out_dir):
            if f.startswith("audio."):
                os.remove(os.path.join(out_dir, f))
        try:
            opts = _opts(clients, {"format": "bestaudio/best",
                                   "outtmpl": os.path.join(out_dir, "audio.%(ext)s"),
                                   "concurrent_fragment_downloads": 8})
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            for f in os.listdir(out_dir):
                if f.startswith("audio.") and not f.endswith(".part"):
                    return os.path.join(out_dir, f)
            raise RuntimeError("Audio fayl topilmadi")
        except Exception as e:
            last = e
            log.warning("download (%s) xato: %s", clients, str(e)[:150])
            if any(k in str(e) for k in FATAL_YT):
                break
    raise last


def ffmpeg_path() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg topilmadi; audio bo‘laklash imkonsiz") from exc


async def split_audio(src: str, out_dir: str) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
        os.path.join(out_dir, "chunk_%04d.mp3"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Audio o'qib bo'lmadi: {err.decode()[-200:]}")
    files = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("chunk_"))
    if not files:
        raise RuntimeError("Audioda ovoz topilmadi")
    return files


# ---------------------- PROGRESS ----------------------
class Progress:
    def __init__(self, msg: Message):
        self.msg, self.last, self.body = msg, 0.0, ""

    async def update(self, text: str, pct: float, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last < 2.5:
            return
        n = max(0, min(10, int(pct / 10)))
        body = f"{text}\n{'▰' * n}{'▱' * (10 - n)} {int(pct)}%"
        if body == self.body:
            return
        try:
            await self.msg.edit_text(body)
            self.last, self.body = now, body
        except Exception:
            pass


# ---------------------- TRANSKRIPSIYA ----------------------
def _transcribe_sync(path: str, lang: str) -> list[dict]:
    with open(path, "rb") as f:
        data = f.read()
    kw = dict(file=(os.path.basename(path), data), model=WHISPER_MODEL,
              response_format="verbose_json", timestamp_granularities=["segment"], temperature=0.0)
    if lang != "auto":
        kw["language"] = lang
    res = groq.audio.transcriptions.create(**kw)
    segs = getattr(res, "segments", None)
    if segs is None and isinstance(res, dict):
        segs = res.get("segments")
    out = []
    for s in segs or []:
        g = (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
        text = (g("text") or "").strip()
        # "hayoliy matn" filtri: sukunat paytidagi bema'ni takrorlar
        if not text or (g("no_speech_prob", 0) > 0.8 and g("avg_logprob", 0) < -1.0):
            continue
        out.append({"s": float(g("start")), "e": float(g("end")), "t": text})
    return out


async def transcribe_chunk(path: str, lang: str) -> list[dict]:
    if os.path.getsize(path) < 4096:      # juda qisqa bo'lak (oxirgi qoldiq)
        return []
    async with chunk_sem:
        for attempt in range(6):
            try:
                return await asyncio.to_thread(_transcribe_sync, path, lang)
            except BadRequestError as e:
                log.warning("Bo'lak o'tkazib yuborildi: %s", str(e)[:120])
                return []
            except RateLimitError:
                log.warning("Groq limit, kutamiz (urinish %d)", attempt + 1)
                await asyncio.sleep(15 * (attempt + 1))
            except Exception as e:
                log.warning("Bo'lak xatosi: %s (urinish %d)", str(e)[:120], attempt + 1)
                await asyncio.sleep(4 * (attempt + 1))
    raise RuntimeError("Groq xizmati javob bermadi yoki limit tugadi. Birozdan so'ng urinib ko'ring.")


async def transcribe_file(audio: str, lang: str, tmp: str, prog: Progress) -> list[dict]:
    await prog.update("✂️ Audio tayyorlanmoqda", 20, True)
    cdir = os.path.join(tmp, "chunks")
    os.makedirs(cdir, exist_ok=True)
    files = await split_audio(audio, cdir)
    try:
        os.remove(audio)
    except OSError:
        pass
    done = 0

    async def run(i: int, f: str):
        nonlocal done
        segs = await transcribe_chunk(f, lang)
        done += 1
        await prog.update(f"🎙 Matnga aylantirilmoqda ({done}/{len(files)})", 25 + 60 * done / len(files))
        off = i * CHUNK_SECONDS
        return [{"s": x["s"] + off, "e": x["e"] + off, "t": x["t"]} for x in segs]

    parts = await asyncio.gather(*(run(i, f) for i, f in enumerate(files)))
    flat, prev = [], None
    for part in parts:
        for s in part:
            if prev and s["t"] == prev:   # ketma-ket bir xil gaplarni olib tashlash
                continue
            flat.append(s)
            prev = s["t"]
    return flat


async def transcribe_video(vid: str, lang: str, tmp: str, prog: Progress) -> list[dict]:
    await prog.update("⬇️ Audio yuklanmoqda", 5, True)
    audio = await asyncio.to_thread(download_audio, f"https://www.youtube.com/watch?v={vid}", tmp)
    return await transcribe_file(audio, lang, tmp, prog)


# ---------------------- MATN TUZISH ----------------------
def build_paragraphs(segs: list[dict]) -> list[tuple[float, str]]:
    paras, cur, cur_start, sc, prev_end = [], "", 0.0, 0, None
    for s in segs:
        gap = (s["s"] - prev_end) if prev_end is not None else 0
        if cur and SENT_END.search(cur) and (gap >= 1.5 or sc >= 5 or len(cur) > 900):
            paras.append((cur_start, cur))
            cur, sc = "", 0
        if not cur:
            cur_start = s["s"]
        cur += (" " if cur else "") + s["t"]
        sc += len(re.findall(r"[.!?…]+(?:\s|$)", s["t"]))
        prev_end = s["e"]
    if cur:
        paras.append((cur_start, cur))
    out = []
    for st, p in paras:
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        p = p[0].upper() + p[1:]
        if not SENT_END.search(p):
            p += "."
        out.append((st, p))
    return out


def clean_md(s: str) -> str:
    s = re.sub(r"[*_`#]+", "", s)
    return re.sub(r"^\s*[-•]\s*", "• ", s, flags=re.M).strip()


# ---------------------- AI XULOSA ----------------------
def _llm(system: str, text: str) -> str:
    for attempt in range(4):
        try:
            r = groq.chat.completions.create(
                model=LLM_MODEL, temperature=0.3, max_tokens=1200,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": text}])
            return r.choices[0].message.content.strip()
        except Exception as e:
            log.warning("LLM xatosi: %s", str(e)[:120])
            time.sleep(8 * (attempt + 1))
    raise RuntimeError("LLM javob bermadi")


def summarize(text: str) -> str:
    same = "Write in the SAME language as the transcript. No markdown symbols except '-' for bullets."
    parts = [text[i:i + LLM_PART_CHARS] for i in range(0, len(text), LLM_PART_CHARS)]
    notes = [_llm(f"Summarize this part of a video transcript in 5-8 key points. {same}", p) for p in parts]
    return _llm(
        "You get notes from a video transcript. Produce: first a short overview paragraph (4-6 sentences), "
        "then a line 'Asosiy fikrlar:' (translate this label to the transcript language) followed by 6-10 "
        f"bullet points starting with '-'. {same}", "\n\n".join(notes))


# ---------------------- FAYL YARATISH ----------------------
def make_pdf(title, url, paras, summary, out_path) -> None:
    ensure_fonts()
    st_title = ParagraphStyle("t", fontName="DejaVu-Bold", fontSize=16, leading=21, spaceAfter=6)
    st_meta = ParagraphStyle("m", fontName="DejaVu", fontSize=9, leading=12, textColor="#666666", spaceAfter=14)
    st_h = ParagraphStyle("h", fontName="DejaVu-Bold", fontSize=12, leading=16, spaceBefore=6, spaceAfter=8)
    st_body = ParagraphStyle("b", fontName="DejaVu", fontSize=11, leading=17, alignment=TA_JUSTIFY, spaceAfter=10)
    st_sum = ParagraphStyle("s", parent=st_body, backColor="#f3f6fb", borderPadding=8, spaceAfter=8)

    doc = SimpleDocTemplate(out_path, pagesize=A4, title=title, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    story = [Paragraph(html.escape(title), st_title)]
    story.append(Paragraph(html.escape(url) if url else "Telegram orqali yuborilgan fayl", st_meta))
    if summary:
        story.append(Paragraph("Qisqacha mazmuni", st_h))
        for line in clean_md(summary).split("\n"):
            if line.strip():
                story.append(Paragraph(html.escape(line.strip()), st_sum))
        story += [Spacer(1, 10), Paragraph("To'liq matn", st_h)]
    for start, p in paras:
        if url:
            link = html.escape(f"{url}&t={int(start)}s")
            ts = f'<a href="{link}" color="#1a56db"><font size="8">[{fmt_ts(start)}]</font></a> '
        else:
            ts = f'<font size="8" color="#1a56db">[{fmt_ts(start)}]</font> '
        story.append(Paragraph(ts + html.escape(p), st_body))
    doc.build(story)


def make_docx(title, url, paras, summary, out_path) -> None:
    d = Document()
    d.add_heading(title, level=1)
    if url:
        d.add_paragraph(url)
    if summary:
        d.add_heading("Qisqacha mazmuni", level=2)
        for line in clean_md(summary).split("\n"):
            if line.strip():
                d.add_paragraph(line.strip())
        d.add_heading("To'liq matn", level=2)
    for start, p in paras:
        d.add_paragraph(f"[{fmt_ts(start)}] {p}")
    d.save(out_path)


def make_txt(title, url, paras, out_path) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{title}\n{url}\n\n")
        for start, p in paras:
            f.write(f"[{fmt_ts(start)}] {p}\n\n")


def make_srt(segs, out_path) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{srt_ts(s['s'])} --> {srt_ts(max(s['e'], s['s'] + 0.5))}\n"
                    f"{textwrap.fill(s['t'], 42)}\n\n")


# ---------------------- HANDLERLAR ----------------------
HELP = ("Menga YouTube havolasini yuboring — videodagi barcha gaplarni matnga aylantirib beraman.\n"
        "Yoki audio / ovozli xabar / video fayl yuboring (20 MB gacha).\n\n"
        "📄 PDF • 🧠 PDF + xulosa • 📝 DOCX • 📃 TXT • 🎬 SRT (subtitr)\n\n"
        "/lang — video tilini tanlash\n/help — yordam")


def format_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 PDF", callback_data=f"fmt:pdf:{key}"),
         InlineKeyboardButton(text="🧠 PDF + xulosa", callback_data=f"fmt:pdfs:{key}")],
        [InlineKeyboardButton(text="📝 DOCX", callback_data=f"fmt:docx:{key}"),
         InlineKeyboardButton(text="📃 TXT", callback_data=f"fmt:txt:{key}"),
         InlineKeyboardButton(text="🎬 SRT", callback_data=f"fmt:srt:{key}")],
    ])


@dp.message(CommandStart())
@dp.message(Command("help"))
async def start(m: Message):
    touch_user(m.from_user)
    await m.answer("Salom! 👋\n" + HELP)


@dp.message(Command("lang"))
async def lang_cmd(m: Message):
    touch_user(m.from_user)
    cur = get_lang(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("✅ " if k == cur else "") + v, callback_data=f"lang:{k}")
    ] for k, v in LANGS.items()])
    await m.answer("Video qaysi tilda? (To'g'ri til tanlansa, aniqlik oshadi)", reply_markup=kb)


@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(cb: CallbackQuery):
    code = cb.data.split(":")[1]
    touch_user(cb.from_user)
    db.execute("UPDATE users SET lang=? WHERE id=?", (code, cb.from_user.id))
    db.commit()
    await cb.answer("Saqlandi ✅")
    await cb.message.edit_text(f"Til: {LANGS.get(code, code)}")


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
    today = date.today().isoformat()
    await m.answer(
        f"👥 Foydalanuvchilar: {q('SELECT COUNT(*) FROM users')}\n"
        f"📹 Bugungi ishlar: {q('SELECT COUNT(*) FROM jobs WHERE day=?', today)}\n"
        f"📹 Jami ishlar: {q('SELECT COUNT(*) FROM jobs')}\n"
        f"⏱ Jami daqiqa: {q('SELECT SUM(minutes) FROM jobs'):.0f}\n"
        f"💾 Keshdagi yozuvlar: {q('SELECT COUNT(*) FROM cache')}")


@dp.message(F.text.func(lambda t: bool(ID_RE.search(t))))
async def handle_link(m: Message):
    touch_user(m.from_user)
    vid = ID_RE.search(m.text).group(1)
    status = await m.answer("🔎 Video tekshirilmoqda...")
    try:
        meta = pending.get(vid)
        if not meta:
            info = await asyncio.to_thread(get_info, f"https://www.youtube.com/watch?v={vid}")
            if info.get("is_live"):
                await status.edit_text("❌ Jonli efirni qayta ishlab bo'lmaydi.")
                return
            meta = {"title": info.get("title", "Video"), "duration": info.get("duration") or 0}
            if len(pending) > 500:
                pending.clear()
            pending[vid] = meta
        mins = meta["duration"] / 60
        if mins > MAX_VIDEO_MINUTES:
            await status.edit_text(f"❌ Video juda uzun ({mins:.0f} daq). Maksimum: {MAX_VIDEO_MINUTES} daq.")
            return
    except Exception as e:
        log.exception("Info xato")
        await status.edit_text(f"❌ {friendly_error(e)}")
        return
    await status.edit_text(f"🎬 {meta['title']}\n⏱ {mins:.0f} daqiqa\n\nQaysi formatda yuboray?",
                           reply_markup=format_kb(vid))


MEDIA_FILTER = (F.audio | F.voice | F.video | F.video_note |
                (F.document & F.document.mime_type.func(
                    lambda x: bool(x) and (x.startswith("audio/") or x.startswith("video/")))))


@dp.message(MEDIA_FILTER)
async def handle_media(m: Message):
    touch_user(m.from_user)
    uid = m.from_user.id
    obj = m.audio or m.voice or m.video or m.video_note or m.document
    if obj.file_size and obj.file_size > TG_FILE_LIMIT:
        await m.answer("❌ Fayl 20 MB dan katta (Telegram cheklovi). YouTube havolasini yuboring "
                       "yoki faylni kichraytiring.")
        return
    if uid in active:
        await m.answer("⏳ Oldingi so'rovingiz hali tugamadi, kuting.")
        return
    key = "up:" + obj.file_unique_id
    lang = get_lang(uid)
    title = getattr(obj, "file_name", None) or getattr(obj, "title", None) or "Audio"
    duration = getattr(obj, "duration", 0) or 0
    status = await m.answer("⬇️ Fayl yuklanmoqda...")
    active.add(uid)
    tmp = tempfile.mkdtemp(prefix="ytbot_")
    try:
        if not cache_get(key, lang):
            if over_limit(uid):
                await status.edit_text(f"⚠️ Kunlik limit tugadi ({DAILY_LIMIT} ta). Ertaga qaytib keling.")
                return
            path = os.path.join(tmp, "media.bin")
            await bot.download(obj, destination=path)
            prog = Progress(status)
            async with job_sem:
                segs = await transcribe_file(path, lang, tmp, prog)
            if not segs:
                await status.edit_text("❌ Faylda gapirilgan matn topilmadi.")
                return
            cache_put(key, lang, title, duration, segs)
            add_usage(uid, key, "upload", duration / 60)
        await status.edit_text(f"🎧 {title}\n\nQaysi formatda yuboray?", reply_markup=format_kb(key))
    except Exception as e:
        log.exception("Media xato")
        await status.edit_text(f"❌ {friendly_error(e)}")
    finally:
        active.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


@dp.callback_query(F.data.startswith("fmt:"))
async def on_format(cb: CallbackQuery):
    _, fmt, vid = cb.data.split(":", 2)
    uid = cb.from_user.id
    await cb.answer()
    if uid in active:
        await cb.message.answer("⏳ Oldingi so'rovingiz hali tugamadi, kuting.")
        return
    active.add(uid)
    status = cb.message
    prog = Progress(status)
    tmp = tempfile.mkdtemp(prefix="ytbot_")
    is_upload = vid.startswith("up:")
    url = "" if is_upload else f"https://www.youtube.com/watch?v={vid}"
    try:
        lang = get_lang(uid)
        row = cache_get(vid, lang)
        if row:
            title, duration, segs, summary = row
            await prog.update("♻️ Tayyor natija topildi", 85, True)
        elif is_upload:
            await status.edit_text("⚠️ Fayl muddati o'tgan (bot qayta ishga tushgan). Iltimos, faylni qayta yuboring.")
            return
        else:
            if over_limit(uid):
                await status.edit_text(f"⚠️ Kunlik limit tugadi ({DAILY_LIMIT} ta yangi video). Ertaga qaytib keling.")
                return
            meta = pending.get(vid)
            if not meta:
                info = await asyncio.to_thread(get_info, url)
                meta = {"title": info.get("title", "Video"), "duration": info.get("duration") or 0}
            title, duration, summary = meta["title"], meta["duration"], None
            if job_sem.locked():
                await status.edit_text("⏳ Navbatda turibsiz, biroz kuting...")
            async with job_sem:
                segs = await transcribe_video(vid, lang, tmp, prog)
            if not segs:
                await status.edit_text("❌ Videoda gapirilgan matn topilmadi.")
                return
            cache_put(vid, lang, title, duration, segs)
            add_usage(uid, vid, fmt, duration / 60)

        paras = build_paragraphs(segs)
        if fmt == "pdfs" and not summary:
            await prog.update("🧠 Xulosa tayyorlanmoqda", 90, True)
            try:
                summary = await asyncio.to_thread(summarize, " ".join(p for _, p in paras))
                cache_set_summary(vid, lang, summary)
            except Exception:
                log.exception("Xulosa xatosi")
                summary = None

        await prog.update("📄 Fayl tayyorlanmoqda", 95, True)
        base = os.path.join(tmp, safe_name(title))
        if fmt in ("pdf", "pdfs"):
            path = base + ".pdf"
            await asyncio.to_thread(make_pdf, title, url, paras, summary if fmt == "pdfs" else None, path)
        elif fmt == "docx":
            path = base + ".docx"
            await asyncio.to_thread(make_docx, title, url, paras, summary, path)
        elif fmt == "txt":
            path = base + ".txt"
            await asyncio.to_thread(make_txt, title, url, paras, path)
        else:
            path = base + ".srt"
            await asyncio.to_thread(make_srt, segs, path)

        note = "" if (fmt != "pdfs" or summary) else "\n⚠️ Xulosa tayyorlanmadi, faqat matn yuborildi."
        await cb.message.answer_document(FSInputFile(path), caption=f"✅ {title}{note}")
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.exception("Xato")
        try:
            await status.edit_text(f"❌ {friendly_error(e)}")
        except Exception:
            pass
    finally:
        active.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message()
async def other(m: Message):
    await m.answer("Iltimos, YouTube havolasini yoki audio fayl yuboring.\n\n" + HELP)


# ---------------------- RENDER: webhook boshqaruvi ----------------------
async def configure_bot():
    setup_cookies()
    ensure_fonts()
    await bot.get_me()  # Tokenni ishga tushishda tekshiradi; qiymat logga chiqarilmaydi.
    if PUBLIC_URL:
        await bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("Telegram webhook ishga tushdi")
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        asyncio.create_task(dp.start_polling(bot))
        log.info("Mahalliy polling ishga tushdi")


async def main():
    await configure_bot()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
