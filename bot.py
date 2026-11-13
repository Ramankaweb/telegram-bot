import asyncio
import html
import io
import json
import logging
import os
import re
import resource
import subprocess
import sys
import tempfile
import textwrap

import cairosvg
import httpx
import imageio
import numpy as np
from aiohttp import web
from groq import AsyncGroq
from PIL import Image
from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

# ---------------- SETTINGS (Render > Environment se badlein) ----------------
TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
PORT = int(os.getenv("PORT", "10000"))
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "")
VW, VH = int(os.getenv("VIDEO_W", "960")), int(os.getenv("VIDEO_H", "540"))
FPS = int(os.getenv("VIDEO_FPS", "12"))
VIDEO_SECONDS = int(os.getenv("VIDEO_SECONDS", "6"))
STORY_SCENES = int(os.getenv("STORY_SCENES", "4"))
SCENE_SECONDS = int(os.getenv("SCENE_SECONDS", "4"))
BOT_NAME = os.getenv("BOT_NAME", "JARVIS")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
groq = AsyncGroq(api_key=GROQ_KEY)
heavy = asyncio.Semaphore(1)  # free server par ek time me ek bhaari kaam
history: dict[int, list] = {}

CHAT_SYS = (f"Tum {BOT_NAME} ho, ek helpful AI assistant. User jis bhasha me likhe (Hindi/Hinglish/English) "
            "usi me jawab do. Jawab saaf, seedha aur kaam ka ho.")
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
STORY_SYS = (f"Tum kahani-lekhak aur vector artist ho. User ke topic par {STORY_SCENES} scenes ki kahani likho. "
             "Har scene is format me do:\n=== SCENE ===\nTEXT: us scene ki 2-3 line ki kahani (user ki bhasha me)\n"
             '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">...</svg>\n'
             "SVG me text/akshar mat likho, sirf shapes aur gradients. Har SVG 2500 characters se chhota. "
             "<image>/<script>/external link mana hai. Aur kuchh extra mat likho.")
CODE_SYS = ("Tum senior programmer ho. User ka kaam poora, working code me likho: ek hi code block me "
            "(```lang), phir 3-4 line me kaise chalana hai. Code me placeholder ya adhura hissa mat chhodo.")


async def ask(system, user, hist=None, temperature=0.7, max_tokens=6000):
    msgs = [{"role": "system", "content": system}] + (hist or []) + [{"role": "user", "content": user}]
    r = await groq.chat.completions.create(model=MODEL, messages=msgs,
                                           temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content


def get_svg(text):
    m = re.search(r"<svg.*?</svg>", text, re.S | re.I)
    if not m:
        raise ValueError("SVG nahi mila")
    svg = m.group(0)
    svg = re.sub(r"<(script|image|foreignObject)\b.*?(</\1>|/>)", "", svg, flags=re.S | re.I)
    return svg


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
        cp, op = os.path.join(d, "a.py"), os.path.join(d, "out.json")
        rp = os.path.join(d, "run.py")
        open(cp, "w").write(code)
        open(rp, "w").write(RUNNER)
        p = subprocess.run([sys.executable, rp, cp, str(n), op], capture_output=True,
                           text=True, timeout=40, preexec_fn=limits, cwd=d)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-300:])
        return json.load(open(op))


def to_img(svg):
    im = Image.open(io.BytesIO(svg_png(svg, VW))).convert("RGB")
    return im.resize((VW, VH))


def write_mp4(images, path):
    with imageio.get_writer(path, fps=FPS, codec="libx264", pixelformat="yuv420p",
                            macro_block_size=None, quality=7) as w:
        for im in images:
            w.append_data(np.array(im))


def build_anim(frames, path):
    write_mp4((to_img(s) for s in frames), path)


def caption_layer(text):
    lines = textwrap.wrap(text, 55)[:4]
    h = 32 * len(lines) + 24
    y0 = VH - h
    t = "".join(
        f'<text x="{VW / 2}" y="{y0 + 36 + i * 32}" font-size="24" fill="white" text-anchor="middle" '
        f'font-family="Noto Sans, Noto Sans Devanagari, DejaVu Sans, sans-serif">{html.escape(l)}</text>'
        for i, l in enumerate(lines))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{VW}" height="{VH}">'
           f'<rect x="0" y="{y0}" width="{VW}" height="{h}" fill="black" fill-opacity="0.6"/>{t}</svg>')
    return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode()))).convert("RGBA")


def build_story_video(scenes, path):
    base = []
    for text, svg in scenes:
        im = to_img(svg).convert("RGBA")
        base.append(Image.alpha_composite(im, caption_layer(text)).convert("RGB"))

    def gen():
        fade = FPS // 2
        for i, im in enumerate(base):
            for _ in range(SCENE_SECONDS * FPS):
                yield im
            if i + 1 < len(base):
                for k in range(1, fade + 1):
                    yield Image.blend(im, base[i + 1], k / (fade + 1))
    write_mp4(gen(), path)


def parse_story(raw):
    scenes, texts = [], []
    for chunk in raw.split("=== SCENE ==="):
        try:
            svg = get_svg(chunk)
        except ValueError:
            continue
        text = chunk.split("<svg")[0].replace("TEXT:", "").strip()
        try:
            svg_png(svg, 100)
        except Exception:  # noqa
            continue
        scenes.append((text, svg))
    return scenes


# ---------------- Telegram helpers ----------------
def allowed(update: Update):
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


async def send_long(update, text):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


def arg_text(ctx):
    return " ".join(ctx.args).strip()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text(f"Aapki ID {update.effective_user.id} allowed nahi hai.")
    await update.message.reply_text(
        f"Namaste! Main {BOT_NAME} hun.\n\n"
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
    n = FPS * VIDEO_SECONDS
    async with heavy:
        try:
            await update.message.chat.send_action("upload_video")
            frames, last = None, None
            for _ in range(3):
                try:
                    out = await ask(VIDEO_SYS, prompt, temperature=0.7, max_tokens=3000)
                    m = re.search(r"```(?:python)?\n(.*?)```", out, re.S)
                    code = m.group(1) if m else out
                    frames = await asyncio.to_thread(run_frames, code, n)
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
            raw = await ask(STORY_SYS, topic, temperature=0.8, max_tokens=7000)
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
            lang, body = m.group(1).lower(), m.group(2)
            await update.message.reply_document(io.BytesIO(body.encode()),
                                                filename=f"main.{EXT.get(lang, 'txt')}")
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
        out = await ask(CHAT_SYS, text, hist=h[-10:])
        h += [{"role": "user", "content": text}, {"role": "assistant", "content": out}]
        del h[:-20]
        await send_long(update, out)
    except Exception as e:  # noqa
        log.exception("chat")
        await update.message.reply_text(f"Error: {e}")


# ---------------- Health page + keep-alive ----------------
async def health(request):
    return web.json_response({"status": "ok", "bot": BOT_NAME, "model": MODEL})


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
    app = Application.builder().token(TOKEN).build()
    for name, fn in [("start", start), ("help", start), ("id", myid), ("reset", reset),
                     ("img", img), ("video", video), ("story", story), ("code", code)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    web_app = web.Application()
    web_app.add_routes([web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health server port %s par chal raha hai", PORT)

    async with app:
        await app.bot.set_my_commands([
            ("img", "Code se image (PNG)"), ("video", "Code se video (MP4)"),
            ("story", "Kahani + vector video"), ("code", "Poora code likhwayein"),
            ("reset", "Baatcheet reset"), ("id", "Meri Telegram ID")])
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(keepalive())
        log.info("Bot chalu hai")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
