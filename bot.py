import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import resource
import secrets
import subprocess
import sys
import tempfile
import textwrap
from urllib.parse import quote

import cairosvg
import httpx
import imageio
import numpy as np
from aiohttp import web
from groq import AsyncGroq
from PIL import Image
from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx URL me bot token chhapta hai
log = logging.getLogger("bot")

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
    ("GROQ_MODEL", "Groq Model", "llama-3.3-70b-versatile", "text", "Jaise llama-3.3-70b-versatile ya llama-3.1-8b-instant"),
    ("ALLOWED_IDS", "Allowed Telegram IDs", "", "text", "Comma se alag. Khaali = sab use kar sakte hain. Apni ID bot ko /id bhej kar milegi"),
    ("BOT_NAME", "Bot ka naam", "JARVIS", "text", ""),
    ("CHAT_PROMPT", "Bot ka behaviour (system prompt)", DEFAULT_PROMPT, "area", "{BOT_NAME} likhne par bot ka naam aa jata hai"),
    ("VIDEO_W", "Video width", "960", "text", "Max 1280"),
    ("VIDEO_H", "Video height", "540", "text", "Max 720"),
    ("VIDEO_FPS", "Video FPS", "12", "text", "Max 24"),
    ("VIDEO_SECONDS", "/video ki lambai (second)", "6", "text", "Max 15"),
    ("STORY_SCENES", "/story me scenes", "4", "text", "Max 8"),
    ("SCENE_SECONDS", "Har scene kitne second", "4", "text", "Max 10"),
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


def num(key, cap):
    try:
        v = int(str(cfg(key)).strip())
    except Exception:  # noqa
        v = int(DEF[key])
    return max(1, min(v, cap))


def vw(): return num("VIDEO_W", 1280) // 2 * 2
def vh(): return num("VIDEO_H", 720) // 2 * 2
def fps(): return num("VIDEO_FPS", 24)


def allowed_ids():
    return {int(x) for x in re.findall(r"\d+", str(cfg("ALLOWED_IDS")))}


# ---------------- Groq ----------------
_groq = (None, None)
heavy = asyncio.Semaphore(1)  # free server par ek time me ek bhaari kaam
history: dict[int, list] = {}


def get_groq():
    global _groq
    key = str(cfg("GROQ_API_KEY")).strip()
    if not key:
        raise RuntimeError("Groq API key abhi set nahi hai. Website ke /admin page par daalein.")
    if _groq[0] != key:
        _groq = (key, AsyncGroq(api_key=key))
    return _groq[1]


async def ask(system, user, hist=None, temperature=0.7, max_tokens=6000):
    msgs = [{"role": "system", "content": system}] + (hist or []) + [{"role": "user", "content": user}]
    r = await get_groq().chat.completions.create(model=cfg("GROQ_MODEL") or DEF["GROQ_MODEL"],
                                                 messages=msgs, temperature=temperature,
                                                 max_tokens=max_tokens)
    return r.choices[0].message.content


SVG_SYS = ("Tum ek expert vector artist ho. Sirf ek complete SVG code do, koi explanation nahi. Rules: "
           'root <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">; '
           "gradients, paths, shapes use karo, rich aur detailed scene banao; <image>, <script>, "
           "external link ya font file mat use karo; svg 6000 characters se chhota rakho.")
VIDEO_SYS = ("Tum Python animator ho. Sirf ek ```python code block do. Code me ek function define karo: "
             "def frame(t): jahan t 0.0 se 1.0 tak hai, aur vo ek complete SVG string return kare "
             'jiska root <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720"> ho. '
             "Chizein t ke hisab se move/rotate/fade/color-change hon (smooth animation). "
             "Sirf `import math` aur `import random` allowed hai. File, network, os, open, eval sab mana hai. "
             "Frame function tez ho (200 se kam shapes).")
CODE_SYS = ("Tum senior programmer ho. User ka kaam poora, working code me likho: ek hi code block me "
            "(```lang), phir 3-4 line me kaise chalana hai. Code me placeholder ya adhura hissa mat chhodo.")


def story_sys():
    n = num("STORY_SCENES", 8)
    return (f"Tum kahani-lekhak aur vector artist ho. User ke topic par {n} scenes ki kahani likho. "
            "Har scene is format me do:\n=== SCENE ===\nTEXT: us scene ki 2-3 line ki kahani (user ki bhasha me)\n"
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">...</svg>\n'
            "SVG me text/akshar mat likho, sirf shapes aur gradients. Har SVG 2500 characters se chhota. "
            "<image>/<script>/external link mana hai. Aur kuchh extra mat likho.")


def get_svg(text):
    m = re.search(r"<svg.*?</svg>", text, re.S | re.I)
    if not m:
        raise ValueError("SVG nahi mila")
    return re.sub(r"<(script|image|foreignObject)\b.*?(</\1>|/>)", "", m.group(0), flags=re.S | re.I)


