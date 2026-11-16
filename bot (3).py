import ast
import asyncio
import copy
import glob
import hashlib
import hmac
import html
import io
import ipaddress
import json
import logging
import math
import operator
import os
import random
import re
import resource
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import wave
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import quote, urlparse

import cairosvg
import httpx
import imageio
import imageio_ffmpeg
import numpy as np
from aiohttp import web
from groq import AsyncGroq
from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, Update)
from telegram.error import InvalidToken
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx URL me bot token chhapta hai
log = logging.getLogger("bot")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
RS = Image.Resampling

# =====================================================================
#  SETTINGS: sab kuchh website ke /admin page se bharein
#  (Render me sirf ADMIN_PASSWORD zaroori hai)
# =====================================================================
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "/tmp/settings.json")
NOTES_FILE = os.getenv("NOTES_FILE", "/tmp/notes.json")
PORT = int(os.getenv("PORT", "10000"))
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "")
ADMIN_PW = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(8)
if not os.getenv("ADMIN_PASSWORD"):
    log.warning("ADMIN_PASSWORD set nahi hai. Is baar ka temporary password: %s", ADMIN_PW)

DEFAULT_PROMPT = ("Tum {BOT_NAME} ho, ek helpful AI assistant. User jis bhasha me likhe "
                  "(Hindi/Hinglish/English) usi me jawab do. Jawab saaf, seedha aur kaam ka ho.")
# (key, label, default, type, hint)
FIELDS = [
    ("TELEGRAM_TOKEN", "Telegram Bot Token", "", "secret", "@BotFather se. Aisa dikhta hai: 123456789:AAH..."),
    ("GROQ_API_KEY", "Groq API Key", "", "secret", "console.groq.com se. gsk_ se shuru hoti hai"),
    ("GROQ_MODEL", "Groq Model (main)", "openai/gpt-oss-120b", "text",
     "'auto' likhein to bot khud sabse powerful available model chunega. Neeche list dikhti hai"),
    ("FALLBACK_MODEL", "Backup model", "openai/gpt-oss-20b", "text", "Main model fail ya rate-limit ho to ye chalega"),
    ("WHISPER_MODEL", "Voice-to-text model", "whisper-large-v3-turbo", "text", "Voice message samajhne ke liye"),
    ("ALLOWED_IDS", "Allowed Telegram IDs", "", "text", "Comma se alag. Khaali = sab use kar sakte hain. Apni ID bot ko /id bhej kar milegi"),
    ("BOT_NAME", "Bot ka naam", "JARVIS", "text", ""),
    ("CHAT_PROMPT", "Bot ka behaviour (system prompt)", DEFAULT_PROMPT, "area", "{BOT_NAME} likhne par bot ka naam aa jata hai"),
    ("DEFAULT_LANG", "Default bhasha (hindi ya english)", "hindi", "text", "Kahani, mausam card aur awaaz ke liye"),
    ("DEFAULT_CITY", "Default shehar (/weather)", "Delhi", "text", ""),
    ("PEXELS_KEY", "Pexels API Key (optional)", "", "secret", "pexels.com/api se free. Photo search aur achhi hoti hai"),
    ("PIXABAY_KEY", "Pixabay API Key (optional)", "", "secret", "pixabay.com/api/docs se free"),
    ("UPSTASH_URL", "Upstash Redis REST URL (yaaddasht ke liye)", "", "secret",
     "upstash.com par free Redis banayein. Isse bot ki yaaddasht, notes aur settings hamesha ke liye bachti hain"),
    ("UPSTASH_TOKEN", "Upstash Redis REST Token", "", "secret", "Wahin REST API section me milta hai"),
    ("VOICE_RATE", "Awaaz ki raftaar", "-6%", "text", "-20% se +20%. Kahani ke liye -6% achha hai"),
    ("VOICE_HI_MALE", "Hindi purush awaaz", "", "text", "Khaali = hi-IN-MadhurNeural"),
    ("VOICE_HI_FEMALE", "Hindi mahila awaaz", "", "text", "Khaali = hi-IN-SwaraNeural"),
    ("VOICE_EN_MALE", "English purush awaaz", "", "text", "Khaali = en-IN-PrabhatNeural"),
    ("VOICE_EN_FEMALE", "English mahila awaaz", "", "text", "Khaali = en-IN-NeerjaNeural"),
    ("MUSIC_VOLUME", "Background music volume (%)", "14", "text", "0 se 100"),
    ("SEND_PARTS", "Kahani ka har part alag bhejein (1 ya 0)", "1", "text", ""),
    ("VIDEO_W", "Video width", "640", "text", "Max 1280. Free Render par chhota rakhna behtar"),
    ("VIDEO_H", "Video height", "360", "text", "Max 720"),
    ("VIDEO_FPS", "Video FPS (ek second me frames)", "15", "text", "Max 30. Jyada = smooth par der lagti hai"),
    ("VIDEO_SECONDS", "/video ki lambai (second)", "6", "text", "Max 15"),
]
DEF = {f[0]: f[2] for f in FIELDS}
_file: dict = {}


def load_file():
    global _file
    try:
        _file = json.load(open(SETTINGS_FILE))
    except Exception:  # noqa
        _file = {}


def save_file():
    try:
        json.dump(_file, open(SETTINGS_FILE, "w"))
    except Exception as e:  # noqa
        log.warning("settings file save nahi hui: %s", e)


def cfg(key):
    if key in _file:
        return _file[key]
    return os.getenv(key, DEF.get(key, ""))


def num(key, cap, lo=1):
    try:
        v = int(str(cfg(key)).strip())
    except Exception:  # noqa
        v = int(DEF[key])
    return max(lo, min(v, cap))


def vw(): return num("VIDEO_W", 1280) // 2 * 2
def vh(): return num("VIDEO_H", 720) // 2 * 2
def fps(): return num("VIDEO_FPS", 30)
def lang_default(): return "english" if str(cfg("DEFAULT_LANG")).strip().lower().startswith("e") else "hindi"


def allowed_ids():
    return {int(x) for x in re.findall(r"\d+", str(cfg("ALLOWED_IDS")))}


# =====================================================================
#  GROQ (chat + voice-to-text + sabse powerful model ka chunav)
# =====================================================================
_groq = (None, None)
heavy = asyncio.Semaphore(1)  # free server par ek time me ek bhaari kaam
history: dict[int, list] = {}
tasks: set = set()
EXCL = ("whisper", "orpheus", "guard", "safeguard", "tts", "playai", "embed")
PREF = ("gpt-oss-120b", "70b", "qwen", "gpt-oss-20b", "llama", "gemma")
_models = {"t": 0.0, "ids": []}


def get_groq():
    global _groq
    key = str(cfg("GROQ_API_KEY")).strip()
    if not key:
        raise RuntimeError("Groq API key abhi set nahi hai. Website ke /admin page par daalein.")
    if _groq[0] != key:
        _groq = (key, AsyncGroq(api_key=key))
    return _groq[1]


async def list_models(force=False):
    """Groq account me jo chat models available hain."""
    if not force and _models["ids"] and time.time() - _models["t"] < 600:
        return _models["ids"]
    try:
        r = await get_groq().models.list()
        ids = [m.id for m in r.data]
        _models["ids"] = [i for i in ids if not any(x in i.lower() for x in EXCL)]
        _models["t"] = time.time()
    except Exception as e:  # noqa
        log.warning("models list fail: %s", e)
    return _models["ids"]


def best_model(ids):
    for k in PREF:
        for i in ids:
            if k in i.lower():
                return i
    return ids[0] if ids else None


async def _chat(mdl, msgs, temperature, max_tokens, extra):
    r = await get_groq().chat.completions.create(
        model=mdl, messages=msgs, temperature=temperature, max_tokens=max_tokens, extra_body=extra)
    out = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
    if not out:
        raise RuntimeError("khaali jawab aaya")
    return out


async def ask(system, user, hist=None, temperature=0.7, max_tokens=6000, model=None):
    main = model or str(cfg("GROQ_MODEL") or "").strip() or DEF["GROQ_MODEL"]
    if main.lower() == "auto":
        main = best_model(await list_models()) or DEF["GROQ_MODEL"]
    fb = str(cfg("FALLBACK_MODEL") or "").strip()
    chain = [main] + ([fb] if fb and fb != main else [])
    msgs = [{"role": "system", "content": system}] + (hist or []) + [{"role": "user", "content": user}]
    last = None
    for ci, mdl in enumerate(chain):
        extra = {"reasoning_effort": "low"} if "gpt-oss" in mdl else None
        for attempt in range(3):
            try:
                return await _chat(mdl, msgs, temperature, max_tokens, extra)
            except Exception as e:  # noqa
                last, s = e, str(e).lower()
                if extra and "reasoning" in s:
                    extra = None
                    continue
                if ("429" in s or "rate" in s) and attempt < 2 and ci == len(chain) - 1:
                    await asyncio.sleep(20)
                    continue
                break
    try:  # aakhri koshish: jo model abhi account me available hai
        b = best_model(await list_models(True))
        if b and b not in chain:
            return await _chat(b, msgs, temperature, max_tokens, None)
    except Exception as e:  # noqa
        last = last or e
    raise last or RuntimeError("Groq jawab nahi de paya")


async def transcribe(data: bytes, name="voice.ogg"):
    r = await get_groq().audio.transcriptions.create(
        file=(name, data), model=cfg("WHISPER_MODEL") or DEF["WHISPER_MODEL"], response_format="text")
    return (r if isinstance(r, str) else getattr(r, "text", "")).strip()


def parse_json(raw):
    raw = re.sub(r"```(?:json)?", "", raw)
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("JSON nahi mila")
    return json.loads(raw[a:b + 1])


# =====================================================================
#  MEMORY: bot aapki baatein yaad rakhta hai
#  Upstash Redis (free) ho to hamesha ke liye, warna temporary file (restart par mit sakti hai)
# =====================================================================
MEMORY_FILE = os.getenv("MEMORY_FILE", "/tmp/memory.json")
MAXF = 40  # kitni baatein yaad rakhni hain
SENS = re.compile(r"(password|passcode|\botp\b|\bpin\b|\bcvv\b|aadhaar|aadhar|pan card|card number|account number|"
                  r"पासवर्ड|ओटीपी|आधार)|\d{9,}", re.I)


class Store:
    """Chhota key-value store. get() fail ho to error deta hai, taaki purani yaaddasht galti se mit na jaye."""

    def __init__(self):
        self.cache = {}

    def creds(self):
        url = str(cfg("UPSTASH_URL")).strip().rstrip("/")
        tok = str(cfg("UPSTASH_TOKEN")).strip()
        return (url, tok) if url and tok else (None, None)

    @property
    def persistent(self):
        return self.creds()[0] is not None

    def describe(self):
        return "Upstash Redis (hamesha ke liye)" if self.persistent else "temporary file (restart par mit sakti hai)"

    def clear_cache(self):
        self.cache.clear()

    async def _cmd(self, *args):
        url, tok = self.creds()
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {tok}"}, json=list(args))
        if r.status_code != 200:
            raise RuntimeError(f"memory DB error {r.status_code}")
        return r.json().get("result")

    def _fload(self):
        try:
            return json.load(open(MEMORY_FILE))
        except Exception:  # noqa
            return {}

    async def get(self, key, default=None):
        if key in self.cache:
            return self.cache[key]
        if self.persistent:
            raw = await self._cmd("GET", key)
            val = json.loads(raw) if raw else None
        else:
            val = self._fload().get(key)
        if val is None:
            val = copy.deepcopy(default)
        self.cache[key] = val
        return val

    async def set(self, key, value):
        self.cache[key] = value
        if self.persistent:
            await self._cmd("SET", key, json.dumps(value, ensure_ascii=False))
        else:
            d = self._fload()
            d[key] = value
            try:
                json.dump(d, open(MEMORY_FILE, "w"), ensure_ascii=False)
            except Exception as e:  # noqa
                log.warning("memory file save fail: %s", e)


store = Store()


async def load_settings_from_db():
    """Restart ke baad /admin ki settings DB se wapas laata hai (file me jo hai wo pehle)."""
    if not store.persistent:
        return
    try:
        for k, v in ((await store.get("settings", {})) or {}).items():
            _file.setdefault(k, v)
        save_file()
    except Exception as e:  # noqa
        log.warning("settings DB se nahi aayi: %s", e)


async def save_settings_to_db():
    if store.persistent:
        try:
            await store.set("settings", dict(_file))
        except Exception as e:  # noqa
            log.warning("settings DB me save nahi hui: %s", e)


FACT_SYS = (
    "Tum ek yaaddasht-manager ho. User aur assistant ki baatcheet se user ke baare me sirf STHAYI, kaam ki baatein nikaalo "
    "(naam, shehar, kaam/padhai, pasand-napasand, parivaar ke naam, lakshya, bhasha, chal rahe projects, aadatein). "
    "Pichli list me se kuchh mat hatao jab tak nayi baat use galat na thahraye; duplicate mat rakho; har baat chhoti "
    "(10 shabd tak) ho. Password, OTP, card/bank/Aadhaar number jaisi cheezein kabhi mat likho. "
    'Sirf JSON do: {"facts": ["...", "..."]} (maximum 30).')
SUM_SYS = ("Neeche purana saaransh aur nayi baatcheet hai. Dono ko milakar 6-8 vaakya ka naya saaransh banao: "
           "kya poochha gaya, kya tay hua, kaun se kaam adhoore hain. Sirf saaransh do, user ki bhasha me.")
learning: set = set()
counters: dict = {}


async def get_mem(uid, name=""):
    mem = await store.get(f"u:{uid}", None)
    if not isinstance(mem, dict):
        mem = {}
    mem.setdefault("facts", [])
    mem.setdefault("summary", "")
    mem.setdefault("on", True)
    if name:
        mem["name"] = name
    store.cache[f"u:{uid}"] = mem
    return mem


def memory_block(mem):
    parts = []
    if mem.get("name"):
        parts.append(f"User ka Telegram naam: {mem['name']}")
    if mem["facts"]:
        parts.append("User ke baare me yaad rakhi baatein:\n" + "\n".join("- " + f for f in mem["facts"]))
    if mem["summary"]:
        parts.append("Pichli baatcheet ka saaransh: " + mem["summary"])
    if not parts:
        return ""
    return ("\n\n" + "\n\n".join(parts)
            + "\n\n(Inhe zaroorat padne par hi sahaj tareeke se use karo; baar-baar mat dohrao.)")


