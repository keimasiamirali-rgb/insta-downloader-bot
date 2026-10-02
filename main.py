import asyncio
import logging
import os
import yt_dlp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from dotenv import load_dotenv
import tempfile

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "سلام! 👋\n"
        "لینک پست، ریلز یا استوری اینستاگرام رو برام بفرست تا برات دانلود کنم."
    )

@dp.message(F.text)
async def download_handler(message: types.Message):
    url = message.text.strip()

    # فقط لینک اینستاگرام
    if "instagram.com" not in url and "instagr.am" not in url:
        return

    # پیام در حال دانلود (بعداً پاک می‌شه)
    status_msg = await message.reply("⏳ در حال دانلود...")

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "best",
            "outtmpl": "%(id)s.%(ext)s",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            # ارسال فایل با ریپلای روی پیام اصلی
            if filename.lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                video = FSInputFile(filename)
                await message.reply_video(video, caption="✅ دانلود شد")
            else:
                photo = FSInputFile(filename)
                await message.reply_photo(photo, caption="✅ دانلود شد")

    except Exception as e:
        await message.reply("❌ نتونستم دانلود کنم.\nلینک ممکنه خصوصی باشه یا اینستاگرام موقتاً جلوی دانلود رو گرفته باشه.")
        print("Error:", e)

    finally:
        # پاک کردن پیام «در حال دانلود»
        try:
            await status_msg.delete()
        except:
            pass

async def main():
    print("ربات شروع به کار کرد...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())