def svg_png(svg, width):
    return cairosvg.svg2png(bytestring=svg.encode(), output_width=width, background_color="white")


async def make_svg(prompt):
    last = None
    for _ in range(2):
        try:
            out = await ask(SVG_SYS, prompt, temperature=0.8, max_tokens=4000)
            svg = get_svg(out)
            png = await asyncio.to_thread(svg_png, svg, 1280)
            return svg, png
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"Image nahi ban payi: {last}")


# ---------------- Sandbox: LLM ka animation code ----------------
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


def to_img(svg):
    im = Image.open(io.BytesIO(svg_png(svg, vw()))).convert("RGB")
    return im.resize((vw(), vh()))


def write_mp4(images, path):
    with imageio.get_writer(path, fps=fps(), codec="libx264", pixelformat="yuv420p",
                            macro_block_size=None, quality=7) as w:
        for im in images:
            w.append_data(np.array(im))


def build_anim(frames, path):
    write_mp4((to_img(s) for s in frames), path)


def caption_layer(text):
    W, H = vw(), vh()
    lines = textwrap.wrap(text, 55)[:4]
    h = 32 * len(lines) + 24
    y0 = H - h
    t = "".join(
        f'<text x="{W / 2}" y="{y0 + 36 + i * 32}" font-size="24" fill="white" text-anchor="middle" '
        f'font-family="Noto Sans, Noto Sans Devanagari, DejaVu Sans, sans-serif">{html.escape(l)}</text>'
        for i, l in enumerate(lines))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">'
           f'<rect x="0" y="{y0}" width="{W}" height="{h}" fill="black" fill-opacity="0.6"/>{t}</svg>')
    return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode()))).convert("RGBA")


def build_story_video(scenes, path):
    base = []
    for text, svg in scenes:
        im = to_img(svg).convert("RGBA")
        base.append(Image.alpha_composite(im, caption_layer(text)).convert("RGB"))
    F, hold = fps(), num("SCENE_SECONDS", 10)

    def gen():
        fade = F // 2
        for i, im in enumerate(base):
            for _ in range(hold * F):
                yield im
            if i + 1 < len(base):
                for k in range(1, fade + 1):
                    yield Image.blend(im, base[i + 1], k / (fade + 1))
    write_mp4(gen(), path)


def parse_story(raw):
    scenes = []
    for chunk in raw.split("=== SCENE ==="):
        try:
            svg = get_svg(chunk)
            svg_png(svg, 100)
        except Exception:  # noqa
            continue
        scenes.append((chunk.split("<svg")[0].replace("TEXT:", "").strip(), svg))
    return scenes


# =====================================================================
#  TELEGRAM HANDLERS
# =====================================================================
def allowed(update: Update):
    ids = allowed_ids()
    return not ids or (update.effective_user and update.effective_user.id in ids)


async def send_long(update, text):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


def arg_text(ctx):
    return " ".join(ctx.args).strip()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    await update.message.reply_text(
        f"Namaste! Main {cfg('BOT_NAME')} hun.\n\n"
        "/img <vivaran> - code se image (PNG)\n"
        "/video <vivaran> - code se animation video (MP4)\n"
        "/story <topic> - poori kahani + vector video\n"
        "/code <kaam> - poora code file me\n"
        "/id - aapki Telegram ID\n"
        "/reset - baatcheet reset\n\n"
        "Normal message bhejein to main kisi bhi sawal ka jawab dunga.")


async def myid(update: Update, ctx):
    await update.message.reply_text(f"Aapki ID: {update.effective_user.id}")


async def reset(update: Update, ctx):
    history.pop(update.effective_chat.id, None)
    await update.message.reply_text("Baatcheet reset ho gayi.")


async def img(update: Update, ctx):
    if not allowed(update):
        return
    prompt = arg_text(ctx)
    if not prompt:
        return await update.message.reply_text("Aise likhein: /img ek sunset wala pahad")
    msg = await update.message.reply_text("Image ban rahi hai...")
    async with heavy:
        try:
            svg, png = await make_svg(prompt)
            await update.message.reply_photo(png)
            await update.message.reply_document(io.BytesIO(png), filename="image.png")
            await update.message.reply_document(io.BytesIO(svg.encode()), filename="image.svg")
        except Exception as e:  # noqa
            log.exception("img")
            await update.message.reply_text(f"Error: {e}")
    await msg.delete()