async def learn(uid, cid):
    """Background me: nayi baatein nikaalta hai aur lambi baatcheet ka saaransh banata hai."""
    if uid in learning:
        return
    learning.add(uid)
    try:
        mem = await get_mem(uid)
        h = await store.get(f"h:{cid}", [])
        fb = str(cfg("FALLBACK_MODEL")).strip() or None
        changed = False
        if mem["on"] and h:
            convo = "\n".join(f"{m['role']}: {str(m['content'])[:400]}" for m in h[-8:])
            raw = await ask(FACT_SYS, "Pichli list: " + json.dumps(mem["facts"], ensure_ascii=False)
                            + "\n\nBaatcheet:\n" + convo, temperature=0.1, max_tokens=900, model=fb)
            facts = []
            for f in parse_json(raw).get("facts", []):
                f = str(f).strip()[:140]
                if f and not SENS.search(f) and f.lower() not in [x.lower() for x in facts]:
                    facts.append(f)
            if facts and not (len(mem["facts"]) > 6 and len(facts) < len(mem["facts"]) // 2):
                mem["facts"] = facts[:MAXF]
                changed = True
        if len(h) > 24:
            old, h[:] = h[:12], h[12:]
            convo = "\n".join(f"{m['role']}: {str(m['content'])[:300]}" for m in old)
            s = await ask(SUM_SYS, f"Purana saaransh: {mem['summary'] or 'koi nahi'}\n\nNayi baatcheet:\n{convo}",
                          temperature=0.2, max_tokens=500, model=fb)
            mem["summary"] = s.strip()[:900]
            await store.set(f"h:{cid}", h)
            changed = True
        if changed:
            await store.set(f"u:{uid}", mem)
    except Exception as e:  # noqa
        log.info("learn fail: %s", e)
    finally:
        learning.discard(uid)


# ---------------- Reminders (restart ke baad bhi bane rehte hain) ----------------
scheduled: set = set()


async def remind_later(rid, cid, due, msg):
    bot = None
    try:
        await asyncio.sleep(max(1.0, due - time.time()))
        for _ in range(10):
            bot = manager.app.bot if manager.app else None
            if bot:
                break
            await asyncio.sleep(30)
        if bot:
            await bot.send_message(cid, f"⏰ Yaad dilana tha: {msg}")
    finally:
        scheduled.discard(rid)
        try:
            items = await store.get("reminders", [])
            items[:] = [r for r in items if r.get("id") != rid]
            await store.set("reminders", items)
        except Exception:  # noqa
            pass


def schedule_reminder(r):
    if r["id"] in scheduled:
        return
    scheduled.add(r["id"])
    t = asyncio.create_task(remind_later(r["id"], r["cid"], r["due"], r["msg"]))
    tasks.add(t)
    t.add_done_callback(tasks.discard)


async def reschedule_reminders():
    try:
        for r in list(await store.get("reminders", [])):
            schedule_reminder(r)
    except Exception as e:  # noqa
        log.warning("reminders wapas nahi aaye: %s", e)


# =====================================================================
#  TEXT / FONTS / IMAGE HELPERS (Hindi shaping ke liye Pillow + raqm)
# =====================================================================
_fp: dict = {}


def has_deva(s):
    return bool(re.search(r"[\u0900-\u097F]", s or ""))


def font_path(deva, bold):
    key = (deva, bold)
    if key in _fp:
        return _fp[key]
    if deva:
        names = ["NotoSansDevanagari-Bold.ttf" if bold else "NotoSansDevanagari-Regular.ttf",
                 "FreeSansBold.ttf" if bold else "FreeSans.ttf"]
    else:
        names = ["NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"]
    names.append("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    found = None
    for n in names:
        m = glob.glob(f"/usr/share/fonts/**/{n}", recursive=True)
        if m:
            found = m[0]
            break
    _fp[key] = found
    return found


def get_font(text, size, bold=False):
    p = font_path(has_deva(text), bold)
    if p:
        for kw in ({"layout_engine": ImageFont.Layout.RAQM}, {}):
            try:
                return ImageFont.truetype(p, size, **kw)
            except Exception:  # noqa
                continue
    return ImageFont.load_default()


def wrap_px(text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if font.getlength(t) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def gradient(W, H, c1, c2):
    t = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    a = np.array(c1, dtype=np.float32)[None, None, :]
    b = np.array(c2, dtype=np.float32)[None, None, :]
    arr = (a * (1 - t) + b * t) * np.ones((1, W, 1), dtype=np.float32)
    return Image.fromarray(arr.astype(np.uint8))


def cover(im, W, H):
    s = max(W / im.width, H / im.height)
    im = im.resize((max(W, int(im.width * s + 0.5)), max(H, int(im.height * s + 0.5))), RS.LANCZOS)
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def png_bytes(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def jpg_bytes(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def get_svg(text):
    m = re.search(r"<svg.*?</svg>", text, re.S | re.I)
    if not m:
        raise ValueError("SVG nahi mila")
    return re.sub(r"<(script|image|foreignObject)\b.*?(</\1>|/>)", "", m.group(0), flags=re.S | re.I)


def svg_png(svg, width):
    return cairosvg.svg2png(bytestring=svg.encode(), output_width=width, background_color="white")


SVG_SYS = ("Tum ek expert vector artist ho. Sirf ek complete SVG code do, koi explanation nahi. Rules: "
           'root <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">; '
           "layered flat-illustration style: aasman ka gradient, door ki pahadiyan/imaarat, beech ke objects, "
           "aage ka foreground, soft shadows, achha colour palette; gradients aur paths use karo; "
           "<image>, <script>, text, external link mana hai; svg 7000 characters se chhota rakho.")


async def make_svg(prompt):
    last = None
    for _ in range(2):
        try:
            out = await ask(SVG_SYS, prompt, temperature=0.8, max_tokens=5000)
            svg = get_svg(out)
            png = await asyncio.to_thread(svg_png, svg, 1280)
            return svg, png
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"Image nahi ban payi: {last}")


# =====================================================================
#  AUDIO: ffmpeg, music (code se), voice (TTS, vaakya-wise + pause)
# =====================================================================
def ff(*args):
    p = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr[-300:])


def media_duration(path):
    p = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise RuntimeError("Audio ki lambai nahi mili")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


MOODS = {
    "calm": (57, [(0, 4, 7), (5, 9, 12), (7, 11, 14), (0, 4, 7)], 60),
    "happy": (60, [(0, 4, 7), (7, 11, 14), (9, 12, 16), (5, 9, 12)], 108),
    "sad": (57, [(0, 3, 7), (8, 12, 15), (3, 7, 10), (10, 14, 17)], 56),
    "tense": (50, [(0, 3, 6), (1, 5, 8), (0, 3, 7), (-2, 2, 5)], 84),
    "epic": (50, [(0, 3, 7), (8, 12, 15), (3, 7, 10), (10, 14, 17)], 76),
    "mystery": (52, [(0, 3, 7), (5, 8, 12), (1, 5, 8), (0, 3, 7)], 64),
}
MOOD_ALIAS = {"shant": "calm", "khush": "happy", "udaas": "sad", "dar": "tense", "josh": "epic", "rahasya": "mystery"}
MOOD_COLORS = {"calm": ((30, 60, 90), (90, 150, 170)), "happy": ((255, 150, 60), (255, 210, 120)),
               "sad": ((30, 35, 60), (90, 100, 130)), "tense": ((40, 20, 30), (120, 50, 50)),
               "epic": ((40, 30, 80), (200, 100, 60)), "mystery": ((20, 25, 45), (70, 60, 110))}


def fix_mood(m):
    m = str(m or "").lower().strip()
    m = MOOD_ALIAS.get(m, m)
    return m if m in MOODS else "calm"


def synth_music(mood, seconds, path, sr=22050):
    root, chords, bpm = MOODS.get(mood, MOODS["calm"])
    seconds = max(3.0, float(seconds))
    beat = 60.0 / bpm
    bar = 4 * beat
    n = int(seconds * sr)
    out = np.zeros(n + int(6 * sr), dtype=np.float32)

    def mtof(m):
        return 440.0 * 2.0 ** ((m - 69) / 12.0)

    t0, i = 0.0, 0
    while t0 < seconds:
        chord = chords[i % len(chords)]
        s, L = int(t0 * sr), int(bar * sr)
        t = np.arange(L, dtype=np.float32) / sr
        env = np.minimum(1.0, t / 0.5) * np.minimum(1.0, (bar - t) / 0.7)
        pad = np.zeros(L, dtype=np.float32)
        for note in chord:
            f = mtof(root + note - 12)
            pad += (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t)
                    + 0.25 * np.sin(2 * np.pi * f * 1.004 * t))
        out[s:s + L] += 0.05 * pad * env
        step, k = beat / 2, 0
        while k * step < bar:
            note = chord[k % len(chord)] + 12 * (1 + (k // len(chord)) % 2)
            f = mtof(root + note)
            m = int(step * 3 * sr)
            tt = np.arange(m, dtype=np.float32) / sr
            a = s + int(k * step * sr)
            out[a:a + m] += np.sin(2 * np.pi * f * tt) * np.exp(-tt * 4.5) * 0.16
            k += 1
        t0 += bar
        i += 1
    out = out[:n]
    fade = min(int(0.8 * sr), n // 2)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)
    out[:fade] *= ramp
    out[-fade:] *= ramp[::-1]
    out = out / (np.max(np.abs(out)) + 1e-6) * 0.8
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((out * 32767).astype(np.int16).tobytes())


def mix_audio(voice, music, out):
    vol = num("MUSIC_VOLUME", 100, 0) / 100.0
    ff("-i", voice, "-i", music, "-filter_complex",
       f"[1:a]volume={vol}[m];[0:a][m]amix=inputs=2:duration=longest:normalize=0[a]",
       "-map", "[a]", "-c:a", "aac", "-b:a", "96k", out)


def music_only(music, out, gain=0.5):
    ff("-i", music, "-filter_complex", f"volume={gain}[a]", "-map", "[a]", "-c:a", "aac", "-b:a", "96k", out)


def mux_audio(video, audio, out):
    ff("-i", video, "-i", audio, "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-shortest",
       "-movflags", "+faststart", out)


def add_music_to_video(video, mood, out):
    dur = media_duration(video)
    wav = video + ".wav"
    synth_music(mood, dur + 1, wav)
    aud = video + ".m4a"
    music_only(wav, aud, min(1.0, num("MUSIC_VOLUME", 100, 0) / 100.0 * 2.2))
    mux_audio(video, aud, out)


VOICES = {("hindi", "male"): "hi-IN-MadhurNeural", ("hindi", "female"): "hi-IN-SwaraNeural",
          ("english", "male"): "en-IN-PrabhatNeural", ("english", "female"): "en-IN-NeerjaNeural"}


def voice_name(lang, gender):
    k = ("VOICE_HI_" if lang == "hindi" else "VOICE_EN_") + ("MALE" if gender == "male" else "FEMALE")
    return str(cfg(k)).strip() or VOICES[(lang, gender)]


def clean_tts(text):
    text = re.sub(r"[*_#`~>\[\]{}|\\]", " ", text)
    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tts_chunks(text, limit=240):
    out, cur = [], ""
    for s in split_sentences(text):
        if cur and len(cur) + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def join_audio(parts, out, gap=0.3):
    """Vaakya-wise awaaz ko beech me chhota pause dekar jodta hai (zyada natural)."""
    if len(parts) == 1:
        ff("-i", parts[0], "-c:a", "libmp3lame", "-b:a", "96k", out)
        return
    args, fl, seq = [], [], []
    for i, p in enumerate(parts):
        args += ["-i", p]
        fl.append(f"[{i}:a]aresample=24000,aformat=channel_layouts=mono[a{i}]")
        seq.append(f"[a{i}]")
        if i < len(parts) - 1:
            fl.append(f"anullsrc=r=24000:cl=mono:d={gap}[s{i}]")
            seq.append(f"[s{i}]")
    fl.append("".join(seq) + f"concat=n={len(seq)}:v=0:a=1[out]")
    ff(*args, "-filter_complex", ";".join(fl), "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "96k", out)


async def tts(text, lang, gender, path):
    text = clean_tts(text)[:4000]
    if not text:
        raise RuntimeError("Bolne ke liye text khaali hai")
    rate = str(cfg("VOICE_RATE")).strip() or "-6%"
    if not re.fullmatch(r"[+-]\d{1,2}%", rate):
        rate = "-6%"
    voice = voice_name(lang, gender)
    try:
        import edge_tts
        parts = []
        for i, ch in enumerate(tts_chunks(text)):
            p = f"{path}.{i}.mp3"
            await asyncio.wait_for(edge_tts.Communicate(ch, voice, rate=rate).save(p), 60)
            if os.path.getsize(p) < 300:
                raise RuntimeError("khaali awaaz")
            parts.append(p)
        await asyncio.to_thread(join_audio, parts, path)
    except Exception as e:  # noqa
        log.warning("edge-tts fail (%s), gTTS try", e)
        from gtts import gTTS
        await asyncio.to_thread(lambda: gTTS(text, lang="hi" if lang == "hindi" else "en", tld="co.in").save(path))


# =====================================================================
#  PHOTO SEARCH (Pexels / Pixabay / Openverse) + download
# =====================================================================
UA = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}


async def search_photos(query, n=8):
    res = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as c:
        pk, xk = str(cfg("PEXELS_KEY")).strip(), str(cfg("PIXABAY_KEY")).strip()
        if pk:
            try:
                r = await c.get("https://api.pexels.com/v1/search", headers={"Authorization": pk},
                                params={"query": query, "per_page": n, "orientation": "landscape"})
                for p in r.json().get("photos", []):
                    res.append({"url": p["src"]["large"], "credit": f"Photo: {p['photographer']} / Pexels"})
            except Exception as e:  # noqa
                log.warning("pexels: %s", e)
        if xk:
            try:
                r = await c.get("https://pixabay.com/api/", params={
                    "key": xk, "q": query, "image_type": "photo", "orientation": "horizontal",
                    "per_page": max(n, 3), "safesearch": "true"})
                for p in r.json().get("hits", []):
                    res.append({"url": p["largeImageURL"], "credit": f"Photo: {p.get('user', '?')} / Pixabay"})
            except Exception as e:  # noqa
                log.warning("pixabay: %s", e)
        if len(res) < 3:
            try:
                r = await c.get("https://api.openverse.org/v1/images/", params={
                    "q": query, "page_size": n, "mature": "false", "aspect_ratio": "wide"})
                for it in r.json().get("results", []):
                    cr = f"{it.get('creator') or 'Unknown'} / {str(it.get('license', '')).upper()} via Openverse"
                    if it.get("thumbnail"):
                        res.append({"url": it["thumbnail"], "credit": cr})
                    elif it.get("url"):
                        res.append({"url": it["url"], "credit": cr})
            except Exception as e:  # noqa
                log.warning("openverse: %s", e)
    return res


async def fetch_image(url):
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=UA) as c:
        r = await c.get(url)
    if r.status_code != 200 or len(r.content) > 15_000_000:
        raise RuntimeError(f"download fail {r.status_code}")
    im = Image.open(io.BytesIO(r.content))
    im.draft("RGB", (2000, 2000))
    im = im.convert("RGB")
    if im.width < 320 or im.height < 200:
        raise RuntimeError("image chhoti hai")
    return im


async def get_photos(query, n=1):
    cands = await search_photos(query)
    top = cands[:6]
    random.shuffle(top)
    cands = top + cands[6:]
    out = []
    for c in cands[:10]:
        try:
            out.append((await fetch_image(c["url"]), c["credit"]))
        except Exception as e:  # noqa
            log.info("photo skip: %s", e)
        if len(out) >= n:
            break
    return out


async def to_english_query(q):
    if q.isascii():
        return q
    try:
        return (await ask("Sirf 3-5 English shabdon me image-search keywords do, aur kuchh nahi.", q,
                          temperature=0.2, max_tokens=60)).strip().strip('."')
    except Exception:  # noqa
        return q


# =====================================================================
#  SANDBOX: LLM ka animation code (/video) - har frame ka alag drawing
# =====================================================================
VIDEO_SYS = (
    "Tum Python animator ho. Sirf ek ```python code block do. Code me ek function define karo: "
    "def frame(t): jahan t 0.0 se 1.0 tak hai (poori video), aur vo ek complete SVG string return kare "
    'jiska root <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720"> ho. '
    "ZAROORI: har frame pichle frame se alag dikhna chahiye. Kam se kam 5 chizein t ke hisab se lagataar chalein: "
    "position, rotation, size, colour, opacity. Sirf ek tasveer ya sthir drishya MAT banao. "
    "Madad ke liye ye functions pehle se maujood hain (import ki zaroorat nahi): "
    "lerp(a,b,t), ease(t) (smooth 0-1), wave(t,freq,phase) (-1 se 1 ka sine), hsl(h,s,l) (rang ka '#rrggbb', h 0-1). "
    "Sirf `import math` aur `import random` allowed hai (random ka seed har frame me fix rakho: random.seed(5)). "
    "File, network, os, open, eval sab mana hai. Layers use karo: gradient aasman, door ki cheezein, beech ki, aage ki. "
    "Frame function tez ho (250 se kam shapes), text/akshar SVG me mat likho.")
RUNNER = '''
import sys, json, math, random, colorsys
def lerp(a, b, t): return a + (b - a) * t
def ease(t):
    t = max(0.0, min(1.0, t)); return t * t * (3 - 2 * t)
def wave(t, freq=1.0, phase=0.0): return math.sin(2 * math.pi * (t * freq + phase))
def hsl(h, s=0.7, l=0.5):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, l, s)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
code = open(sys.argv[1]).read()
n = int(sys.argv[2])
g = {"math": math, "random": random, "lerp": lerp, "ease": ease, "wave": wave, "hsl": hsl}
exec(code, g)
out = [str(g["frame"](i / (n - 1))) for i in range(n)]
json.dump(out, open(sys.argv[3], "w"))
'''
BANNED = re.compile(r"__|\b(open|exec|eval|compile|globals|locals|getattr|setattr|delattr|input|breakpoint|vars|dir)\s*\(")


def check_code(code):
    for line in code.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")) and s not in ("import math", "import random"):
            raise ValueError(f"Import allowed nahi: {s}")
    if BANNED.search(code):
        raise ValueError("Code me mana kiya hua function mila")


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))


def run_frames(code, n):
    check_code(code)
    with tempfile.TemporaryDirectory() as d:
        cp, op, rp = (os.path.join(d, x) for x in ("a.py", "out.json", "run.py"))
        open(cp, "w").write(code)
        open(rp, "w").write(RUNNER)
        p = subprocess.run([sys.executable, rp, cp, str(max(n, 2)), op], capture_output=True,
                           text=True, timeout=45, preexec_fn=limits, cwd=d)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-300:])
        return json.load(open(op))


