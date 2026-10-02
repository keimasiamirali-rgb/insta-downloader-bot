import asyncio
import glob
import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import time

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.media_group import MediaGroupBuilder
from dotenv import load_dotenv

# ───────────────────────── تنظیمات ─────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN در فایل .env تنظیم نشده است.")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)       # برای /stats
COOKIES_FILE = os.getenv("COOKIES_FILE", "")           # مثلاً cookies.txt
DB_PATH = os.getenv("DB_PATH", "bot.db")

MAX_FILE_MB = 50          # سقف آپلود ربات‌ها در Bot API معمولی
MAX_CONCURRENT = 3        # حداکثر دانلود همزمان
COOLDOWN_SECONDS = 8      # فاصله بین دو درخواست هر کاربر
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("insta-bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
sem = asyncio.Semaphore(MAX_CONCURRENT)
last_request: dict[int, float] = {}

IG_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s]+")
CODE_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)"
)

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen INTEGER,
        downloads INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cache (
        code TEXT PRIMARY KEY,
        url TEXT,
        caption TEXT,
        items TEXT,
        audio_id TEXT
    );
    """
)
db.commit()


def touch_user(user: types.User) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_seen) VALUES (?, ?, ?)",
        (user.id, user.username, int(time.time())),
    )
    db.commit()


def inc_downloads(user_id: int) -> None:
    db.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,))
    db.commit()


def cache_get(code: str):
    return db.execute("SELECT * FROM cache WHERE code = ?", (code,)).fetchone()


def cache_put(code: str, url: str, caption: str, items: list) -> None:
    db.execute(
        "INSERT OR REPLACE INTO cache (code, url, caption, items) VALUES (?, ?, ?, ?)",
        (code, url, caption, json.dumps(items)),
    )
    db.commit()


def cache_set_audio(code: str, audio_id: str) -> None:
    db.execute("UPDATE cache SET audio_id = ? WHERE code = ?", (audio_id, code))
    db.commit()


# ───────────────────────── ابزارها ─────────────────────────
def clean_url(url: str) -> str:
    return url.split("?")[0].split("#")[0]


def extract_code(url: str) -> str | None:
    m = CODE_RE.search(url)
    return m.group(1) if m else None


def cooldown_left(user_id: int) -> int:
    """اگر کاربر در کولداون باشه ثانیه‌های باقی‌مانده، وگرنه ۰ برمی‌گردونه."""
    if user_id == ADMIN_ID:
        return 0
    now = time.monotonic()
    passed = now - last_request.get(user_id, 0)
    if passed < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - passed) + 1
    last_request[user_id] = now
    return 0


def build_caption(author: str | None, desc: str | None) -> str:
    parts = []
    if author:
        parts.append(f"👤 <b>{html.escape(author)}</b>")
    if desc:
        d = desc.strip()
        if len(d) > 500:
            d = d[:500].rstrip() + "…"
        parts.append(html.escape(d))
    parts.append("✅ دانلود شد")
    caption = "\n\n".join(parts)
    if len(caption) > 1000:  # سقف کپشن تلگرام ۱۰۲۴ کاراکتره
        caption = "\n\n".join(parts[:1] + parts[-1:])
    return caption


def friendly_error(e: Exception) -> str:
    t = str(e).lower()
    if any(k in t for k in ("login", "private", "cookies", "not available", "empty media")):
        return "🔒 این پست خصوصیه یا اینستاگرام برای دیدنش لاگین می‌خواد."
    if any(k in t for k in ("429", "rate-limit", "rate limit", "too many")):
        return "⏱ اینستاگرام موقتاً درخواست‌ها رو محدود کرده. چند دقیقه دیگه دوباره امتحان کن."
    return "❌ نتونستم دانلود کنم. لینک رو چک کن و دوباره امتحان کن."


def mp3_markup(code: str | None, kinds: list[str]) -> InlineKeyboardMarkup | None:
    if not code or kinds != ["video"]:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎵 دریافت صدا (MP3)", callback_data=f"mp3:{code}")]
        ]
    )


# ───────────────────────── دانلود (در thread جدا) ─────────────────────────
def _base_opts(tmpdir: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "socket_timeout": 20,
        "retries": 3,
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def download_media(url: str, tmpdir: str):
    opts = _base_opts(tmpdir)
    opts.update(
        {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = info.get("entries") or [info]
    items, too_big = [], 0
    for e in entries:
        if not e:
            continue
        rd = e.get("requested_downloads") or []
        path = rd[0].get("filepath") if rd else None
        if not path or not os.path.exists(path):
            too_big += 1  # معمولاً یعنی از max_filesize رد شده
            continue
        if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
            too_big += 1
            continue
        is_video = path.lower().endswith(VIDEO_EXTS)
        items.append(
            {
                "type": "video" if is_video else "photo",
                "media": FSInputFile(path),
                "width": int(e["width"]) if e.get("width") else None,
                "height": int(e["height"]) if e.get("height") else None,
                "duration": int(e["duration"]) if e.get("duration") else None,
            }
        )
    author = info.get("uploader") or info.get("channel")
    desc = info.get("description") or info.get("title")
    return items, too_big, author, desc


def download_audio(url: str, tmpdir: str):
    opts = _base_opts(tmpdir)
    opts.update(
        {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = glob.glob(os.path.join(tmpdir, "*.mp3"))
    title = info.get("title") or "audio"
    performer = info.get("uploader")
    return (files[0] if files else None), title, performer


# ───────────────────────── ارسال به تلگرام ─────────────────────────
async def _send_single(message: types.Message, e: dict, caption, markup):
    if e["type"] == "video":
        m = await message.reply_video(
            e["media"],
            caption=caption,
            width=e.get("width"),
            height=e.get("height"),
            duration=e.get("duration"),
            supports_streaming=True,
            reply_markup=markup,
        )
        return ("video", m.video.file_id)
    m = await message.reply_photo(e["media"], caption=caption, reply_markup=markup)
    return ("photo", m.photo[-1].file_id)


async def send_items(message: types.Message, entries: list[dict], caption, markup=None):
    """ارسال یک یا چند مدیا. خروجی: لیست (نوع، file_id) برای کش."""
    sent = []
    chunks = [entries[i : i + 10] for i in range(0, len(entries), 10)]
    for ci, chunk in enumerate(chunks):
        cap = caption if ci == 0 else None
        if len(chunk) == 1:
            single_markup = markup if len(entries) == 1 else None
            sent.append(await _send_single(message, chunk[0], cap, single_markup))
            continue

        builder = MediaGroupBuilder(caption=cap)
        for e in chunk:
            if e["type"] == "video":
                builder.add_video(
                    media=e["media"],
                    width=e.get("width"),
                    height=e.get("height"),
                    duration=e.get("duration"),
                    supports_streaming=True,
                )
            else:
                builder.add_photo(media=e["media"])
        msgs = await message.reply_media_group(media=builder.build())
        for m in msgs:
            if m.video:
                sent.append(("video", m.video.file_id))
            elif m.photo:
                sent.append(("photo", m.photo[-1].file_id))
    return sent


# ───────────────────────── هندلرها ─────────────────────────
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    touch_user(message.from_user)
    await message.answer(
        "سلام! 👋\n"
        "لینک پست، ریلز یا استوری اینستاگرام رو برام بفرست تا برات دانلود کنم.\n\n"
        "• پست‌های چندتایی (کاروسل) هم پشتیبانی می‌شن\n"
        "• برای ریلز می‌تونی بعد از دانلود، صداش رو هم به‌صورت MP3 بگیری"
    )


@dp.message(Command("stats"))
async def stats_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total = db.execute("SELECT COALESCE(SUM(downloads), 0) c FROM users").fetchone()["c"]
    cached = db.execute("SELECT COUNT(*) c FROM cache").fetchone()["c"]
    await message.answer(
        f"📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: {users}\n"
        f"⬇️ دانلودها: {total}\n"
        f"💾 آیتم‌های کش‌شده: {cached}"
    )


@dp.message(F.text)
async def download_handler(message: types.Message):
    m = IG_RE.search(message.text)
    if not m:
        return  # پیام بی‌ربط؛ در گروه‌ها هم مزاحم نمی‌شیم

    touch_user(message.from_user)

    wait = cooldown_left(message.from_user.id)
    if wait:
        await message.reply(f"⏳ لطفاً {wait} ثانیه دیگه صبر کن.")
        return

    url = clean_url(m.group(0))
    code = extract_code(url)

    # ۱) اگه قبلاً دانلود شده، مستقیم از تلگرام می‌فرستیم
    if code:
        cached = cache_get(code)
        if cached:
            try:
                kinds_ids = json.loads(cached["items"])
                entries = [{"type": t, "media": fid} for t, fid in kinds_ids]
                await send_items(
                    message,
                    entries,
                    cached["caption"],
                    mp3_markup(code, [t for t, _ in kinds_ids]),
                )
                inc_downloads(message.from_user.id)
                return
            except Exception:
                log.exception("cache send failed, falling back to download")

    # ۲) دانلود تازه
    status_msg = await message.reply("⏳ در حال دانلود...")
    try:
        async with ChatActionSender.upload_video(chat_id=message.chat.id, bot=bot):
            async with sem:
                with tempfile.TemporaryDirectory() as tmpdir:
                    items, too_big, author, desc = await asyncio.to_thread(
                        download_media, url, tmpdir
                    )

                    if not items:
                        text = (
                            f"📦 حجم فایل بیشتر از {MAX_FILE_MB} مگابایته و تلگرام اجازه ارسالش رو نمی‌ده."
                            if too_big
                            else "❌ مدیایی پیدا نشد."
                        )
                        await message.reply(text)
                        return

                    caption = build_caption(author, desc)
                    kinds = [i["type"] for i in items]
                    sent = await send_items(message, items, caption, mp3_markup(code, kinds))

                    if too_big:
                        await message.reply(
                            f"ℹ️ {too_big} مورد به‌خاطر حجم بالای {MAX_FILE_MB}MB ارسال نشد."
                        )

        if code and sent:
            cache_put(code, url, caption, [list(s) for s in sent])
        inc_downloads(message.from_user.id)

    except yt_dlp.utils.DownloadError as e:
        log.warning("download error for %s: %s", url, e)
        await message.reply(friendly_error(e))
    except Exception:
        log.exception("unexpected error for %s", url)
        await message.reply("❌ خطای غیرمنتظره‌ای پیش اومد. بعداً دوباره امتحان کن.")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


@dp.callback_query(F.data.startswith("mp3:"))
async def mp3_handler(cb: types.CallbackQuery):
    if not isinstance(cb.message, types.Message):
        await cb.answer()
        return

    code = cb.data.split(":", 1)[1]
    row = cache_get(code)
    if not row:
        await cb.answer("اطلاعات پیدا نشد؛ لینک رو دوباره بفرست.", show_alert=True)
        return

    # اگه صدا قبلاً ساخته شده، فوری می‌فرستیم
    if row["audio_id"]:
        await cb.answer()
        await cb.message.reply_audio(row["audio_id"])
        return

    wait = cooldown_left(cb.from_user.id)
    if wait:
        await cb.answer(f"لطفاً {wait} ثانیه دیگه صبر کن.", show_alert=True)
        return

    await cb.answer("🎵 در حال آماده‌سازی...")
    try:
        async with ChatActionSender.upload_voice(chat_id=cb.message.chat.id, bot=bot):
            async with sem:
                with tempfile.TemporaryDirectory() as tmpdir:
                    path, title, performer = await asyncio.to_thread(
                        download_audio, row["url"], tmpdir
                    )
                    if not path or os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
                        await cb.message.reply("❌ نتونستم صدا رو آماده کنم.")
                        return
                    sent = await cb.message.reply_audio(
                        FSInputFile(path), title=title, performer=performer
                    )
        cache_set_audio(code, sent.audio.file_id)
    except Exception:
        log.exception("audio error for %s", row["url"])
        await cb.message.reply("❌ نتونستم صدا رو آماده کنم.")


# ───────────────────────── اجرا ─────────────────────────
async def main():
    log.info("ربات شروع به کار کرد...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