async def video(update: Update, ctx):
    if not allowed(update):
        return
    prompt = arg_text(ctx)
    if not prompt:
        return await update.message.reply_text("Aise likhein: /video ghoomta hua grah aur taare")
    msg = await update.message.reply_text("Video ban rahi hai (1-2 minute lag sakte hain)...")
    n = fps() * num("VIDEO_SECONDS", 15)
    async with heavy:
        try:
            await update.message.chat.send_action("upload_video")
            frames, last = None, None
            for _ in range(3):
                try:
                    out = await ask(VIDEO_SYS, prompt, temperature=0.7, max_tokens=3000)
                    m = re.search(r"```(?:python)?\n(.*?)```", out, re.S)
                    frames = await asyncio.to_thread(run_frames, m.group(1) if m else out, n)
                    await asyncio.to_thread(to_img, frames[0])  # test render
                    break
                except Exception as e:  # noqa
                    last, frames = e, None
            if not frames:
                raise RuntimeError(f"Animation code nahi chala: {last}")
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "video.mp4")
                await asyncio.to_thread(build_anim, frames, path)
                with open(path, "rb") as f:
                    await update.message.reply_video(f, supports_streaming=True,
                                                     read_timeout=180, write_timeout=180)
        except Exception as e:  # noqa
            log.exception("video")
            await update.message.reply_text(f"Error: {e}")
    await msg.delete()


async def story(update: Update, ctx):
    if not allowed(update):
        return
    topic = arg_text(ctx)
    if not topic:
        return await update.message.reply_text("Aise likhein: /story ek chhota robot jo dost dhundta hai")
    msg = await update.message.reply_text("Kahani aur vector scenes ban rahe hain...")
    async with heavy:
        try:
            raw = await ask(story_sys(), topic, temperature=0.8, max_tokens=7000)
            scenes = await asyncio.to_thread(parse_story, raw)
            if not scenes:
                raise RuntimeError("Scenes nahi ban paye, dobara try karein")
            await send_long(update, "\n\n".join(f"{i}. {t}" for i, (t, _) in enumerate(scenes, 1)))
            await update.message.chat.send_action("upload_video")
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "story.mp4")
                await asyncio.to_thread(build_story_video, scenes, path)
                with open(path, "rb") as f:
                    await update.message.reply_video(f, supports_streaming=True,
                                                     read_timeout=180, write_timeout=180)
            for i, (_, svg) in enumerate(scenes, 1):
                png = await asyncio.to_thread(svg_png, svg, 1280)
                await update.message.reply_document(io.BytesIO(png), filename=f"scene{i}.png")
        except Exception as e:  # noqa
            log.exception("story")
            await update.message.reply_text(f"Error: {e}")
    await msg.delete()


EXT = {"python": "py", "javascript": "js", "html": "html", "css": "css", "java": "java",
       "cpp": "cpp", "c": "c", "bash": "sh", "json": "json", "typescript": "ts"}


async def code(update: Update, ctx):
    if not allowed(update):
        return
    task = arg_text(ctx)
    if not task:
        return await update.message.reply_text("Aise likhein: /code Python me calculator banao")
    msg = await update.message.reply_text("Code likh raha hun...")
    try:
        out = await ask(CODE_SYS, task, temperature=0.3, max_tokens=8000)
        m = re.search(r"```(\w*)\n(.*?)```", out, re.S)
        if m:
            await update.message.reply_document(io.BytesIO(m.group(2).encode()),
                                                filename=f"main.{EXT.get(m.group(1).lower(), 'txt')}")
            rest = (out[:m.start()] + out[m.end():]).strip()
            if rest:
                await send_long(update, rest)
        else:
            await send_long(update, out)
    except Exception as e:  # noqa
        log.exception("code")
        await update.message.reply_text(f"Error: {e}")
    await msg.delete()


async def chat(update: Update, ctx):
    if not allowed(update):
        return
    cid = update.effective_chat.id
    h = history.setdefault(cid, [])
    text = update.message.text
    try:
        await update.message.chat.send_action("typing")
        system = str(cfg("CHAT_PROMPT") or DEFAULT_PROMPT).replace("{BOT_NAME}", str(cfg("BOT_NAME")))
        out = await ask(system, text, hist=h[-10:])
        h += [{"role": "user", "content": text}, {"role": "assistant", "content": out}]
        del h[:-20]
        await send_long(update, out)
    except Exception as e:  # noqa
        log.exception("chat")
        await update.message.reply_text(f"Error: {e}")


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
        for name, fn in [("start", start), ("help", start), ("id", myid), ("reset", reset),
                         ("img", img), ("video", video), ("story", story), ("code", code)]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
        try:
            await app.initialize()
            await app.bot.set_my_commands([
                ("img", "Code se image (PNG)"), ("video", "Code se video (MP4)"),
                ("story", "Kahani + vector video"), ("code", "Poora code likhwayein"),
                ("reset", "Baatcheet reset"), ("id", "Meri Telegram ID")])
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
    out = f'<h1>{html.escape(str(cfg("BOT_NAME")))} - Settings</h1>'
    if msg:
        out += f'<div class="msg">{html.escape(msg)}</div>'
    out += (f'<div class="st {"ok" if ok else "bad"}">Telegram bot: {html.escape(manager.status)}{who}</div>'
            f'<div class="st {"ok" if groq_ok else "bad"}">Groq key: {"set hai" if groq_ok else "set nahi hai"}</div>'
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
