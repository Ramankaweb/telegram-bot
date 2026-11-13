import asyncio
import glob
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import random
import re
import resource
import secrets
import subprocess
import sys
import tempfile
import wave
from datetime import datetime
from urllib.parse import quote

import cairosvg
import httpx
import imageio
import imageio_ffmpeg
import numpy as np
from aiohttp import web
from groq import AsyncGroq
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
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
    ("GROQ_MODEL", "Groq Model", "openai/gpt-oss-120b", "text", "Jaise openai/gpt-oss-120b ya llama-3.3-70b-versatile"),
    ("WHISPER_MODEL", "Voice-to-text model", "whisper-large-v3-turbo", "text", "Voice message samajhne ke liye"),
    ("ALLOWED_IDS", "Allowed Telegram IDs", "", "text", "Comma se alag. Khaali = sab use kar sakte hain. Apni ID bot ko /id bhej kar milegi"),
    ("BOT_NAME", "Bot ka naam", "JARVIS", "text", ""),
    ("CHAT_PROMPT", "Bot ka behaviour (system prompt)", DEFAULT_PROMPT, "area", "{BOT_NAME} likhne par bot ka naam aa jata hai"),
    ("DEFAULT_LANG", "Default bhasha (hindi ya english)", "hindi", "text", "Kahani, mausam card aur awaaz ke liye"),
    ("DEFAULT_CITY", "Default shehar (/weather)", "Delhi", "text", ""),
    ("PEXELS_KEY", "Pexels API Key (optional)", "", "secret", "pexels.com/api se free. Photo search aur achhi hoti hai"),
    ("PIXABAY_KEY", "Pixabay API Key (optional)", "", "secret", "pixabay.com/api/docs se free"),
    ("MUSIC_VOLUME", "Background music volume (%)", "14", "text", "0 se 100"),
    ("SEND_PARTS", "Kahani ka har part alag bhejein (1 ya 0)", "1", "text", ""),
    ("VIDEO_W", "Video width", "640", "text", "Max 1280. Free Render par chhota rakhna behtar"),
    ("VIDEO_H", "Video height", "360", "text", "Max 720"),
    ("VIDEO_FPS", "Video FPS", "10", "text", "Max 24"),
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
def fps(): return num("VIDEO_FPS", 24)
def lang_default(): return "english" if str(cfg("DEFAULT_LANG")).strip().lower().startswith("e") else "hindi"


def allowed_ids():
    return {int(x) for x in re.findall(r"\d+", str(cfg("ALLOWED_IDS")))}


# =====================================================================
#  GROQ (chat + voice-to-text)
# =====================================================================
_groq = (None, None)
heavy = asyncio.Semaphore(1)  # free server par ek time me ek bhaari kaam
history: dict[int, list] = {}
tasks: set = set()


def get_groq():
    global _groq
    key = str(cfg("GROQ_API_KEY")).strip()
    if not key:
        raise RuntimeError("Groq API key abhi set nahi hai. Website ke /admin page par daalein.")
    if _groq[0] != key:
        _groq = (key, AsyncGroq(api_key=key))
    return _groq[1]


async def ask(system, user, hist=None, temperature=0.7, max_tokens=6000):
    model = cfg("GROQ_MODEL") or DEF["GROQ_MODEL"]
    msgs = [{"role": "system", "content": system}] + (hist or []) + [{"role": "user", "content": user}]
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else None
    for attempt in range(4):
        try:
            r = await get_groq().chat.completions.create(
                model=model, messages=msgs, temperature=temperature,
                max_tokens=max_tokens, extra_body=extra)
            out = r.choices[0].message.content or ""
            return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
        except Exception as e:  # noqa
            s = str(e).lower()
            if extra and "reasoning" in s:
                extra = None
                continue
            if attempt < 3 and ("429" in s or "rate" in s):
                await asyncio.sleep(15 * (attempt + 1))
                continue
            raise


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
#  AUDIO: ffmpeg, music (code se), voice (TTS)
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
    return MOOD_ALIAS.get(m, m) if MOOD_ALIAS.get(m, m) in MOODS else "calm"


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