def motion_ok(frames):
    """Animation sach me chal rahi hai ya sirf ek hi tasveer hai?"""
    step = max(1, len(frames) // 12)
    return len(set(frames[::step])) >= 6


async def get_code_frames(prompt, n, tries=3):
    """LLM se animation code lekar frames banata hai. Sthir (static) ho to dobara kehta hai."""
    last, hint = None, ""
    for _ in range(tries):
        try:
            out = await ask(VIDEO_SYS, prompt + hint, temperature=0.8, max_tokens=4500)
            m = re.search(r"```(?:python)?\n(.*?)```", out, re.S)
            frames = await asyncio.to_thread(run_frames, m.group(1) if m else out, n)
            await asyncio.to_thread(svg_png, frames[0], 100)  # test render
            if not motion_ok(frames):
                hint = "\n\nPichli baar animation sthir thi. Ab t ka use karke har cheez ko lagataar hilao."
                last = RuntimeError("animation hil nahi rahi thi")
                continue
            return frames
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"Animation code nahi chala: {last}")


def open_writer(path):
    return imageio.get_writer(path, fps=fps(), codec="libx264", pixelformat="yuv420p",
                              macro_block_size=None, quality=None,
                              ffmpeg_params=["-preset", "veryfast", "-crf", "27"])


def message_layer(text, W, H):
    size = max(18, int(H * 0.085))
    font = get_font(text, size, True)
    lines = wrap_px(text, font, int(W * 0.88))[:3]
    lh = int(size * 1.35)
    h = lh * len(lines) + 20
    layer = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y = 10
    for ln in lines:
        d.text((W / 2, y), ln, font=font, fill=(255, 255, 255, 255), anchor="ma",
               stroke_width=max(2, size // 12), stroke_fill=(0, 0, 0, 255))
        y += lh
    return layer, int(H * 0.5 - h / 2)


def build_anim(frames, path, message=None):
    W, H, F = vw(), vh(), fps()
    lay = ly = None
    if message:
        lay, ly = message_layer(message, W, H)
    with open_writer(path) as w:
        for i, s in enumerate(frames):
            im = Image.open(io.BytesIO(svg_png(s, W))).convert("RGB").resize((W, H))
            if lay is not None:
                a = min(1.0, i / max(1.0, F * 0.8))
                l2 = lay if a >= 1 else lay.copy()
                if a < 1:
                    l2.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
                im.paste(l2, (0, ly), l2)
            w.append_data(np.asarray(im))


# =====================================================================
#  SCENE VIDEO: har scene me kai shots (2-3 alag visuals), zoom/pan,
#  crossfade aur vaakya-wise Hindi subtitles
# =====================================================================
def caption_layer(text, W, H):
    size = max(16, int(H * 0.055))
    font = get_font(text, size, True)
    lines = wrap_px(text, font, int(W * 0.9))[:4]
    lh = int(size * 1.5)
    h = lh * len(lines) + int(size * 0.9)
    layer = Image.new("RGBA", (W, h), (0, 0, 0, 150))
    d = ImageDraw.Draw(layer)
    y = int(size * 0.45)
    for ln in lines:
        d.text(((W - font.getlength(ln)) / 2, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh
    return layer, H - h


def split_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", text.strip()) if p.strip()]
    return parts or [text]


def kb_frame(base, p, zin, direction, W, H):
    """Ken-Burns: dheere zoom + pan. base 1.3x bada hota hai."""
    BW, BH = base.size
    z = 1.0 + 0.3 * (p if zin else 1 - p)
    vw_, vh_ = BW / z, BH / z
    x0 = (BW - vw_) / 2 + (p - 0.5) * (BW - vw_) * 0.8 * direction
    y0 = (BH - vh_) / 2 + (p - 0.5) * (BH - vh_) * 0.3
    x0 = min(max(0, x0), BW - vw_)
    y0 = min(max(0, y0), BH - vh_)
    return base.resize((W, H), RS.BILINEAR, box=(x0, y0, x0 + vw_, y0 + vh_))


def build_scene_video(shots, sents, dur, path, idx):
    """shots: PIL images (1.3x). Vaakya shots me baante jate hain, shots ke beech crossfade."""
    W, H, F = vw(), vh(), fps()
    n = max(4, int(dur * F))
    ns = max(1, min(len(shots), len(sents)))
    shots = shots[:ns]
    layers = [caption_layer(s, W, H) for s in sents]
    ls = np.array([max(1, len(s)) for s in sents], dtype=float)
    sb = np.cumsum(ls) / ls.sum() * n  # har vaakya kis frame par khatam
    groups = np.array_split(np.arange(len(sents)), ns)
    ends = [float(sb[g[-1]]) for g in groups]
    starts = [0.0] + ends[:-1]
    cf = max(2, int(0.45 * F))
    fade = max(1, int(0.3 * F))
    black = Image.new("RGB", (W, H))
    direc = lambda j: 1 if (idx + j) % 3 else -1  # noqa: E731
    k = j = 0
    with open_writer(path) as w:
        for f in range(n):
            while j < ns - 1 and f >= ends[j]:
                j += 1
            p = (f - starts[j]) / max(1.0, ends[j] - starts[j] - 1)
            fr = kb_frame(shots[j], min(max(p, 0.0), 1.0), (idx + j) % 2 == 0, direc(j), W, H)
            if j < ns - 1 and f >= ends[j] - cf:
                nxt = kb_frame(shots[j + 1], 0.0, (idx + j + 1) % 2 == 0, direc(j + 1), W, H)
                fr = Image.blend(fr, nxt, min(1.0, (f - (ends[j] - cf) + 1) / cf))
            while k < len(sents) - 1 and f >= sb[k]:
                k += 1
            lay, ly = layers[k]
            fr.paste(lay, (0, ly), lay)
            if f < fade:
                fr = Image.blend(black, fr, f / fade)
            elif f >= n - fade:
                fr = Image.blend(black, fr, (n - 1 - f) / fade)
            w.append_data(np.asarray(fr))


def build_code_scene(frames_svg, sents, path):
    """Poora scene code-animation: har frame LLM ke code se bana."""
    W, H, F = vw(), vh(), fps()
    n = len(frames_svg)
    layers = [caption_layer(s, W, H) for s in sents]
    ls = np.array([max(1, len(s)) for s in sents], dtype=float)
    sb = np.cumsum(ls) / ls.sum() * n
    fade = max(1, int(0.3 * F))
    black = Image.new("RGB", (W, H))
    k = 0
    with open_writer(path) as w:
        for f, s in enumerate(frames_svg):
            fr = Image.open(io.BytesIO(svg_png(s, W))).convert("RGB").resize((W, H))
            while k < len(sents) - 1 and f >= sb[k]:
                k += 1
            lay, ly = layers[k]
            fr.paste(lay, (0, ly), lay)
            if f < fade:
                fr = Image.blend(black, fr, f / fade)
            elif f >= n - fade:
                fr = Image.blend(black, fr, (n - 1 - f) / fade)
            w.append_data(np.asarray(fr))


# =====================================================================
#  WEATHER CARD (Open-Meteo, bina key ke) + CHART + POSTER + QR
# =====================================================================
def wmo(code):
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloud"


LABEL = {"clear": ("Clear sky", "साफ़ आसमान"), "partly": ("Partly cloudy", "हल्के बादल"),
         "cloud": ("Cloudy", "बादल छाए"), "fog": ("Fog", "कोहरा"), "rain": ("Rain", "बारिश"),
         "snow": ("Snow", "बर्फ़बारी"), "storm": ("Thunderstorm", "आंधी-तूफ़ान")}
DAYS_HI = ["सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"]
SKY = {"clear": ((40, 120, 225), (130, 200, 250)), "partly": ((70, 130, 210), (150, 195, 235)),
       "cloud": ((90, 105, 125), (160, 172, 188)), "fog": ((120, 130, 140), (175, 180, 185)),
       "rain": ((45, 60, 88), (100, 120, 148)), "snow": ((115, 145, 190), (205, 222, 242)),
       "storm": ((28, 28, 48), (78, 66, 100)), "night": ((8, 14, 44), (45, 55, 115))}


def weather_icon(kind, size, night=False):
    S = size * 3
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def glow(cx, cy, r, col):
        g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(g).ellipse((cx - r * 1.7, cy - r * 1.7, cx + r * 1.7, cy + r * 1.7), fill=col + (110,))
        im.alpha_composite(g.filter(ImageFilter.GaussianBlur(r * 0.4)))

    def sun(cx, cy, r):
        glow(cx, cy, r, (255, 205, 60))
        for a in range(0, 360, 45):
            c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
            d.line((cx + c * r * 1.3, cy + s * r * 1.3, cx + c * r * 1.75, cy + s * r * 1.75),
                   fill=(255, 214, 80, 255), width=max(2, int(r * 0.14)))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 214, 70, 255))

    def moon(cx, cy, r):
        glow(cx, cy, r, (190, 205, 255))
        m = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        md = ImageDraw.Draw(m)
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(240, 240, 255, 255))
        md.ellipse((cx - r * 0.3, cy - r * 1.1, cx + r * 1.5, cy + r * 0.9), fill=(0, 0, 0, 0))
        im.alpha_composite(m)

    def cloud(cx, cy, w, col=(255, 255, 255, 255)):
        h = w * 0.55
        d.ellipse((cx - w * .5, cy - h * .1, cx - w * .1, cy + h * .5), fill=col)
        d.ellipse((cx - w * .28, cy - h * .5, cx + w * .2, cy + h * .35), fill=col)
        d.ellipse((cx + w * 0, cy - h * .2, cx + w * .5, cy + h * .5), fill=col)
        d.rounded_rectangle((cx - w * .42, cy + h * .05, cx + w * .42, cy + h * .5), radius=h * .25, fill=col)

    celestial = moon if night else sun
    if kind == "clear":
        celestial(S * .5, S * .5, S * .2)
    elif kind == "partly":
        celestial(S * .38, S * .38, S * .16)
        cloud(S * .56, S * .6, S * .62)
    elif kind == "cloud":
        cloud(S * .5, S * .5, S * .75, (236, 241, 248, 255))
    elif kind == "fog":
        cloud(S * .5, S * .38, S * .62, (230, 234, 240, 255))
        for i, y in enumerate((.66, .77, .88)):
            d.line((S * (.22 + .05 * i), S * y, S * (.78 - .05 * i), S * y), fill=(235, 238, 244, 255), width=int(S * .035))
    elif kind == "rain":
        cloud(S * .5, S * .36, S * .68, (205, 216, 232, 255))
        for i in range(4):
            x = S * (.3 + .13 * i)
            d.line((x, S * .64, x - S * .05, S * .84), fill=(120, 185, 255, 255), width=int(S * .035))
    elif kind == "snow":
        cloud(S * .5, S * .36, S * .68, (225, 233, 245, 255))
        for i in range(4):
            for j in range(2):
                x, y = S * (.3 + .13 * i + .05 * j), S * (.66 + .13 * j)
                d.ellipse((x - S * .02, y - S * .02, x + S * .02, y + S * .02), fill=(255, 255, 255, 255))
    else:  # storm
        cloud(S * .5, S * .34, S * .7, (130, 140, 165, 255))
        d.polygon([(S * .54, S * .55), (S * .4, S * .78), (S * .5, S * .78), (S * .44, S * .95),
                   (S * .66, S * .7), (S * .55, S * .7)], fill=(255, 220, 70, 255))
    return im.resize((size, size), RS.LANCZOS)


async def fetch_weather(city):
    async with httpx.AsyncClient(timeout=20) as c:
        g = (await c.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "en"})).json()
        if not g.get("results"):
            raise RuntimeError(f"'{city}' shehar nahi mila")
        r0 = g["results"][0]
        w = (await c.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": r0["latitude"], "longitude": r0["longitude"], "timezone": "auto", "forecast_days": 5,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})).json()
    return {"city": r0["name"], "country": r0.get("country", ""), "cur": w["current"], "daily": w["daily"]}


def weather_card(info, lang):
    hi = lang == "hindi"
    W, H = 1280, 720
    cur, dl = info["cur"], info["daily"]
    kind = wmo(cur["weather_code"])
    night = not cur.get("is_day", 1)
    c1, c2 = SKY["night"] if night and kind in ("clear", "partly", "cloud") else SKY[kind]
    base = gradient(W, H, c1, c2).convert("RGBA")
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    pw = (W - 140 - 4 * 20) // 5
    for i in range(5):
        x = 70 + i * (pw + 20)
        ld.rounded_rectangle((x, 470, x + pw, 680), radius=28, fill=(255, 255, 255, 48))
    base.alpha_composite(lay)
    d = ImageDraw.Draw(base)

    def T(xy, text, size, fill=(255, 255, 255, 255), bold=False, anchor="la"):
        d.text(xy, text, font=get_font(text, size, bold), fill=fill, anchor=anchor)

    T((70, 55), f"{info['city']}, {info['country']}".strip(", "), 62, bold=True)
    T((72, 135), LABEL[kind][1 if hi else 0], 40, (255, 255, 255, 230))
    T((66, 175), f"{round(cur['temperature_2m'])}°", 200, bold=True)
    feels, hum, wind = ("महसूस", "नमी", "हवा") if hi else ("Feels like", "Humidity", "Wind")
    T((74, 412), f"{feels} {round(cur['apparent_temperature'])}°   {hum} {cur['relative_humidity_2m']}%   "
                 f"{wind} {round(cur['wind_speed_10m'])} km/h", 30, (255, 255, 255, 235))
    base.alpha_composite(weather_icon(kind, 360, night), (800, 50))
    for i in range(5):
        x = 70 + i * (pw + 20)
        dt = datetime.strptime(dl["time"][i], "%Y-%m-%d")
        name = "आज" if (i == 0 and hi) else "Today" if i == 0 else (DAYS_HI[dt.weekday()] if hi else dt.strftime("%a"))
        T((x + pw / 2, 482), name, 30, bold=True, anchor="ma")
        base.alpha_composite(weather_icon(wmo(dl["weather_code"][i]), 96), (int(x + pw / 2 - 48), 516))
        T((x + pw / 2, 608), f"{round(dl['temperature_2m_max'][i])}° / {round(dl['temperature_2m_min'][i])}°", 28, anchor="ma")
        pr = dl["precipitation_probability_max"][i]
        if pr is not None:
            T((x + pw / 2, 646), f"{'बारिश' if hi else 'Rain'} {pr}%", 22, (255, 255, 255, 200), anchor="ma")
    return base.convert("RGB")


def parse_chart(text):
    title = ""
    if ":" in text and "=" in text.split(":", 1)[1]:
        title, text = text.split(":", 1)
    pairs = [(a.strip(), float(b)) for a, b in re.findall(r"([^=,;\n]+?)\s*=\s*(-?\d+(?:\.\d+)?)", text)]
    return title.strip(), pairs


def chart_image(title, pairs):
    W, H = 1280, 720
    im = gradient(W, H, (18, 24, 56), (40, 52, 110))
    d = ImageDraw.Draw(im)
    if title:
        d.text((W / 2, 50), title, font=get_font(title, 54, True), fill="white", anchor="ma")
    n = len(pairs)
    mx = max(max(v for _, v in pairs), 1e-9)
    left, right, top, bottom = 90, W - 70, 170, H - 120
    pal = [(255, 99, 132), (54, 162, 235), (255, 206, 86), (75, 192, 192), (153, 102, 255), (255, 159, 64)]
    d.line((left, bottom, right, bottom), fill=(200, 210, 240), width=3)
    slot = (right - left) / n
    bw = min(150, slot * 0.6)
    for i, (lab, v) in enumerate(pairs):
        cx = left + slot * (i + 0.5)
        h = max(2, (bottom - top) * max(v, 0) / mx)
        d.rounded_rectangle((cx - bw / 2, bottom - h, cx + bw / 2, bottom), radius=12, fill=pal[i % len(pal)])
        vs = f"{v:g}"
        d.text((cx, bottom - h - 12), vs, font=get_font(vs, 34, True), fill="white", anchor="ms")
        d.text((cx, bottom + 22), lab[:14], font=get_font(lab, 30), fill=(225, 232, 255), anchor="ma")
    return im


def make_poster(text, author, bg):
    S = 1080
    if bg is not None:
        im = ImageEnhance.Brightness(cover(bg, S, S)).enhance(0.45)
    else:
        c = random.choice(list(MOOD_COLORS.values()))
        im = gradient(S, S, c[0], c[1])
    d = ImageDraw.Draw(im)
    for size in range(84, 27, -4):
        font = get_font(text, size, True)
        lines = wrap_px(text, font, S - 190)
        if len(lines) * size * 1.4 <= S - 400:
            break
    y = (S - len(lines) * size * 1.4) / 2 - 20
    for ln in lines:
        d.text((S / 2, y), ln, font=font, fill="white", anchor="ma", stroke_width=2, stroke_fill=(0, 0, 0))
        y += size * 1.4
    if author:
        a = "— " + author
        d.text((S / 2, S - 150), a, font=get_font(a, 40), fill=(235, 235, 235), anchor="ma", stroke_width=1, stroke_fill=(0, 0, 0))
    return im


# =====================================================================
#  TOOLS: wish animation, meme, photo effects, QR, calculators, PDF, net helpers
# =====================================================================
STYLE_ALIAS = {"birthday": "confetti", "janamdin": "confetti", "party": "confetti", "newyear": "confetti",
               "confetti": "confetti", "diwali": "stars", "stars": "stars", "tare": "stars", "eid": "stars",
               "love": "hearts", "pyar": "hearts", "hearts": "hearts", "winter": "snow", "snow": "snow",
               "christmas": "snow", "bubbles": "bubbles", "holi": "confetti"}
STYLE_BG = {"confetti": ((255, 94, 98), (110, 60, 200)), "stars": ((10, 10, 45), (70, 40, 130)),
            "hearts": ((255, 95, 140), (150, 50, 160)), "snow": ((40, 90, 160), (150, 200, 240)),
            "bubbles": ((20, 120, 160), (90, 210, 200))}
PCOL = [(255, 90, 90), (255, 200, 60), (90, 200, 120), (80, 160, 255), (200, 110, 255), (255, 140, 200)]
_HEART = [(16 * math.sin(a) ** 3, -(13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)))
          for a in np.linspace(0, 2 * math.pi, 28)]


def particle_video(path, title, sub, style, secs, F=None):
    """Poora code se animation: gradient + gire/uthte particles + text pop-in. Har frame alag."""
    W, H = vw(), vh()
    F = F or max(fps(), 15)
    n = max(4, int(secs * F))
    rng = np.random.default_rng(7)
    N = 60 if style == "stars" else 70
    px, py = rng.uniform(0, W, N), rng.uniform(0, H, N)
    sp = rng.uniform(0.15, 0.5, N) * H
    sz = rng.uniform(0.014, 0.034, N) * H
    rot, vr, ph = rng.uniform(0, 6.28, N), rng.uniform(-3, 3, N), rng.uniform(0, 6.28, N)
    c1, c2 = STYLE_BG.get(style, STYLE_BG["confetti"])
    g1, g2 = gradient(W, H, c1, c2), gradient(W, H, c2, c1)
    # text layer
    tl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tl)
    for size in range(int(H * 0.16), 19, -4):
        font = get_font(title, size, True)
        lines = wrap_px(title, font, int(W * 0.86))
        if len(lines) * size * 1.3 <= H * 0.55:
            break
    total = len(lines) * size * 1.3 + (int(size * 0.7) if sub else 0)
    y = H / 2 - total / 2
    for ln in lines:
        td.text((W / 2, y), ln, font=font, fill=(255, 255, 255, 255), anchor="ma",
                stroke_width=max(2, size // 16), stroke_fill=(0, 0, 0, 200))
        y += size * 1.3
    if sub:
        sf = get_font(sub, max(14, int(size * 0.45)))
        td.text((W / 2, y + 4), sub, font=sf, fill=(255, 255, 255, 235), anchor="ma",
                stroke_width=1, stroke_fill=(0, 0, 0, 160))
    bbox = tl.getbbox() or (0, 0, W, H)
    tbox = tl.crop(bbox)
    tcx, tcy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    with open_writer(path) as w:
        for f in range(n):
            t = f / F
            base = Image.blend(g1, g2, 0.5 + 0.5 * math.sin(t * 0.8)).convert("RGBA")
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            for i in range(N):
                col = PCOL[i % len(PCOL)]
                s = float(sz[i])
                if style == "stars":
                    x, yy = px[i], py[i]
                    a = int(130 + 120 * math.sin(t * 3 + ph[i]))
                    pts = [(x + (s if k % 2 == 0 else s * .3) * math.cos(k * math.pi / 4),
                            yy + (s if k % 2 == 0 else s * .3) * math.sin(k * math.pi / 4)) for k in range(8)]
                    d.polygon(pts, fill=(255, 245, 190, max(40, min(255, a))))
                elif style in ("confetti", "snow"):
                    yy = (py[i] + sp[i] * t * (1 if style == "confetti" else 0.5)) % (H + 40) - 20
                    x = px[i] + math.sin(t * 1.3 + ph[i]) * 18
                    if style == "snow":
                        d.ellipse((x - s * .5, yy - s * .5, x + s * .5, yy + s * .5), fill=(255, 255, 255, 220))
                    else:
                        a = rot[i] + vr[i] * t
                        cw, chh = s, s * .5
                        pts = [(x + math.cos(a) * dx - math.sin(a) * dy, yy + math.sin(a) * dx + math.cos(a) * dy)
                               for dx, dy in ((-cw, -chh), (cw, -chh), (cw, chh), (-cw, chh))]
                        d.polygon(pts, fill=col + (230,))
                else:  # bubbles / hearts rise up
                    yy = (py[i] - sp[i] * t * 0.8) % (H + 40) - 20
                    x = px[i] + math.sin(t * 1.1 + ph[i]) * 22
                    if style == "bubbles":
                        d.ellipse((x - s, yy - s, x + s, yy + s), outline=(255, 255, 255, 190), width=2)
                        d.ellipse((x - s * .5, yy - s * .6, x - s * .2, yy - s * .3), fill=(255, 255, 255, 170))
                    else:
                        sc = s / 16.0
                        d.polygon([(x + hx * sc, yy + hy * sc) for hx, hy in _HEART], fill=col + (215,))
            base.alpha_composite(ov)
            q = min(1.0, t / 0.9)
            e = 1 + 2.70158 * (q - 1) ** 3 + 1.70158 * (q - 1) ** 2  # ease-out-back
            tb = tbox.resize((max(1, int(tbox.width * e)), max(1, int(tbox.height * e))), RS.BILINEAR)
            a = min(1.0, t / 0.5)
            if a < 1:
                tb.putalpha(tb.getchannel("A").point(lambda v: int(v * a)))
            cy = tcy + math.sin(t * 2.2) * H * 0.008
            base.paste(tb, (int(tcx - tb.width / 2), int(cy - tb.height / 2)), tb)
            w.append_data(np.asarray(base.convert("RGB")))


def meme_image(im, top, bottom):
    im = im.convert("RGB")
    W = 900
    H = min(1300, int(W * im.height / im.width))
    im = im.resize((W, H), RS.LANCZOS)
    d = ImageDraw.Draw(im)

    def put(text, at_top):
        text = text.strip()
        if not text:
            return
        text = text.upper() if text.isascii() else text
        for size in range(int(W * 0.11), 17, -3):
            font = get_font(text, size, True)
            lines = wrap_px(text, font, int(W * 0.94))
            if len(lines) <= 3:
                break
        y = 10 if at_top else H - len(lines) * size * 1.15 - 14
        for ln in lines:
            d.text((W / 2, y), ln, font=font, fill="white", anchor="ma",
                   stroke_width=max(2, size // 10), stroke_fill="black")
            y += size * 1.15

    put(top, True)
    put(bottom, False)
    return im


FX = ["bw", "sepia", "blur", "sharpen", "sketch", "cartoon", "pixel", "invert", "mirror", "vivid"]
FX_LABEL = {"bw": "⚫ B&W", "sepia": "🟤 Sepia", "blur": "🌫 Blur", "sharpen": "🔍 Sharp", "sketch": "✏️ Sketch",
            "cartoon": "🎨 Cartoon", "pixel": "🔲 Pixel", "invert": "🔄 Invert", "mirror": "🪞 Mirror", "vivid": "🌈 Vivid"}


def apply_fx(im, name):
    im = im.convert("RGB")
    if max(im.size) > 1600:
        im.thumbnail((1600, 1600))
    if name == "bw":
        return ImageOps.grayscale(im).convert("RGB")
    if name == "sepia":
        return ImageOps.colorize(ImageOps.grayscale(im), (30, 15, 0), (255, 240, 205))
    if name == "blur":
        return im.filter(ImageFilter.GaussianBlur(5))
    if name == "sharpen":
        return im.filter(ImageFilter.UnsharpMask(radius=2, percent=220, threshold=2))
    if name == "invert":
        return ImageOps.invert(im)
    if name == "mirror":
        return ImageOps.mirror(im)
    if name == "pixel":
        s = max(8, im.width // 64)
        return im.resize((max(1, im.width // s), max(1, im.height // s)), RS.BILINEAR).resize(im.size, RS.NEAREST)
    if name == "sketch":
        g = np.asarray(ImageOps.grayscale(im), dtype=np.float32)
        b = np.asarray(Image.fromarray((255 - g).astype(np.uint8)).filter(ImageFilter.GaussianBlur(10)), dtype=np.float32)
        return Image.fromarray(np.clip(g * 255.0 / (255.0 - b + 1.0), 0, 255).astype(np.uint8)).convert("RGB")
    if name == "cartoon":
        p = ImageOps.posterize(ImageEnhance.Color(im).enhance(1.4), 3)
        edges = ImageOps.grayscale(im).filter(ImageFilter.GaussianBlur(1)).filter(ImageFilter.FIND_EDGES)
        p.paste((0, 0, 0), mask=edges.point(lambda v: 255 if v > 38 else 0))
        return p
    if name == "vivid":
        return ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(1.6)).enhance(1.15)
    raise ValueError("effect nahi mila")


# ---------------- QR ----------------
def qr_matrix(text):
    try:
        import segno
        return [list(r) for r in segno.make(text, error="m", micro=False).matrix]
    except ImportError:
        pass
    try:
        import qrcode
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
        q.add_data(text)
        q.make(fit=True)
        return q.get_matrix()
    except ImportError:
        raise RuntimeError("QR library install nahi hui. requirements.txt me segno honi chahiye.")


def qr_image(text, fg=(0, 0, 0), bg=(255, 255, 255), size=840):
    m = qr_matrix(text)
    n = len(m)
    cell = max(6, size // (n + 8))
    S = (n + 8) * cell
    im = Image.new("RGB", (S, S), bg)
    d = ImageDraw.Draw(im)
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                d.rectangle(((x + 4) * cell, (y + 4) * cell, (x + 5) * cell - 1, (y + 5) * cell - 1), fill=fg)
    return im


def wifi_string(name, pw, kind="WPA"):
    esc = lambda s: re.sub(r'([\\;,:"])', r"\\\1", s)  # noqa: E731
    if kind.upper() == "NOPASS" or not pw:
        return f"WIFI:T:nopass;S:{esc(name)};;"
    return f"WIFI:T:{kind.upper()};S:{esc(name)};P:{esc(pw)};;"


# ---------------- Calculators ----------------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.FloorDiv: operator.floordiv}
_FN = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
       "exp": math.exp, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
       "radians": math.radians, "degrees": math.degrees, "factorial": math.factorial}
_CONST = {"pi": math.pi, "e": math.e}


def safe_eval(expr):
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%\s*(?:of|ka)\s*(\d+(?:\.\d+)?)", expr, re.I)
    if m:
        return float(m.group(1)) * float(m.group(2)) / 100

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 1000 or abs(a) > 1e6):
                raise ValueError("bahut bada hisaab")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN and not n.keywords:
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and (not args or args[0] > 170):
                raise ValueError("factorial bahut bada")
            return _FN[n.func.id](*args)
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        raise ValueError("ye expression allowed nahi")

    return ev(ast.parse(expr, mode="eval"))


UNITS = {
    "length": {"m": 1, "meter": 1, "metre": 1, "km": 1000, "cm": .01, "mm": .001, "mile": 1609.344, "miles": 1609.344,
               "ft": .3048, "feet": .3048, "foot": .3048, "inch": .0254, "inches": .0254, "yard": .9144, "yards": .9144},
    "mass": {"kg": 1, "g": .001, "mg": 1e-6, "lb": .45359237, "lbs": .45359237, "oz": .028349523, "quintal": 100,
             "ton": 1000, "tonne": 1000},
    "volume": {"l": 1, "liter": 1, "litre": 1, "ml": .001, "gallon": 3.785411784, "cup": .2365882365}}


def convert_units(v, a, b):
    a, b = a.lower().replace("°", ""), b.lower().replace("°", "")
    if a in ("c", "f", "k") and b in ("c", "f", "k"):
        c = v if a == "c" else (v - 32) * 5 / 9 if a == "f" else v - 273.15
        return c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15
    for table in UNITS.values():
        if a in table and b in table:
            return v * table[a] / table[b]
    raise ValueError("ye units samajh nahi aaye")


def emi_calc(p, annual, years):
    n = max(1, round(years * 12))
    r = annual / 12 / 100
    emi = p / n if r == 0 else p * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return emi, emi * n, emi * n - p, n


def age_calc(dob, today=None):
    today = today or date.today()
    y, m, d = today.year - dob.year, today.month - dob.month, today.day - dob.day
    if d < 0:
        m -= 1
        pm = today.month - 1 or 12
        d += [31, 29 if today.year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][pm - 1]
    if m < 0:
        y -= 1
        m += 12
    try:
        nb = dob.replace(year=today.year)
    except ValueError:
        nb = date(today.year, 3, 1)
    if nb < today:
        try:
            nb = dob.replace(year=today.year + 1)
        except ValueError:
            nb = date(today.year + 1, 3, 1)
    return y, m, d, (today - dob).days, (nb - today).days


def parse_date(s):
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.strip(), f).date()
        except ValueError:
            pass
    raise ValueError("Tarikh aise likhein: 2000-05-14 ya 14-05-2000")


# ---------------- PDF ----------------
def text_to_pdf(text, title=""):
    W, H, M = 1240, 1754, 110
    size = 34
    font = get_font(text + title, size)
    lh = int(size * 1.6)
    lines = []
    for para in text.split("\n"):
        lines += wrap_px(para, font, W - 2 * M) or [""]
    per = (H - 2 * M) // lh
    pages = []
    for i in range(0, max(1, len(lines)), per):
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        y = M
        if i == 0 and title:
            tf = get_font(title, 52, True)
            d.text((M, y), title, font=tf, fill=(20, 20, 20))
            y += 90
        for ln in lines[i:i + per]:
            d.text((M, y), ln, font=font, fill=(20, 20, 20))
            y += lh
        pages.append(im)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buf.getvalue()


# ---------------- Net helpers ----------------
def is_public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa
        return False
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


async def fetch_page_text(url):
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=False) as c:
        r = None
        for _ in range(4):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname or not await asyncio.to_thread(is_public_host, p.hostname):
                raise RuntimeError("Ye link allowed nahi hai")
            r = await c.get(url)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(httpx.URL(url).join(r.headers["location"]))
                continue
            break
    t = r.text[:600000]
    t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()[:12000]


async def fetch_news(topic):
    url = ("https://news.google.com/rss/search?q=" + quote(topic) + "&hl=hi&gl=IN&ceid=IN:hi") if topic \
        else "https://news.google.com/rss?hl=hi&gl=IN&ceid=IN:hi"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as c:
        r = await c.get(url)
    items = [(it.findtext("title") or "", it.findtext("link") or "") for it in ET.fromstring(r.content).iter("item")]
    return items[:6]


async def fetch_wiki(topic):
    lang = "hi" if has_deva(topic) else "en"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as c:
        s = (await c.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": topic, "format": "json", "srlimit": 1})).json()
        hits = s.get("query", {}).get("search", [])
        if not hits:
            raise RuntimeError("Wikipedia par kuchh nahi mila")
        title = hits[0]["title"]
        r = (await c.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_")))).json()
    return {"title": r.get("title", title), "text": r.get("extract", ""),
            "img": (r.get("thumbnail") or {}).get("source"),
            "url": ((r.get("content_urls") or {}).get("desktop") or {}).get("page", "")}


async def fetch_rate(amount, a, b):
    async with httpx.AsyncClient(timeout=20, headers=UA) as c:
        r = (await c.get(f"https://open.er-api.com/v6/latest/{a.upper()}")).json()
    rate = (r.get("rates") or {}).get(b.upper())
    if r.get("result") != "success" or rate is None:
        raise RuntimeError("Currency code galat hai (jaise USD, INR, EUR)")
    return rate, amount * rate


async def fetch_tz(city):
    async with httpx.AsyncClient(timeout=20) as c:
        g = (await c.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "en"})).json()
    if not g.get("results"):
        raise RuntimeError(f"'{city}' shehar nahi mila")
    r0 = g["results"][0]
    return r0["name"], r0.get("country", ""), r0["timezone"]


def load_notes():
    try:
        return json.load(open(NOTES_FILE))
    except Exception:  # noqa
        return {}


def save_notes(d):
    try:
        json.dump(d, open(NOTES_FILE, "w"))
    except Exception as e:  # noqa
        log.warning("notes save fail: %s", e)


# =====================================================================
#  STORY: sawal -> kahani (JSON) -> har scene me kai shots (photo/vector/code-animation)
#  + awaaz + music -> title card + scenes + end card (apna message) -> video
# =====================================================================
QS = [
    ("lang", "Kahani kis bhasha me chahiye?", [("Hindi", "hindi"), ("English", "english")]),
    ("genre", "Kaisi kahani chahiye?", [("Naitik/seekh", "moral"), ("Adventure", "adventure"), ("Hasya", "funny"),
                                         ("Rahasya", "mystery"), ("Bhavuk", "emotional"), ("Sci-fi", "sci-fi")]),
    ("scenes", "Kitne parts (scenes) ki?", [("3", "3"), ("5", "5"), ("7", "7")]),
    ("visual", "Visuals kaise chahiye?", [("Real photos", "photo"), ("Vector art", "vector"),
                                          ("Dono mix", "mix"), ("Code animation", "anim")]),
    ("voice", "Kiski awaaz?", [("Purush", "male"), ("Mahila", "female")]),
]
GENRE_STYLE = {"moral": "stars", "adventure": "bubbles", "funny": "confetti", "mystery": "stars",
               "emotional": "hearts", "sci-fi": "stars"}


def default_opts():
    return {"lang": lang_default(), "genre": "moral", "scenes": "5", "visual": "mix", "voice": "male"}


def q_keyboard(step):
    key, _, opts = QS[step]
    btns = [InlineKeyboardButton(lab, callback_data=f"sq:{step}:{val}") for lab, val in opts]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton("⚡ Baaki default se shuru karo", callback_data=f"sq:{step}:fast")])
    return InlineKeyboardMarkup(rows)


def split_msg(text):
    """'kahani ka vishay | msg: Happy Birthday Raman' -> (vishay, message)"""
    m = re.search(r"\|\s*(?:msg|message|sandesh)\s*:\s*(.+)$", text, re.I | re.S)
    if m:
        return text[:m.start()].strip(), m.group(1).strip()
    return text.strip(), ""


async def write_story(topic, o):
    n = int(o["scenes"])
    lang_txt = "Hindi (Devanagari script me, saral bolchaal wali)" if o["lang"] == "hindi" else "simple English"
    system = (
        "Tum behtareen kahani-lekhak ho. Sirf valid JSON do, aur kuchh nahi. Format: "
        '{"title": "...", "mood": "calm|happy|sad|tense|epic|mystery", "scenes": [{"narration": "...", '
        '"image_queries": ["...", "...", "..."], "mood": "calm|happy|sad|tense|epic|mystery"}]}. '
        f"Rules: theek {n} scenes; kahani ka prakar: {o['genre']}; narration ki bhasha: {lang_txt}; "
        "har scene me 3 chhote vaakya (35-50 shabd) jo awaaz me padhne layak hon; emoji, star, bracket mat likho; "
        "image_queries me har vaakya ke liye ek alag English query (3-6 shabd) ho jo us vaakya ka concrete drishya bataye "
        "(jaise 'old man walking village road sunset'), alag-alag angle/jagah se, naam ya abstract baat nahi; "
        "kahani ka shuru, madhya aur ant saaf ho, ant me seekh ya twist ho.")
    last = None
    for _ in range(2):
        try:
            data = parse_json(await ask(system, f"Kahani ka vishay: {topic}", temperature=0.9, max_tokens=6000))
            sc = [s for s in data.get("scenes", []) if str(s.get("narration", "")).strip()]
            if sc:
                data["scenes"] = sc[:n]
                data.setdefault("title", topic)
                return data
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"Kahani nahi ban payi: {last}")


async def one_visual(query, text, want_photo):
    """(PIL image 1.3x, credit). Photo -> vector -> gradient fallback."""
    W, H = int(vw() * 1.3), int(vh() * 1.3)
    if want_photo:
        try:
            got = await get_photos(query, 1)
            if got:
                return cover(got[0][0], W, H), got[0][1]
        except Exception as e:  # noqa
            log.info("photo fail: %s", e)
    try:
        out = await ask(SVG_SYS, f"Drishya: {query}. Kahani ka hissa: {text[:200]}", temperature=0.8, max_tokens=5000)
        png = await asyncio.to_thread(svg_png, get_svg(out), W)
        return cover(Image.open(io.BytesIO(png)).convert("RGB"), W, H), ""
    except Exception as e:  # noqa
        log.info("vector fail: %s", e)
    c = random.choice(list(MOOD_COLORS.values()))
    return gradient(W, H, c[0], c[1]), ""


async def scene_shots(sc, i, o, topic, sents):
    qs = sc.get("image_queries") or ([sc["image_query"]] if sc.get("image_query") else []) or [topic]
    ns = min(3, len(sents))
    groups = np.array_split(np.arange(len(sents)), ns)
    shots, credits = [], []
    for j, g in enumerate(groups):
        text = " ".join(sents[x] for x in g)
        q = qs[min(int(g[0]), len(qs) - 1)]
        mode = o["visual"]
        want_photo = mode == "photo" or (mode == "mix" and (i + j) % 2 == 0)
        im, cr = await one_visual(q, text, want_photo)
        shots.append(im)
        if cr:
            credits.append(cr)
    return shots, credits


async def make_scene(d, i, sc, o, story_mood, topic):
    voice = os.path.join(d, f"v{i}.mp3")
    await tts(sc["narration"], o["lang"], o["voice"], voice)
    dur = await asyncio.to_thread(media_duration, voice) + 0.8
    sents = split_sentences(sc["narration"])
    wav, aud, silent, clip = (os.path.join(d, f"{n}{i}.{e}") for n, e in
                              (("m", "wav"), ("a", "m4a"), ("s", "mp4"), ("c", "mp4")))
    await asyncio.to_thread(synth_music, fix_mood(sc.get("mood") or story_mood), dur + 0.5, wav)
    await asyncio.to_thread(mix_audio, voice, wav, aud)
    credits, built = [], False
    if o["visual"] == "anim":
        try:
            q = (sc.get("image_queries") or [topic])[0]
            frames = await get_code_frames(f"Drishya: {q}. Kahani ka hissa: {sc['narration'][:200]}",
                                           min(int(dur * fps()), fps() * 20))
            await asyncio.to_thread(build_code_scene, frames, sents, silent)
            built = True
        except Exception as e:  # noqa
            log.info("anim scene fail (%s), shots par ja raha hun", e)
    if not built:
        shots, credits = await scene_shots(sc, i, o, topic, sents)
        await asyncio.to_thread(build_scene_video, shots, sents, dur, silent, i)
    await asyncio.to_thread(ff, "-i", silent, "-i", aud, "-c:v", "copy", "-c:a", "copy", "-shortest", clip)
    return clip, credits


async def make_card(d, name, title, sub, speak, o, mood, style):
    """Title / end card: animated particles + text (+ awaaz agar speak diya ho)."""
    v, dur = None, 3.2
    if speak:
        try:
            v = os.path.join(d, f"{name}.mp3")
            await tts(speak, o["lang"], o["voice"], v)
            dur = await asyncio.to_thread(media_duration, v) + 1.2
        except Exception as e:  # noqa
            log.info("card voice fail: %s", e)
            v = None
    silent, wav, aud, clip = (os.path.join(d, f"{name}.{e}") for e in ("s.mp4", "m.wav", "a.m4a", "c.mp4"))
    await asyncio.to_thread(particle_video, silent, title, sub, style, dur)
    await asyncio.to_thread(synth_music, mood, dur + 0.5, wav)
    if v:
        await asyncio.to_thread(mix_audio, v, wav, aud)
    else:
        await asyncio.to_thread(music_only, wav, aud, 0.4)
    await asyncio.to_thread(ff, "-i", silent, "-i", aud, "-c:v", "copy", "-c:a", "copy", "-shortest", clip)
    return clip


async def say(bot, cid, text):
    for i in range(0, len(text), 4000):
        await bot.send_message(cid, text[i:i + 4000])


async def send_video_file(bot, cid, path, caption=None):
    with open(path, "rb") as f:
        await bot.send_video(cid, f, caption=caption, supports_streaming=True, width=vw(), height=vh(),
                             read_timeout=300, write_timeout=300)


async def run_story(bot, cid, topic, o, message=""):
    status = None
    try:
        async with heavy:
            status = await bot.send_message(cid, "✍️ Kahani likh raha hun...")
            data = await write_story(topic, o)
            sc = data["scenes"]
            await say(bot, cid, f"📖 {data['title']}\n\n" + "\n\n".join(f"{i}. {s['narration']}" for i, s in enumerate(sc, 1)))
            hi = o["lang"] == "hindi"
            style = GENRE_STYLE.get(o["genre"], "stars")
            clips, credits = [], []
            with tempfile.TemporaryDirectory() as d:
                await status.edit_text("🎬 Title card ban raha hai...")
                clips.append(await make_card(d, "t", data["title"], "एक कहानी" if hi else "A story", "",
                                             o, fix_mood(data.get("mood")), style))
                for i, s in enumerate(sc, 1):
                    await status.edit_text(f"🎬 Part {i}/{len(sc)} ban raha hai (visuals + awaaz + music)...")
                    clip, cr = await make_scene(d, i, s, o, data.get("mood"), topic)
                    clips.append(clip)
                    credits += cr
                    if str(cfg("SEND_PARTS")).strip() in ("1", "yes", "haan", "true"):
                        await send_video_file(bot, cid, clip, f"Part {i}/{len(sc)}")
                await status.edit_text("🎬 End card ban raha hai...")
                end_title = message or ("समाप्त" if hi else "The End")
                clips.append(await make_card(d, "e", end_title, data["title"], message, o, "happy", style))
                await status.edit_text("🎞️ Saare parts jod raha hun...")
                lst, final = os.path.join(d, "list.txt"), os.path.join(d, "final.mp4")
                open(lst, "w").write("".join(f"file '{c}'\n" for c in clips))
                await asyncio.to_thread(ff, "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                                        "-movflags", "+faststart", final)
                await send_video_file(bot, cid, final, f"🎬 {data['title']} (poori kahani)")
            if credits:
                await say(bot, cid, "📷 Photo credits:\n" + "\n".join(sorted(set(credits))))
    except Exception as e:  # noqa
        log.exception("story")
        await bot.send_message(cid, f"Error: {e}")
    finally:
        if status:
            try:
                await status.delete()
            except Exception:  # noqa
                pass


# =====================================================================
#  TELEGRAM HANDLERS
# =====================================================================
class Usage(Exception):
    pass


def allowed(update: Update):
    ids = allowed_ids()
    return not ids or (update.effective_user and update.effective_user.id in ids)


def arg_text(update, ctx):
    forced = ctx.user_data.pop("_arg", None) if ctx.user_data is not None else None
    if forced is not None:
        return forced.strip()
    t = " ".join(ctx.args or []).strip()
    if not t and update.message and update.message.reply_to_message:
        t = (update.message.reply_to_message.text or update.message.reply_to_message.caption or "").strip()
    return t


def job(label, heavy_task=False):
    def deco(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not allowed(update):
                return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
            msg = await update.message.reply_text(label)
            try:
                if heavy_task:
                    async with heavy:
                        await fn(update, ctx)
                else:
                    await fn(update, ctx)
            except Usage as u:
                await update.message.reply_text(str(u))
            except Exception as e:  # noqa
                log.exception(fn.__name__)
                await update.message.reply_text(f"Error: {e}")
            finally:
                try:
                    await msg.delete()
                except Exception:  # noqa
                    pass
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


HELP = (
    "Namaste! Main {name} hun. Mere features:\n\n"
    "🎬 BANANE WALE\n"
    "/story <vishay> [| msg: apna sandesh] - sawal puchh kar poori kahani: photo/vector/code-animation, Hindi awaaz, music, parts + joda hua video\n"
    "/video <vivaran> [| msg: text] - code se smooth animation + music\n"
    "/wish <style> | <sandesh> - animated wish video (birthday, diwali, love, snow, bubbles)\n"
    "/img <vivaran> - vector image (PNG + SVG)\n"
    "/photo <shabd> - real photo search\n"
    "/weather <shehar> - mausam graphic card\n"
    "/quote <vishay> - quote poster, /meme up | niche | photo-shabd - meme\n"
    "/chart Title: A=10, B=20 - bar chart, /qr <text>, /wifi naam | password - QR code\n"
    "/music <mood> [second], /tts <text> - music aur awaaz\n"
    "📷 Photo bhejein - 10 effects (B&W, sketch, cartoon...) ya caption me 'meme upar | niche'\n\n"
    "🧰 TOOLS\n"
    "/calc 25*4+10 ya 20% of 500, /convert 10 km to miles, /currency 100 usd inr, /emi 500000 9 5, /age 2000-05-14\n"
    "/news [vishay], /wiki <vishay>, /time <shehar>, /summarize <link ya text>, /translate hindi: text\n"
    "/pdf <text>, /pass [lambai], /pick a, b, c, /dice, /coin, /remind 10m kaam\n"
    "/note <text>, /notes, /delnote <number>\n"
    "/shayari <vishay>, /joke, /fact, /code <kaam>\n\n"
    "🧠 YAADDASHT: bot aapki baatein yaad rakhta hai. /remember <baat>, /memory, /forget <number|all>\n\n"
    "🎤 Voice message bhejein to samajh kar jawab dunga. /voice on = jawab awaaz me\n"
    "Seedha likh sakte hain: 'Mumbai ka mausam', '100 usd to inr', 'ek kahani banao...'\n"
    "/diag - jaanch, /id - aapki ID, /reset - baatcheet reset")


async def cmd_start(update: Update, ctx):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    await update.message.reply_text(HELP.format(name=cfg("BOT_NAME")))


async def cmd_id(update: Update, ctx):
    await update.message.reply_text(f"Aapki ID: {update.effective_user.id}")


async def cmd_reset(update: Update, ctx):
    cid = update.effective_chat.id
    history.pop(cid, None)
    try:
        await store.set(f"h:{cid}", [])
    except Exception:  # noqa
        pass
    await update.message.reply_text("Baatcheet reset ho gayi. Aapke baare me yaad rakhi baatein bani hain "
                                    "(hatane ke liye /forget all).")


async def cmd_voice(update: Update, ctx):
    if not allowed(update):
        return
    on = (ctx.args[0].lower() if ctx.args else "") in ("on", "haan", "1")
    ctx.user_data["voice"] = on
    await update.message.reply_text("Ab jawab awaaz me bhi aayenge 🔊" if on else "Awaaz wale jawab band.")


async def cmd_diag(update: Update, ctx):
    if not allowed(update):
        return
    import importlib.util
    from PIL import features
    have = lambda m: "✅" if importlib.util.find_spec(m) else "❌"  # noqa: E731
    models = await list_models(True)
    best = best_model(models)
    await update.message.reply_text(
        "🔧 Jaanch\n"
        f"Hindi font: {'✅ ' + os.path.basename(font_path(True, False) or '') if font_path(True, False) else '❌'}\n"
        f"Raqm (Hindi shaping): {'✅' if features.check('raqm') else '❌'}\n"
        f"edge-tts (awaaz): {have('edge_tts')} | gTTS: {have('gtts')}\n"
        f"QR: segno {have('segno')} | qrcode {have('qrcode')}\n"
        f"ffmpeg: {'✅' if os.path.exists(FFMPEG) else '❌'}\n"
        f"Photo keys: Pexels {'✅' if str(cfg('PEXELS_KEY')).strip() else '—'} | "
        f"Pixabay {'✅' if str(cfg('PIXABAY_KEY')).strip() else '—'} (Openverse hamesha)\n"
        f"Model (main): {cfg('GROQ_MODEL')} | backup: {cfg('FALLBACK_MODEL')}\n"
        f"Sabse powerful available: {best}\n"
        f"Available models: {', '.join(models[:12]) or 'list nahi mili'}\n"
        f"Memory: {store.describe()}\n"
        f"Video: {vw()}x{vh()} @ {fps()} fps")


async def download_photo(bot, file_id):
    f = await bot.get_file(file_id)
    return Image.open(io.BytesIO(bytes(await f.download_as_bytearray())))


@job("Image ban rahi hai...", True)
async def cmd_img(update, ctx):
    prompt = arg_text(update, ctx)
    if not prompt:
        raise Usage("Aise likhein: /img ek sunset wala pahad")
    svg, png = await make_svg(prompt)
    await update.message.reply_photo(png)
    await update.message.reply_document(io.BytesIO(png), filename="image.png")
    await update.message.reply_document(io.BytesIO(svg.encode()), filename="image.svg")


@job("Photo dhundh raha hun...")
async def cmd_photo(update, ctx):
    q = arg_text(update, ctx)
    if not q:
        raise Usage("Aise likhein: /photo mountain lake sunrise")
    got = await get_photos(await to_english_query(q), 3)
    if not got:
        raise Usage("Koi photo nahi mili. Dusre shabd try karein.")
    media = [InputMediaPhoto(jpg_bytes(im), caption=(" | ".join(c for _, c in got))[:1000] if i == 0 else None)
             for i, (im, _) in enumerate(got)]
    await update.message.reply_media_group(media)


@job("Video ban rahi hai (1-3 minute lag sakte hain)...", True)
async def cmd_video(update, ctx):
    prompt, message = split_msg(arg_text(update, ctx))
    if not prompt:
        raise Usage("Aise likhein: /video ghoomta hua grah aur taare | msg: Happy Diwali")
    frames = await get_code_frames(prompt, fps() * num("VIDEO_SECONDS", 15))
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.mp4"), os.path.join(d, "b.mp4")
        await asyncio.to_thread(build_anim, frames, a, message or None)
        try:
            await asyncio.to_thread(add_music_to_video, a, "calm", b)
        except Exception as e:  # noqa
            log.warning("music add fail: %s", e)
            b = a
        with open(b, "rb") as f:
            await update.message.reply_video(f, supports_streaming=True, read_timeout=180, write_timeout=180)


@job("Wish video ban rahi hai...", True)
async def cmd_wish(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /wish birthday | Raman ko janamdin mubarak ho!\n"
                    "Styles: birthday, diwali, love, snow, bubbles, stars")
    style, msg = "confetti", t
    if "|" in t:
        a, b = t.split("|", 1)
        if a.strip().lower() in STYLE_ALIAS:
            style, msg = STYLE_ALIAS[a.strip().lower()], b.strip()
    lang = "hindi" if has_deva(msg) else "english"
    with tempfile.TemporaryDirectory() as d:
        v, dur = os.path.join(d, "v.mp3"), 6.0
        try:
            await tts(msg, lang, "female", v)
            dur = max(6.0, await asyncio.to_thread(media_duration, v) + 1.5)
        except Exception as e:  # noqa
            log.info("wish voice fail: %s", e)
            v = None
        silent, wav, aud, out = (os.path.join(d, x) for x in ("s.mp4", "m.wav", "a.m4a", "o.mp4"))
        await asyncio.to_thread(particle_video, silent, msg, "", style, dur, max(fps(), 20))
        await asyncio.to_thread(synth_music, "happy", dur + 0.5, wav)
        if v:
            await asyncio.to_thread(mix_audio, v, wav, aud)
        else:
            await asyncio.to_thread(music_only, wav, aud, 0.5)
        await asyncio.to_thread(ff, "-i", silent, "-i", aud, "-c:v", "copy", "-c:a", "copy", "-shortest",
                                "-movflags", "+faststart", out)
        with open(out, "rb") as f:
            await update.message.reply_video(f, supports_streaming=True, read_timeout=180, write_timeout=180)


@job("Mausam dekh raha hun...")
async def cmd_weather(update, ctx):
    city = arg_text(update, ctx) or str(cfg("DEFAULT_CITY"))
    info = await fetch_weather(city)
    lang = lang_default()
    png = png_bytes(await asyncio.to_thread(weather_card, info, lang))
    cur = info["cur"]
    cap = f"{info['city']}: {round(cur['temperature_2m'])}°C, {LABEL[wmo(cur['weather_code'])][1 if lang == 'hindi' else 0]}"
    await update.message.reply_photo(png, caption=cap)
    await update.message.reply_document(io.BytesIO(png), filename="weather.png")


@job("Samay dekh raha hun...")
async def cmd_time(update, ctx):
    from zoneinfo import ZoneInfo
    city = arg_text(update, ctx) or str(cfg("DEFAULT_CITY"))
    name, country, tz = await fetch_tz(city)
    now = datetime.now(ZoneInfo(tz))
    await update.message.reply_text(f"🕒 {name}, {country}\n{now:%d %b %Y, %I:%M %p}\n({tz})")


@job("Chart ban raha hai...")
async def cmd_chart(update, ctx):
    title, pairs = parse_chart(arg_text(update, ctx))
    if len(pairs) < 2:
        raise Usage("Aise likhein: /chart Bikri: Jan=10, Feb=25, Mar=18")
    png = png_bytes(await asyncio.to_thread(chart_image, title, pairs[:8]))
    await update.message.reply_photo(png)
    await update.message.reply_document(io.BytesIO(png), filename="chart.png")


@job("Poster ban raha hai...")
async def cmd_quote(update, ctx):
    topic = arg_text(update, ctx)
    if not topic:
        raise Usage("Aise likhein: /quote mehnat ka mahatva")
    raw = await ask('Sirf JSON do: {"quote": "...", "author": "...", "query": "..."}. quote user ki bhasha me, '
                    "ek prabhavshali pankti (agar user ne khud poori line likhi hai to wahi rakho); author ka naam "
                    "ya khaali; query English me 3-5 shabd ka background photo (prakritik/shant drishya).",
                    topic, temperature=0.8, max_tokens=800)
    data = parse_json(raw)
    bg = None
    try:
        got = await get_photos(data.get("query") or "calm nature landscape", 1)
        bg = got[0][0] if got else None
    except Exception:  # noqa
        pass
    png = png_bytes(await asyncio.to_thread(make_poster, data["quote"], data.get("author", ""), bg))
    await update.message.reply_photo(png)
    await update.message.reply_document(io.BytesIO(png), filename="quote.png")


@job("Meme ban raha hai...")
async def cmd_meme(update, ctx):
    t = arg_text(update, ctx)
    rp = update.message.reply_to_message
    if not t and not (rp and rp.photo):
        raise Usage("Aise likhein: /meme upar ka text | neeche ka text | photo ka shabd (optional)\n"
                    "Ya kisi photo par reply karke /meme upar | neeche")
    parts = [p.strip() for p in t.split("|")]
    top, bottom, q = (parts + ["", "", ""])[:3]
    if rp and rp.photo:
        bg = await download_photo(ctx.bot, rp.photo[-1].file_id)
    else:
        got = await get_photos(await to_english_query(q or top or bottom or "funny cat"), 1)
        bg = got[0][0] if got else gradient(900, 700, (60, 60, 120), (150, 90, 160))
    await update.message.reply_photo(jpg_bytes(await asyncio.to_thread(meme_image, bg, top, bottom)))


@job("QR ban raha hai...")
async def cmd_qr(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /qr https://example.com")
    png = png_bytes(await asyncio.to_thread(qr_image, t))
    await update.message.reply_photo(png)
    await update.message.reply_document(io.BytesIO(png), filename="qr.png")


@job("WiFi QR ban raha hai...")
async def cmd_wifi(update, ctx):
    parts = [p.strip() for p in arg_text(update, ctx).split("|")]
    if not parts or not parts[0]:
        raise Usage("Aise likhein: /wifi GharKaWiFi | password123 | WPA\n(password na ho to sirf naam likhein)")
    name, pw, kind = (parts + ["", "", ""])[:3]
    png = png_bytes(await asyncio.to_thread(qr_image, wifi_string(name, pw, kind or "WPA")))
    await update.message.reply_photo(png, caption="📶 Is QR ko scan karke seedha WiFi se jud sakte hain")


@job("Awaaz ban rahi hai...")
async def cmd_tts(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /tts namaste doston, kaise hain aap?")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "voice.mp3")
        await tts(t, "hindi" if has_deva(t) else "english", "male", p)
        with open(p, "rb") as f:
            await update.message.reply_audio(f, title="Awaaz")


@job("Music ban raha hai...")
async def cmd_music(update, ctx):
    args = ctx.args or []
    mood = fix_mood(args[0]) if args else "calm"
    secs = min(max(int(args[1]), 5), 120) if len(args) > 1 and args[1].isdigit() else 30
    with tempfile.TemporaryDirectory() as d:
        w, m = os.path.join(d, "m.wav"), os.path.join(d, "m.mp3")
        await asyncio.to_thread(synth_music, mood, secs, w)
        await asyncio.to_thread(ff, "-i", w, "-b:a", "128k", m)
        with open(m, "rb") as f:
            await update.message.reply_audio(f, title=f"{mood} music", performer=str(cfg("BOT_NAME")))


@job("Anuvaad ho raha hai...")
async def cmd_translate(update, ctx):
    t = arg_text(update, ctx)
    if ":" not in t:
        raise Usage("Aise likhein: /translate hindi: Good morning my friend")
    lang, text = t.split(":", 1)
    await update.message.reply_text(
        await ask(f"Neeche ke text ka {lang.strip()} me anuvaad karo. Sirf anuvaad do.", text.strip(), temperature=0.2))


EXT = {"python": "py", "javascript": "js", "html": "html", "css": "css", "java": "java",
       "cpp": "cpp", "c": "c", "bash": "sh", "json": "json", "typescript": "ts"}
CODE_SYS = ("Tum senior programmer ho. User ka kaam poora, working code me likho: ek hi code block me "
            "(```lang), phir 3-4 line me kaise chalana hai. Code me placeholder ya adhura hissa mat chhodo.")


@job("Code likh raha hun...")
async def cmd_code(update, ctx):
    task = arg_text(update, ctx)
    if not task:
        raise Usage("Aise likhein: /code Python me calculator banao")
    out = await ask(CODE_SYS, task, temperature=0.3, max_tokens=8000)
    m = re.search(r"```(\w*)\n(.*?)```", out, re.S)
    if m:
        await update.message.reply_document(io.BytesIO(m.group(2).encode()),
                                            filename=f"main.{EXT.get(m.group(1).lower(), 'txt')}")
        rest = (out[:m.start()] + out[m.end():]).strip()
        if rest:
            await say(ctx.bot, update.effective_chat.id, rest)
    else:
        await say(ctx.bot, update.effective_chat.id, out)


# ---------------- Tools ----------------
@job("Hisaab ho raha hai...")
async def cmd_calc(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /calc 25*4+10   ya   /calc 20% of 500   ya   /calc sqrt(144)")
    try:
        v = safe_eval(t)
    except ZeroDivisionError:
        raise Usage("Zero se bhaag nahi ho sakta")
    except Exception as e:  # noqa
        raise Usage(f"Hisaab nahi ho paya: {e}")
    await update.message.reply_text(f"🧮 {t} = {v:,.10g}")


@job("Badal raha hun...")
async def cmd_convert(update, ctx):
    m = re.match(r"^\s*(-?[\d.]+)\s*([a-zA-Z°]+)\s*(?:to|ko|->|se)\s*([a-zA-Z°]+)\s*$", arg_text(update, ctx))
    if not m:
        raise Usage("Aise likhein: /convert 10 km to miles   (kg, lb, c, f, l, ml, ft, inch...)")
    try:
        r = convert_units(float(m.group(1)), m.group(2), m.group(3))
    except Exception as e:  # noqa
        raise Usage(str(e))
    await update.message.reply_text(f"🔁 {m.group(1)} {m.group(2)} = {r:,.6g} {m.group(3)}")


@job("Rate dekh raha hun...")
async def cmd_currency(update, ctx):
    m = re.search(r"(-?[\d.]+)\s*([a-zA-Z]{3})\s*(?:to|in|ko|se|->)?\s*([a-zA-Z]{3})\b", arg_text(update, ctx))
    if not m:
        raise Usage("Aise likhein: /currency 100 usd inr")
    amount = float(m.group(1))
    rate, res = await fetch_rate(amount, m.group(2), m.group(3))
    await update.message.reply_text(f"💱 {amount:g} {m.group(2).upper()} = {res:,.2f} {m.group(3).upper()}\n"
                                    f"(1 {m.group(2).upper()} = {rate:,.4f} {m.group(3).upper()})")


@job("EMI nikal raha hun...")
async def cmd_emi(update, ctx):
    nums = re.findall(r"\d+(?:\.\d+)?", arg_text(update, ctx).replace(",", ""))
    if len(nums) < 3:
        raise Usage("Aise likhein: /emi 500000 9 5   (loan rashi, saalana byaaj %, saal)")
    p, r, y = (float(x) for x in nums[:3])
    emi, total, interest, n = emi_calc(p, r, y)
    await update.message.reply_text(
        f"🏦 EMI Calculator\nLoan: ₹{p:,.0f}\nByaaj: {r:g}% saalana\nSamay: {n} mahine\n\n"
        f"Maasik EMI: ₹{emi:,.0f}\nKul byaaj: ₹{interest:,.0f}\nKul bhugtaan: ₹{total:,.0f}")


@job("Umar nikal raha hun...")
async def cmd_age(update, ctx):
    try:
        dob = parse_date(arg_text(update, ctx))
    except ValueError as e:
        raise Usage(str(e))
    if dob > date.today():
        raise Usage("Janm tarikh aage ki nahi ho sakti")
    y, m, d, days, nxt = age_calc(dob)
    await update.message.reply_text(f"🎂 Umar: {y} saal, {m} mahine, {d} din\nKul din: {days:,}\n"
                                    f"Agla janamdin: {nxt} din baad")


@job("Password ban raha hai...")
async def cmd_pass(update, ctx):
    a = (ctx.args or [""])[0]
    n = min(max(int(a), 8), 64) if a.isdigit() else 16
    chars = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*?"
    pw = "".join(secrets.choice(chars) for _ in range(n))
    await update.message.reply_text(f"🔐 {pw}\n\n(Ise kisi ko na dikhayein. Chat se delete kar dein.)")


@job("Chun raha hun...")
async def cmd_pick(update, ctx):
    items = [x.strip() for x in re.split(r"[,|]", arg_text(update, ctx)) if x.strip()]
    if len(items) < 2:
        raise Usage("Aise likhein: /pick chai, coffee, lassi")
    await update.message.reply_text(f"🎯 {secrets.choice(items)}")


@job("Pasa fenk raha hun...")
async def cmd_dice(update, ctx):
    a = (ctx.args or ["1"])[0]
    n = min(max(int(a), 1), 6) if a.isdigit() else 1
    r = [secrets.randbelow(6) + 1 for _ in range(n)]
    await update.message.reply_text("🎲 " + " + ".join(map(str, r)) + (f" = {sum(r)}" if n > 1 else ""))


@job("Sikka uchhal raha hun...")
async def cmd_coin(update, ctx):
    await update.message.reply_text("🪙 " + secrets.choice(["Chit (Heads)", "Pat (Tails)"]))


@job("Khabrein dhundh raha hun...")
async def cmd_news(update, ctx):
    items = await fetch_news(arg_text(update, ctx))
    if not items:
        raise Usage("Koi khabar nahi mili")
    await update.message.reply_text("📰 Taaza khabrein\n\n" + "\n\n".join(f"{i}. {t}\n{l}" for i, (t, l) in enumerate(items, 1)))


@job("Wikipedia dekh raha hun...")
async def cmd_wiki(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /wiki Taj Mahal")
    w = await fetch_wiki(t)
    cap = f"📚 {w['title']}\n\n{w['text'][:850]}\n\n{w['url']}"
    try:
        if w["img"]:
            return await update.message.reply_photo(w["img"], caption=cap[:1000])
    except Exception:  # noqa
        pass
    await say(ctx.bot, update.effective_chat.id, cap)


@job("Padh kar saaransh bana raha hun...")
async def cmd_summarize(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /summarize https://example.com/article   ya   /summarize <lamba text>")
    src = await fetch_page_text(t.split()[0]) if re.match(r"https?://", t) else t
    if len(src) < 40:
        raise Usage("Padhne layak text nahi mila")
    lang = "English" if (src.isascii() and lang_default() == "english") else "Hindi"
    await say(ctx.bot, update.effective_chat.id,
              await ask(f"Neeche ke text ka {lang} me 5-6 bullet points me saaransh do. Sirf zaroori baatein.",
                        src[:12000], temperature=0.3))


# (remind_later ab MEMORY section me hai)


@job("Yaad dilane ka time set ho raha hai...")
async def cmd_remind(update, ctx):
    m = re.match(r"^\s*(\d+)\s*(s|sec|m|min|minute|h|hr|hour|d|din)?\s*(.*)$", arg_text(update, ctx), re.I | re.S)
    if not m or not m.group(3).strip():
        raise Usage("Aise likhein: /remind 10m dawa lena   (s, m, h, d)")
    mult = {"s": 1, "sec": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
            "d": 86400, "din": 86400}.get((m.group(2) or "m").lower(), 60)
    secs = int(m.group(1)) * mult
    if secs < 5 or secs > 7 * 86400:
        raise Usage("Time 5 second se 7 din ke beech rakhein")
    r = {"id": secrets.token_hex(4), "cid": update.effective_chat.id, "due": time.time() + secs, "msg": m.group(3).strip()}
    saved = True
    try:
        items = await store.get("reminders", [])
        items.append(r)
        await store.set("reminders", items)
    except Exception as e:  # noqa
        log.warning("reminder save fail: %s", e)
        saved = False
    schedule_reminder(r)
    note = "" if (saved and store.persistent) else "\n(Bot restart hua to ye reminder chala jayega. Hamesha ke liye Upstash memory jodein.)"
    await update.message.reply_text(f"⏰ Theek hai, {int(m.group(1))}{(m.group(2) or 'm')} baad yaad dilaunga.{note}")


@job("Note save ho raha hai...")
async def cmd_note(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /note kal bijli ka bill bharna hai")
    key = f"n:{update.effective_user.id}"
    items = await store.get(key, [])
    items.append(t[:500])
    del items[:-50]
    await store.set(key, items)
    await update.message.reply_text(f"📝 Note #{len(items)} save ho gaya.")


@job("Notes dekh raha hun...")
async def cmd_notes(update, ctx):
    items = await store.get(f"n:{update.effective_user.id}", [])
    if not items:
        raise Usage("Abhi koi note nahi hai. /note text se jodein.")
    await say(ctx.bot, update.effective_chat.id, "📝 Aapke notes\n\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(items, 1)))


@job("Note hata raha hun...")
async def cmd_delnote(update, ctx):
    a = (ctx.args or [""])[0]
    key = f"n:{update.effective_user.id}"
    items = await store.get(key, [])
    if not a.isdigit() or not 1 <= int(a) <= len(items):
        raise Usage("Aise likhein: /delnote 2   (number /notes me dekhein)")
    items.pop(int(a) - 1)
    await store.set(key, items)
    await update.message.reply_text("🗑️ Note hata diya.")


@job("PDF ban raha hai...")
async def cmd_pdf(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /pdf Title | poora text   (ya kisi message par reply karke /pdf)")
    title, body = (t.split("|", 1) + [""])[:2] if "|" in t and len(t.split("|", 1)[0]) < 80 else ("", t)
    pdf = await asyncio.to_thread(text_to_pdf, (body or title).strip(), title.strip() if body else "")
    await update.message.reply_document(io.BytesIO(pdf), filename="document.pdf")


async def fun_text(update, ctx, kind):
    t = arg_text(update, ctx)
    lang = "Hindi (Devanagari)" if (has_deva(t) or lang_default() == "hindi") else "English"
    prompts = {"shayari": f"Ek dil ko chhoone wali {lang} shayari (4 line) likho. Vishay: {t or 'zindagi'}. Sirf shayari do.",
               "joke": f"Ek saaf-suthra, hasane wala {lang} joke sunao. Vishay: {t or 'koi bhi'}. Sirf joke do.",
               "fact": f"Ek dilchasp aur sach {lang} fact batao. Vishay: {t or 'koi bhi'}. 2-3 vaakya me."}
    await say(ctx.bot, update.effective_chat.id,
              await ask("Tum rachnatmak lekhak ho.", prompts[kind], temperature=0.95, max_tokens=700))


@job("Shayari likh raha hun...")
async def cmd_shayari(update, ctx):
    await fun_text(update, ctx, "shayari")


@job("Joke soch raha hun...")
async def cmd_joke(update, ctx):
    await fun_text(update, ctx, "joke")


@job("Fact dhundh raha hun...")
async def cmd_fact(update, ctx):
    await fun_text(update, ctx, "fact")


# ---------------- Memory commands ----------------
@job("Yaad rakh raha hun...")
async def cmd_remember(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /remember meri behen ka naam Priya hai")
    if SENS.search(t):
        raise Usage("Password, OTP, card/bank/Aadhaar number jaisi baatein main yaad nahi rakhta.")
    mem = await get_mem(update.effective_user.id)
    if t[:140].lower() not in [f.lower() for f in mem["facts"]]:
        mem["facts"].append(t[:140])
    del mem["facts"][:-MAXF]
    await store.set(f"u:{update.effective_user.id}", mem)
    await update.message.reply_text(f"🧠 Yaad rakh liya: {t[:140]}")


@job("Yaaddasht dekh raha hun...")
async def cmd_memory(update, ctx):
    uid = update.effective_user.id
    mem = await get_mem(uid)
    a = (ctx.args or [""])[0].lower()
    if a in ("on", "off"):
        mem["on"] = a == "on"
        await store.set(f"u:{uid}", mem)
        return await update.message.reply_text("🧠 Apne aap seekhna chalu." if mem["on"] else "🧠 Apne aap seekhna band. (Purani yaaddasht bani rahegi.)")
    lines = [f"🧠 Meri yaaddasht ({store.describe()})",
             f"Apne aap seekhna: {'chalu' if mem['on'] else 'band'}"]
    if mem["facts"]:
        lines.append("\nAapke baare me:")
        lines += [f"{i}. {f}" for i, f in enumerate(mem["facts"], 1)]
    else:
        lines.append("\nAbhi aapke baare me kuchh yaad nahi. Baat karte rahein ya /remember se bataiye.")
    if mem["summary"]:
        lines.append("\nPichli baatcheet ka saaransh:\n" + mem["summary"])
    lines.append("\nHatane ke liye: /forget <number>, /forget summary, /forget all")
    await say(ctx.bot, update.effective_chat.id, "\n".join(lines))


@job("Bhool raha hun...")
async def cmd_forget(update, ctx):
    uid, cid = update.effective_user.id, update.effective_chat.id
    a = (ctx.args or [""])[0].lower()
    mem = await get_mem(uid)
    if a == "all":
        mem["facts"], mem["summary"] = [], ""
        await store.set(f"h:{cid}", [])
        history.pop(cid, None)
        msg = "🧹 Sab bhool gaya: baatein, saaransh aur pichli baatcheet."
    elif a == "summary":
        mem["summary"] = ""
        msg = "🧹 Saaransh hata diya."
    elif a.isdigit() and 1 <= int(a) <= len(mem["facts"]):
        msg = f"🧹 Bhool gaya: {mem['facts'].pop(int(a) - 1)}"
    else:
        raise Usage("Aise likhein: /forget 2   ya   /forget summary   ya   /forget all   (numbers /memory me dekhein)")
    await store.set(f"u:{uid}", mem)
    await update.message.reply_text(msg)


# ---------------- Story ----------------
async def cmd_story(update: Update, ctx):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    topic, msg = split_msg(arg_text(update, ctx))
    if not topic:
        return await update.message.reply_text(
            "Aise likhein: /story ek chhota robot jo dost dhundta hai\n"
            "Apna sandesh jodna ho: /story birthday ki kahani | msg: Raman ko janamdin mubarak!")
    ctx.user_data["story"] = {"topic": topic, "msg": msg, "opts": {}}
    await update.message.reply_text(f"📖 Vishay: {topic}\n\n1/{len(QS)}  {QS[0][1]}", reply_markup=q_keyboard(0))


async def story_cb(update: Update, ctx):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("Allowed nahi", show_alert=True)
    await q.answer()
    st = ctx.user_data.get("story")
    if not st:
        return await q.edit_message_text("Ye session purana ho gaya. /story dobara chalayein.")
    _, step, val = q.data.split(":", 2)
    step = int(step)
    if val == "fast":
        step = len(QS)
    else:
        st["opts"][QS[step][0]] = val
        step += 1
    if step >= len(QS):
        ctx.user_data.pop("story", None)
        o = default_opts()
        o.update(st["opts"])
        await q.edit_message_text(
            f"✅ Vishay: {st['topic']}\nBhasha: {o['lang']} | Prakar: {o['genre']} | Parts: {o['scenes']} | "
            f"Visual: {o['visual']} | Awaaz: {o['voice']}"
            + (f"\nAapka sandesh: {st['msg']}" if st.get("msg") else "")
            + "\n\nKahani ban rahi hai, thoda time lagega...")
        t = asyncio.create_task(run_story(ctx.bot, q.message.chat_id, st["topic"], o, st.get("msg", "")))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    else:
        await q.edit_message_text(f"📖 Vishay: {st['topic']}\n\n{step + 1}/{len(QS)}  {QS[step][1]}",
                                  reply_markup=q_keyboard(step))


# ---------------- Photo effects ----------------
def fx_keyboard():
    btns = [InlineKeyboardButton(FX_LABEL[n], callback_data=f"fx:{n}") for n in FX]
    return InlineKeyboardMarkup([btns[i:i + 3] for i in range(0, len(btns), 3)])


async def on_photo(update: Update, ctx):
    if not allowed(update):
        return
    m = update.message
    fid = m.photo[-1].file_id
    ctx.user_data["last_photo"] = fid
    cap = (m.caption or "").strip()
    low = cap.lower()
    try:
        first = low.split()[0] if low.split() else ""
        if first == "meme":
            parts = [p.strip() for p in cap[4:].strip(" :").split("|")]
            top, bottom = (parts + ["", ""])[:2]
            img = await download_photo(ctx.bot, fid)
            return await m.reply_photo(jpg_bytes(await asyncio.to_thread(meme_image, img, top, bottom)))
        if first in FX:
            img = await download_photo(ctx.bot, fid)
            return await m.reply_photo(jpg_bytes(await asyncio.to_thread(apply_fx, img, first)))
        await m.reply_text("🎨 Kaunsa effect lagaun? (Meme ke liye photo bhejte waqt caption me "
                           "'meme upar text | neeche text' likhein)", reply_markup=fx_keyboard())
    except Exception as e:  # noqa
        log.exception("photo")
        await m.reply_text(f"Error: {e}")


async def fx_cb(update: Update, ctx):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("Allowed nahi", show_alert=True)
    fid = ctx.user_data.get("last_photo")
    if not fid:
        return await q.answer("Pehle photo bhejein", show_alert=True)
    await q.answer("Effect lag raha hai...")
    try:
        img = await download_photo(ctx.bot, fid)
        out = await asyncio.to_thread(apply_fx, img, q.data.split(":", 1)[1])
        await ctx.bot.send_photo(q.message.chat_id, jpg_bytes(out))
    except Exception as e:  # noqa
        log.exception("fx")
        await ctx.bot.send_message(q.message.chat_id, f"Error: {e}")


# ---------------- Chat + voice + seedhe likhne wale intents ----------------
async def chat_reply(update: Update, ctx, text):
    cid, user = update.effective_chat.id, update.effective_user
    await update.message.chat.send_action("typing")
    mem = None
    try:
        mem = await get_mem(user.id, user.first_name or "")
        h = await store.get(f"h:{cid}", [])
    except Exception as e:  # noqa
        log.warning("memory read fail: %s", e)
        h = history.setdefault(cid, [])
    system = str(cfg("CHAT_PROMPT") or DEFAULT_PROMPT).replace("{BOT_NAME}", str(cfg("BOT_NAME")))
    if mem is not None:
        system += memory_block(mem)
    out = await ask(system, text, hist=h[-16:])
    h += [{"role": "user", "content": text[:2000]}, {"role": "assistant", "content": out[:2000]}]
    del h[:-60]
    if mem is not None:
        try:
            await store.set(f"h:{cid}", h)
        except Exception as e:  # noqa
            log.warning("history save fail: %s", e)
        counters[user.id] = counters.get(user.id, 0) + 1
        if counters[user.id] % 5 == 0 or len(h) > 24:
            t = asyncio.create_task(learn(user.id, cid))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    await say(ctx.bot, cid, out)
    if ctx.user_data.get("voice"):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.mp3")
            await tts(out, "hindi" if has_deva(out) else "english", "male", p)
            with open(p, "rb") as f:
                await update.message.reply_audio(f, title="Jawab")


async def extract_city(text):
    try:
        c = (await ask("Is sentence me jis shehar ka mausam pucha gaya hai uska naam English me do. "
                       "Sirf shehar ka naam, kuchh aur nahi. Shehar na ho to NONE.", text,
                       temperature=0, max_tokens=40)).strip().strip('."')
        return "" if c.upper().startswith("NONE") else c
    except Exception:  # noqa
        return ""


async def route_text(update, ctx, text):
    """Seedhe likhe sawal ko sahi feature par bhejta hai. True = sambhal liya."""
    t, low = text.strip(), text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*([a-z]{3})\s*(?:to|in|ko|se)\s*([a-z]{3})\b", low)
    if m and len(low) < 60:
        ctx.user_data["_arg"] = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        await cmd_currency(update, ctx)
        return True
    if re.search(r"\bqr\b", low):
        arg = re.sub(r"(?i)\bqr\b|\bcode\b|banao|bana do|bana|generate|create|bhejo|ke liye|\bka\b|\bki\b", " ", t)
        arg = re.sub(r"\s+", " ", arg).strip()
        if arg:
            ctx.user_data["_arg"] = arg
            await cmd_qr(update, ctx)
            return True
    if re.search(r"(mausam|मौसम|weather|barish|बारिश)", low):
        ctx.user_data["_arg"] = await extract_city(t)
        await cmd_weather(update, ctx)
        return True
    if re.search(r"(kahani|कहानी|\bstory\b)", low) and re.search(r"(bana|likh|sun|chahiye|generate|create|बना|लिख|सुना|चाहिए)", low):
        ctx.user_data["_arg"] = t
        await cmd_story(update, ctx)
        return True
    return False


async def on_text(update: Update, ctx):
    if not allowed(update):
        return
    try:
        if await route_text(update, ctx, update.message.text):
            return
        await chat_reply(update, ctx, update.message.text)
    except Exception as e:  # noqa
        log.exception("chat")
        await update.message.reply_text(f"Error: {e}")


async def on_voice(update: Update, ctx):
    if not allowed(update):
        return
    try:
        m = update.message
        att = m.voice or m.audio
        f = await att.get_file()
        data = bytes(await f.download_as_bytearray())
        text = await transcribe(data, "voice.ogg" if m.voice else (att.file_name or "audio.mp3"))
        if not text:
            return await m.reply_text("Awaaz samajh nahi aayi, dobara bolein.")
        await m.reply_text(f"🎤 {text}")
        if not await route_text(update, ctx, text):
            await chat_reply(update, ctx, text)
    except Exception as e:  # noqa
        log.exception("voice")
        await update.message.reply_text(f"Error: {e}")


COMMANDS = [("story", "Sawal + poori kahani video"), ("video", "Smooth code animation video"),
            ("wish", "Animated wish video"), ("img", "Vector image"), ("photo", "Real photo search"),
            ("weather", "Mausam graphic card"), ("quote", "Quote poster"), ("meme", "Meme banao"),
            ("chart", "Bar chart"), ("qr", "QR code"), ("wifi", "WiFi QR"), ("music", "Code se music"),
            ("tts", "Text se awaaz"), ("calc", "Calculator"), ("convert", "Unit converter"),
            ("currency", "Currency rate"), ("emi", "Loan EMI"), ("age", "Umar calculator"),
            ("news", "Taaza khabrein"), ("wiki", "Wikipedia"), ("time", "Kisi shehar ka samay"),
            ("summarize", "Link/text ka saaransh"), ("translate", "Anuvaad"), ("pdf", "Text se PDF"),
            ("remind", "Yaad dilao"), ("note", "Note likho"), ("notes", "Mere notes"),
            ("pass", "Strong password"), ("pick", "Chun do"), ("dice", "Pasa"), ("coin", "Sikka"),
            ("shayari", "Shayari"), ("joke", "Joke"), ("fact", "Dilchasp fact"), ("code", "Poora code"),
            ("remember", "Kuchh yaad rakhwao"), ("memory", "Bot ki yaaddasht dekho"),
            ("forget", "Yaaddasht se hatao"), ("voice", "Jawab awaaz me on/off"), ("diag", "Jaanch"), ("reset", "Baatcheet reset"),
            ("id", "Meri Telegram ID")]


class BotManager:
    """Telegram bot ko website se start/stop/restart karta hai. Galat token par crash nahi hota."""

    def __init__(self):
        self.app = None
        self.status = "Band"
        self.username = ""

    async def stop(self):
        app, self.app = self.app, None
        if app:
            for step in (app.updater.stop, app.stop, app.shutdown):
                try:
                    await step()
                except Exception:  # noqa
                    pass
        self.status, self.username = "Band", ""

    async def start(self):
        await self.stop()
        token = str(cfg("TELEGRAM_TOKEN")).strip()
        if not token:
            self.status = "Telegram token abhi nahi daala gaya"
            return
        app = Application.builder().token(token).build()
        handlers = {
            "start": cmd_start, "help": cmd_start, "id": cmd_id, "reset": cmd_reset, "voice": cmd_voice,
            "diag": cmd_diag, "img": cmd_img, "photo": cmd_photo, "video": cmd_video, "wish": cmd_wish,
            "weather": cmd_weather, "time": cmd_time, "chart": cmd_chart, "quote": cmd_quote, "meme": cmd_meme,
            "qr": cmd_qr, "wifi": cmd_wifi, "tts": cmd_tts, "music": cmd_music, "translate": cmd_translate,
            "code": cmd_code, "calc": cmd_calc, "convert": cmd_convert, "currency": cmd_currency, "emi": cmd_emi,
            "age": cmd_age, "pass": cmd_pass, "pick": cmd_pick, "dice": cmd_dice, "coin": cmd_coin,
            "news": cmd_news, "wiki": cmd_wiki, "summarize": cmd_summarize, "remind": cmd_remind,
            "note": cmd_note, "notes": cmd_notes, "delnote": cmd_delnote, "pdf": cmd_pdf,
            "shayari": cmd_shayari, "joke": cmd_joke, "fact": cmd_fact, "story": cmd_story,
            "remember": cmd_remember, "memory": cmd_memory, "forget": cmd_forget}
        for name, fn in handlers.items():
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(story_cb, pattern=r"^sq:"))
        app.add_handler(CallbackQueryHandler(fx_cb, pattern=r"^fx:"))
        app.add_handler(MessageHandler(filters.PHOTO, on_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        try:
            await app.initialize()
            await app.bot.set_my_commands(COMMANDS)
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            self.app, self.username, self.status = app, app.bot.username, "Chalu"
            log.info("Bot chalu: @%s", self.username)
            asyncio.create_task(reschedule_reminders())
        except InvalidToken:
            self.status = "Token galat hai. BotFather se dobara copy karke daalein."
            log.warning(self.status)
        except Exception as e:  # noqa
            self.status = f"Error: {type(e).__name__}: {str(e).replace(token, '***')}"
            log.warning(self.status)
            try:
                await app.shutdown()
            except Exception:  # noqa
                pass


manager = BotManager()


# =====================================================================
#  WEBSITE: /health  +  /admin (login + settings form)
# =====================================================================
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot Admin</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f4f7;margin:0;padding:16px;color:#222}
.card{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:18px;box-shadow:0 1px 4px #0002}
h1{font-size:22px;margin:0 0 12px} label{display:block;font-weight:600;margin:14px 0 4px}
input,textarea{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #bbb;border-radius:8px}
small{color:#666;display:block;margin-top:3px} button{padding:12px 18px;font-size:16px;border:0;border-radius:8px;
background:#4f46e5;color:#fff;margin-top:18px;margin-right:8px} button.g{background:#666}
.st{padding:10px;border-radius:8px;background:#eef;margin-bottom:8px} .ok{background:#e6f7ea}.bad{background:#fdeaea}
.msg{background:#fff7d6;padding:10px;border-radius:8px;margin-bottom:10px}
</style></head><body><div class="card">{{BODY}}</div></body></html>"""


def sign():
    return hmac.new(ADMIN_PW.encode(), b"admin-session", hashlib.sha256).hexdigest()


def authed(request):
    return hmac.compare_digest(request.cookies.get("adm", ""), sign())


def page(body):
    return web.Response(text=PAGE.replace("{{BODY}}", body), content_type="text/html")


def login_html(msg=""):
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    return (f'<h1>Admin Login</h1>{m}<form method="post" action="/login">'
            '<label>Admin Password</label><input type="password" name="pw" autofocus>'
            '<button>Login</button></form>')


def form_html(msg="", models=None):
    ok = manager.status == "Chalu"
    who = f' - <a href="https://t.me/{manager.username}">@{manager.username}</a>' if ok else ""
    groq_ok = bool(str(cfg("GROQ_API_KEY")).strip())
    photo_src = "Pexels/Pixabay/Openverse" if (str(cfg("PEXELS_KEY")).strip() or str(cfg("PIXABAY_KEY")).strip()) else "Openverse (bina key)"
    models_html = ('<div class="st">Available models: ' + html.escape(", ".join(models)) + '</div>') if models else ""
    out = f'<h1>{html.escape(str(cfg("BOT_NAME")))} - Settings</h1>'
    if msg:
        out += f'<div class="msg">{html.escape(msg)}</div>'
    out += (f'<div class="st {"ok" if ok else "bad"}">Telegram bot: {html.escape(manager.status)}{who}</div>'
            f'<div class="st {"ok" if groq_ok else "bad"}">Groq key: {"set hai" if groq_ok else "set nahi hai"}</div>'
            f'<div class="st">Photo search: {photo_src}</div>'
            f'<div class="st {"ok" if store.persistent else "bad"}">Yaaddasht: {html.escape(store.describe())}</div>'
            + models_html +
            '<form method="post" action="/save">')
    for key, label, default, kind, hint in FIELDS:
        cur = str(cfg(key))
        if kind == "secret":
            ph = f"saved (......{cur[-4:]}). Badalna ho tabhi likhein" if cur else "yahan paste karein"
            inp = f'<input type="password" name="{key}" placeholder="{html.escape(ph)}" autocomplete="off">'
        elif kind == "area":
            inp = f'<textarea name="{key}" rows="5">{html.escape(cur)}</textarea>'
        else:
            inp = f'<input name="{key}" value="{html.escape(cur)}">'
        out += f"<label>{html.escape(label)}</label>{inp}" + (f"<small>{html.escape(hint)}</small>" if hint else "")
    out += ('<button>Save</button></form><form method="post" action="/restart" style="display:inline">'
            '<button class="g">Bot restart</button></form>'
            '<form method="post" action="/logout" style="display:inline"><button class="g">Logout</button></form>')
    return out


def redirect(msg=""):
    raise web.HTTPSeeOther("/admin" + (f"?msg={quote(msg)}" if msg else ""))


async def persist_to_render(updates):
    """Optional: RENDER_API_KEY ho to settings Render ke Environment me bhi save hoti hain,
    taaki restart ke baad bhi bani rahein."""
    key, sid = os.getenv("RENDER_API_KEY"), os.getenv("RENDER_SERVICE_ID")
    if not key or not sid:
        return
    async with httpx.AsyncClient(timeout=20) as c:
        for k, v in updates.items():
            if not v:
                continue
            try:
                r = await c.put(f"https://api.render.com/v1/services/{sid}/env-vars/{k}",
                                headers={"Authorization": f"Bearer {key}"}, json={"value": v})
                log.info("Render env %s: %s", k, r.status_code)
            except Exception as e:  # noqa
                log.warning("Render env save fail: %s", e)


async def h_health(request):
    return web.json_response({"status": "ok", "bot": manager.status})


async def h_home(request):
    return page(f'<h1>{html.escape(str(cfg("BOT_NAME")))}</h1>'
                f'<div class="st">Bot: {html.escape(manager.status)}</div>'
                '<p><a href="/admin">Admin settings</a></p>')


async def h_admin(request):
    if not authed(request):
        return page(login_html())
    models = await list_models()
    return page(form_html(request.query.get("msg", ""), models))


async def h_login(request):
    data = await request.post()
    if hmac.compare_digest(str(data.get("pw", "")), ADMIN_PW):
        resp = web.HTTPSeeOther("/admin")
        resp.set_cookie("adm", sign(), httponly=True, secure=True, samesite="Strict", max_age=7 * 86400)
        raise resp
    await asyncio.sleep(1.5)
    return page(login_html("Password galat hai"))


async def h_logout(request):
    resp = web.HTTPSeeOther("/admin")
    resp.del_cookie("adm")
    raise resp


async def h_save(request):
    if not authed(request):
        raise web.HTTPSeeOther("/admin")
    data = await request.post()
    updates = {}
    for key, label, default, kind, hint in FIELDS:
        v = str(data.get(key, "")).strip()
        if kind == "secret" and not v:
            continue  # khaali = purani value rakho
        if v != str(cfg(key)):
            updates[key] = v
    _file.update(updates)
    save_file()
    if "UPSTASH_URL" in updates or "UPSTASH_TOKEN" in updates:
        store.clear_cache()
        await load_settings_from_db()
    await save_settings_to_db()
    if "TELEGRAM_TOKEN" in updates:
        await manager.start()
    asyncio.create_task(persist_to_render(updates))
    redirect("Save ho gaya" + (" - bot restart hua" if "TELEGRAM_TOKEN" in updates else ""))


async def h_restart(request):
    if not authed(request):
        raise web.HTTPSeeOther("/admin")
    await manager.start()
    redirect("Bot restart ho gaya")


async def keepalive():
    if not SELF_URL:
        return
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            await asyncio.sleep(600)
            try:
                await c.get(f"{SELF_URL}/health")
            except Exception as e:  # noqa
                log.warning("ping fail: %s", e)


async def main():
    load_file()
    await load_settings_from_db()
    web_app = web.Application()
    web_app.add_routes([
        web.get("/", h_home), web.get("/health", h_health), web.get("/admin", h_admin),
        web.post("/login", h_login), web.post("/logout", h_logout),
        web.post("/save", h_save), web.post("/restart", h_restart)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Website port %s par chal rahi hai (/admin, /health)", PORT)
    await manager.start()  # token galat ho tab bhi website chalti rehti hai
    asyncio.create_task(keepalive())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