def add_music_to_video(video, mood, out):
    dur = media_duration(video)
    wav = video + ".wav"
    synth_music(mood, dur + 1, wav)
    vol = num("MUSIC_VOLUME", 100, 0) / 100.0 * 2.2
    ff("-i", video, "-i", wav, "-filter_complex", f"[1:a]volume={min(vol, 1.0)}[a]",
       "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-shortest",
       "-movflags", "+faststart", out)


VOICES = {("hindi", "male"): "hi-IN-MadhurNeural", ("hindi", "female"): "hi-IN-SwaraNeural",
          ("english", "male"): "en-IN-PrabhatNeural", ("english", "female"): "en-IN-NeerjaNeural"}


def clean_tts(text):
    text = re.sub(r"[*_#`~>\[\]{}|\\]", " ", text)
    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def tts(text, lang, gender, path):
    text = clean_tts(text)[:3000]
    if not text:
        raise RuntimeError("Bolne ke liye text khaali hai")
    try:
        import edge_tts
        await asyncio.wait_for(edge_tts.Communicate(text, VOICES[(lang, gender)]).save(path), 90)
        if os.path.getsize(path) < 500:
            raise RuntimeError("khaali awaaz")
    except Exception as e:  # noqa
        log.warning("edge-tts fail (%s), gTTS try", e)
        from gtts import gTTS
        await asyncio.to_thread(lambda: gTTS(text, lang="hi" if lang == "hindi" else "en").save(path))


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
#  SANDBOX: LLM ka animation code (/video)
# =====================================================================
VIDEO_SYS = ("Tum Python animator ho. Sirf ek ```python code block do. Code me ek function define karo: "
             "def frame(t): jahan t 0.0 se 1.0 tak hai, aur vo ek complete SVG string return kare "
             'jiska root <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720"> ho. '
             "Chizein t ke hisab se move/rotate/fade/color-change hon (smooth animation), gradients aur layers use karo. "
             "Sirf `import math` aur `import random` allowed hai. File, network, os, open, eval sab mana hai. "
             "Frame function tez ho (200 se kam shapes).")
RUNNER = '''
import sys, json, math, random
code = open(sys.argv[1]).read()
n = int(sys.argv[2])
g = {"math": math, "random": random}
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
        p = subprocess.run([sys.executable, rp, cp, str(n), op], capture_output=True,
                           text=True, timeout=40, preexec_fn=limits, cwd=d)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-300:])
        return json.load(open(op))


def open_writer(path):
    return imageio.get_writer(path, fps=fps(), codec="libx264", pixelformat="yuv420p",
                              macro_block_size=None, quality=None,
                              ffmpeg_params=["-preset", "veryfast", "-crf", "27"])


def build_anim(frames, path):
    W, H = vw(), vh()
    with open_writer(path) as w:
        for s in frames:
            im = Image.open(io.BytesIO(svg_png(s, W))).convert("RGB").resize((W, H))
            w.append_data(np.asarray(im))


# =====================================================================
#  SCENE VIDEO: Ken-Burns zoom/pan + Hindi subtitles
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


def build_scene_video(base, sents, dur, path, idx):
    W, H, F = vw(), vh(), fps()
    n = max(2, int(dur * F))
    BW, BH = base.size
    layers = [caption_layer(s, W, H) for s in sents]
    lens = np.cumsum([max(1, len(s)) for s in sents]) / sum(max(1, len(s)) for s in sents) * n
    zin, direction = idx % 2 == 0, 1 if idx % 3 else -1
    fade = max(1, int(0.3 * F))
    black = Image.new("RGB", (W, H))
    k = 0
    with open_writer(path) as w:
        for f in range(n):
            p = f / (n - 1)
            z = 1.0 + 0.3 * (p if zin else 1 - p)
            vw_, vh_ = BW / z, BH / z
            x0 = (BW - vw_) / 2 + (p - 0.5) * (BW - vw_) * 0.8 * direction
            y0 = (BH - vh_) / 2 + (p - 0.5) * (BH - vh_) * 0.3
            x0 = min(max(0, x0), BW - vw_)
            y0 = min(max(0, y0), BH - vh_)
            fr = base.resize((W, H), RS.BILINEAR, box=(x0, y0, x0 + vw_, y0 + vh_))
            while k < len(sents) - 1 and f >= lens[k]:
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
#  STORY: sawal -> kahani (JSON) -> photo/vector + awaaz + music -> video
# =====================================================================
QS = [
    ("lang", "Kahani kis bhasha me chahiye?", [("Hindi", "hindi"), ("English", "english")]),
    ("genre", "Kaisi kahani chahiye?", [("Naitik/seekh", "moral"), ("Adventure", "adventure"), ("Hasya", "funny"),
                                         ("Rahasya", "mystery"), ("Bhavuk", "emotional"), ("Sci-fi", "sci-fi")]),
    ("scenes", "Kitne parts (scenes) ki?", [("3", "3"), ("5", "5"), ("7", "7")]),
    ("visual", "Visuals kaise chahiye?", [("Real photos", "photo"), ("Vector art", "vector"), ("Dono mix", "mix")]),
    ("voice", "Kiski awaaz?", [("Purush", "male"), ("Mahila", "female")]),
]


def default_opts():
    return {"lang": lang_default(), "genre": "moral", "scenes": "5", "visual": "mix", "voice": "male"}


def q_keyboard(step):
    key, _, opts = QS[step]
    btns = [InlineKeyboardButton(lab, callback_data=f"sq:{step}:{val}") for lab, val in opts]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton("⚡ Baaki default se shuru karo", callback_data=f"sq:{step}:fast")])
    return InlineKeyboardMarkup(rows)


async def write_story(topic, o):
    n = int(o["scenes"])
    lang_txt = "Hindi (Devanagari script me, saral bolchaal wali)" if o["lang"] == "hindi" else "simple English"
    system = (
        "Tum behtareen kahani-lekhak ho. Sirf valid JSON do, aur kuchh nahi. Format: "
        '{"title": "...", "mood": "calm|happy|sad|tense|epic|mystery", "scenes": [{"narration": "...", '
        '"image_query": "...", "mood": "calm|happy|sad|tense|epic|mystery"}]}. '
        f"Rules: theek {n} scenes; kahani ka prakar: {o['genre']}; narration ki bhasha: {lang_txt}; "
        "har scene me 2-3 chhote vaakya (30-45 shabd) jo awaaz me padhne layak hon; emoji, star, bracket mat likho; "
        "image_query English me 3-6 shabd ka ho aur kisi concrete drishya ka (jaise 'old man walking village road sunset'), "
        "naam ya abstract baat ka nahi; kahani ka shuru, madhya aur ant saaf ho, ant me seekh ya twist ho.")
    last = None
    for _ in range(2):
        try:
            data = parse_json(await ask(system, f"Kahani ka vishay: {topic}", temperature=0.9, max_tokens=5000))
            sc = [s for s in data.get("scenes", []) if str(s.get("narration", "")).strip()]
            if sc:
                data["scenes"] = sc[:n]
                data.setdefault("title", topic)
                return data
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"Kahani nahi ban payi: {last}")


async def scene_visual(sc, i, o, topic):
    """(PIL image, credit). visual: photo / vector / mix."""
    W, H = int(vw() * 1.3), int(vh() * 1.3)
    q = sc.get("image_query") or topic
    mode = o["visual"]
    want_photo = mode == "photo" or (mode == "mix" and i % 2 == 1)
    if want_photo:
        try:
            got = await get_photos(q, 1)
            if got:
                return cover(got[0][0], W, H), got[0][1]
        except Exception as e:  # noqa
            log.info("photo fail: %s", e)
    try:
        prompt = f"Drishya: {q}. Kahani ka hissa: {sc['narration'][:220]}"
        out = await ask(SVG_SYS, prompt, temperature=0.8, max_tokens=5000)
        png = await asyncio.to_thread(svg_png, get_svg(out), W)
        return cover(Image.open(io.BytesIO(png)).convert("RGB"), W, H), ""
    except Exception as e:  # noqa
        log.info("vector fail: %s", e)
    c = MOOD_COLORS[fix_mood(sc.get("mood"))]
    return gradient(W, H, c[0], c[1]), ""


async def make_scene(d, i, sc, o, story_mood, topic):
    voice = os.path.join(d, f"v{i}.mp3")
    await tts(sc["narration"], o["lang"], o["voice"], voice)
    dur = await asyncio.to_thread(media_duration, voice) + 0.8
    base, credit = await scene_visual(sc, i, o, topic)
    wav, aud, silent, clip = (os.path.join(d, f"{n}{i}") for n in ("m", "a", "s", "c"))
    wav, aud, silent, clip = wav + ".wav", aud + ".m4a", silent + ".mp4", clip + ".mp4"
    await asyncio.to_thread(synth_music, fix_mood(sc.get("mood") or story_mood), dur + 0.5, wav)
    await asyncio.to_thread(mix_audio, voice, wav, aud)
    await asyncio.to_thread(build_scene_video, base, split_sentences(sc["narration"]), dur, silent, i)
    await asyncio.to_thread(ff, "-i", silent, "-i", aud, "-c:v", "copy", "-c:a", "copy", "-shortest", clip)
    return clip, credit


async def say(bot, cid, text):
    for i in range(0, len(text), 4000):
        await bot.send_message(cid, text[i:i + 4000])


async def send_video_file(bot, cid, path, caption=None):
    with open(path, "rb") as f:
        await bot.send_video(cid, f, caption=caption, supports_streaming=True, width=vw(), height=vh(),
                             read_timeout=300, write_timeout=300)


async def run_story(bot, cid, topic, o):
    status = None
    try:
        async with heavy:
            status = await bot.send_message(cid, "✍️ Kahani likh raha hun...")
            data = await write_story(topic, o)
            sc = data["scenes"]
            await say(bot, cid, f"📖 {data['title']}\n\n" + "\n\n".join(f"{i}. {s['narration']}" for i, s in enumerate(sc, 1)))
            clips, credits = [], []
            with tempfile.TemporaryDirectory() as d:
                for i, s in enumerate(sc, 1):
                    await status.edit_text(f"🎬 Part {i}/{len(sc)} ban raha hai (photo + awaaz + music)...")
                    clip, credit = await make_scene(d, i, s, o, data.get("mood"), topic)
                    clips.append(clip)
                    if credit:
                        credits.append(credit)
                    if str(cfg("SEND_PARTS")).strip() in ("1", "yes", "haan", "true"):
                        await send_video_file(bot, cid, clip, f"Part {i}/{len(sc)}")
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
    t = " ".join(ctx.args).strip()
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
    "🎬 /story <vishay> - sawal puchh kar poori kahani: photo/vector scenes + Hindi awaaz + music, parts me aur joda hua video\n"
    "🖼️ /photo <shabd> - real photo dhundh kar bhejta hun (credit ke saath)\n"
    "🎨 /img <vivaran> - code se vector image (PNG + SVG)\n"
    "📹 /video <vivaran> - code se animation video + music\n"
    "🌦️ /weather <shehar> - mausam ka graphic card\n"
    "🗣️ /tts <text> - text ko awaaz me\n"
    "🎵 /music <mood> [second] - code se music (calm, happy, sad, tense, epic, mystery)\n"
    "📊 /chart Title: A=10, B=20 - bar chart image\n"
    "💬 /quote <vishay ya line> - photo wala quote poster\n"
    "🔳 /qr <text ya link> - QR code\n"
    "🌐 /translate hindi: <text> - anuvaad\n"
    "💻 /code <kaam> - poora code file me\n"
    "🔊 /voice on|off - jawab awaaz me bhi\n"
    "🎤 Voice message bhejein to main samajh kar jawab dunga\n"
    "/id - aapki Telegram ID, /reset - baatcheet reset\n\n"
    "Koi bhi sawal seedha likh dein.")


async def cmd_start(update: Update, ctx):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    await update.message.reply_text(HELP.format(name=cfg("BOT_NAME")))


async def cmd_id(update: Update, ctx):
    await update.message.reply_text(f"Aapki ID: {update.effective_user.id}")


async def cmd_reset(update: Update, ctx):
    history.pop(update.effective_chat.id, None)
    await update.message.reply_text("Baatcheet reset ho gayi.")


async def cmd_voice(update: Update, ctx):
    if not allowed(update):
        return
    on = (ctx.args[0].lower() if ctx.args else "") in ("on", "haan", "1")
    ctx.user_data["voice"] = on
    await update.message.reply_text("Ab jawab awaaz me bhi aayenge 🔊" if on else "Awaaz wale jawab band.")


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


@job("Video ban rahi hai (1-2 minute lag sakte hain)...", True)
async def cmd_video(update, ctx):
    prompt = arg_text(update, ctx)
    if not prompt:
        raise Usage("Aise likhein: /video ghoomta hua grah aur taare")
    n = fps() * num("VIDEO_SECONDS", 15)
    frames, last = None, None
    for _ in range(3):
        try:
            out = await ask(VIDEO_SYS, prompt, temperature=0.7, max_tokens=4000)
            m = re.search(r"```(?:python)?\n(.*?)```", out, re.S)
            frames = await asyncio.to_thread(run_frames, m.group(1) if m else out, n)
            await asyncio.to_thread(svg_png, frames[0], 100)  # test render
            break
        except Exception as e:  # noqa
            last, frames = e, None
    if not frames:
        raise RuntimeError(f"Animation code nahi chala: {last}")
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.mp4"), os.path.join(d, "b.mp4")
        await asyncio.to_thread(build_anim, frames, a)
        try:
            await asyncio.to_thread(add_music_to_video, a, "calm", b)
        except Exception as e:  # noqa
            log.warning("music add fail: %s", e)
            b = a
        with open(b, "rb") as f:
            await update.message.reply_video(f, supports_streaming=True, read_timeout=180, write_timeout=180)


@job("Mausam dekh raha hun...")
async def cmd_weather(update, ctx):
    city = arg_text(update, ctx) or str(cfg("DEFAULT_CITY"))
    info = await fetch_weather(city)
    lang = lang_default()
    im = await asyncio.to_thread(weather_card, info, lang)
    png = png_bytes(im)
    cur = info["cur"]
    cap = (f"{info['city']}: {round(cur['temperature_2m'])}°C, {LABEL[wmo(cur['weather_code'])][1 if lang == 'hindi' else 0]}")
    await update.message.reply_photo(png, caption=cap)
    await update.message.reply_document(io.BytesIO(png), filename="weather.png")


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
                    topic, temperature=0.8, max_tokens=500)
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


@job("QR ban raha hai...")
async def cmd_qr(update, ctx):
    t = arg_text(update, ctx)
    if not t:
        raise Usage("Aise likhein: /qr https://example.com")
    import qrcode
    im = qrcode.make(t).convert("RGB").resize((720, 720), RS.NEAREST)
    await update.message.reply_photo(png_bytes(im))


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
    out = await ask(f"Neeche ke text ka {lang.strip()} me anuvaad karo. Sirf anuvaad do.", text.strip(), temperature=0.2)
    await update.message.reply_text(out)


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


async def cmd_story(update: Update, ctx):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    topic = arg_text(update, ctx)
    if not topic:
        return await update.message.reply_text("Aise likhein: /story ek chhota robot jo dost dhundta hai")
    ctx.user_data["story"] = {"topic": topic, "opts": {}}
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
            f"Visual: {o['visual']} | Awaaz: {o['voice']}\n\nKahani ban rahi hai, thoda time lagega...")
        t = asyncio.create_task(run_story(ctx.bot, q.message.chat_id, st["topic"], o))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    else:
        await q.edit_message_text(f"📖 Vishay: {st['topic']}\n\n{step + 1}/{len(QS)}  {QS[step][1]}",
                                  reply_markup=q_keyboard(step))


async def chat_reply(update: Update, ctx, text):
    cid = update.effective_chat.id
    h = history.setdefault(cid, [])
    await update.message.chat.send_action("typing")
    system = str(cfg("CHAT_PROMPT") or DEFAULT_PROMPT).replace("{BOT_NAME}", str(cfg("BOT_NAME")))
    out = await ask(system, text, hist=h[-10:])
    h += [{"role": "user", "content": text}, {"role": "assistant", "content": out}]
    del h[:-20]
    await say(ctx.bot, cid, out)
    if ctx.user_data.get("voice"):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.mp3")
            await tts(out, "hindi" if has_deva(out) else "english", "male", p)
            with open(p, "rb") as f:
                await update.message.reply_audio(f, title="Jawab")


async def on_text(update: Update, ctx):
    if not allowed(update):
        return
    try:
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
        await chat_reply(update, ctx, text)
    except Exception as e:  # noqa
        log.exception("voice")
        await update.message.reply_text(f"Error: {e}")


COMMANDS = [("story", "Sawal + poori kahani video"), ("photo", "Real photo search"), ("img", "Code se vector image"),
            ("video", "Code se animation video"), ("weather", "Mausam graphic card"), ("tts", "Text se awaaz"),
            ("music", "Code se music"), ("chart", "Bar chart"), ("quote", "Quote poster"), ("qr", "QR code"),
            ("translate", "Anuvaad"), ("code", "Poora code likhwayein"), ("voice", "Jawab awaaz me on/off"),
            ("reset", "Baatcheet reset"), ("id", "Meri Telegram ID")]


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
        for name, fn in [("start", cmd_start), ("help", cmd_start), ("id", cmd_id), ("reset", cmd_reset),
                         ("voice", cmd_voice), ("img", cmd_img), ("photo", cmd_photo), ("video", cmd_video),
                         ("weather", cmd_weather), ("chart", cmd_chart), ("quote", cmd_quote), ("qr", cmd_qr),
                         ("tts", cmd_tts), ("music", cmd_music), ("translate", cmd_translate),
                         ("code", cmd_code), ("story", cmd_story)]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(story_cb, pattern=r"^sq:"))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        try:
            await app.initialize()
            await app.bot.set_my_commands(COMMANDS)
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            self.app, self.username, self.status = app, app.bot.username, "Chalu"
            log.info("Bot chalu: @%s", self.username)
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


def form_html(msg=""):
    ok = manager.status == "Chalu"
    who = f' - <a href="https://t.me/{manager.username}">@{manager.username}</a>' if ok else ""
    groq_ok = bool(str(cfg("GROQ_API_KEY")).strip())
    photo_src = "Pexels/Pixabay/Openverse" if (str(cfg("PEXELS_KEY")).strip() or str(cfg("PIXABAY_KEY")).strip()) else "Openverse (bina key)"
    out = f'<h1>{html.escape(str(cfg("BOT_NAME")))} - Settings</h1>'
    if msg:
        out += f'<div class="msg">{html.escape(msg)}</div>'
    out += (f'<div class="st {"ok" if ok else "bad"}">Telegram bot: {html.escape(manager.status)}{who}</div>'
            f'<div class="st {"ok" if groq_ok else "bad"}">Groq key: {"set hai" if groq_ok else "set nahi hai"}</div>'
            f'<div class="st">Photo search: {photo_src}</div>'
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
    return page(form_html(request.query.get("msg", "")))


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
