import asyncio
import html
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import aiohttp
import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))
MAX_REQUESTS = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "10"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(49 * 1024 * 1024)))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

FACEBOOK_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com",
    "fb.watch", "www.fb.watch",
}
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("facebook-video-bot")

rate_windows: dict[int, deque[float]] = defaultdict(deque)
cache: dict[str, tuple[float, "VideoResult"]] = {}

MENU_BUTTONS = [["✦ إرسال رابط", "✦ المساعدة"], ["✦ عن البوت", "✦ إعادة التشغيل"]]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        MENU_BUTTONS,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="✦ أرسل رابط فيديو فيسبوك...",
    )


@dataclass
class VideoOption:
    url: str
    height: int
    format_id: str | None = None
    filesize: int | None = None


@dataclass
class VideoResult:
    title: str
    thumbnail: str | None
    hd: VideoOption | None
    sd: VideoOption | None
    source_url: str
    created_at: float


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/", "/health"}:
            body = b"facebook-video-bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health server listening on port %s", port)


def normalize_facebook_url(value: str) -> str | None:
    value = value.strip().strip("<>[](){}.,!?؛،")
    if not value.lower().startswith(("http://", "https://")):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in FACEBOOK_HOSTS:
        return None
    return value


def allowed_request(user_id: int) -> bool:
    now = time.time()
    window = rate_windows[user_id]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= MAX_REQUESTS:
        return False
    window.append(now)
    return True


def clean_expired_cache() -> None:
    now = time.time()
    expired = [key for key, (stamp, _) in cache.items() if now - stamp > CACHE_TTL]
    for key in expired:
        cache.pop(key, None)


def choose_options(info: dict[str, Any]) -> VideoResult:
    formats = info.get("formats") or []
    progressive = []
    fallback = []
    for item in formats:
        direct_url = item.get("url")
        if not direct_url or item.get("protocol") in {"m3u8", "m3u8_native", "http_dash_segments"}:
            continue
        if item.get("vcodec") in (None, "none"):
            continue
        height = int(item.get("height") or 0)
        option = VideoOption(
            url=direct_url,
            height=height,
            format_id=str(item.get("format_id")) if item.get("format_id") else None,
            filesize=item.get("filesize") or item.get("filesize_approx"),
        )
        fallback.append(option)
        if item.get("acodec") not in (None, "none"):
            progressive.append(option)

    choices = progressive or fallback
    choices = sorted(choices, key=lambda x: (x.height, x.filesize or 0))
    if not choices:
        # Some extractors expose a selected direct URL even when formats are sparse.
        direct = info.get("url")
        if direct:
            choices = [VideoOption(url=direct, height=int(info.get("height") or 0), format_id=str(info.get("format_id") or "best"))]

    hd = choices[-1] if choices else None
    sd_candidates = [item for item in choices if item.height <= 720]
    sd = (sd_candidates[-1] if sd_candidates else (choices[0] if choices else None))
    title = str(info.get("title") or "فيديو فيسبوك")[:900]
    thumbnail = info.get("thumbnail")
    return VideoResult(title=title, thumbnail=thumbnail, hd=hd, sd=sd, source_url=str(info.get("webpage_url") or ""), created_at=time.time())


def extract_video(url: str) -> VideoResult:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "http_headers": {
            "User-Agent": os.getenv(
                "USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
            ),
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        },
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    return choose_options(info)


async def fetch_video_size(url: str) -> int | None:
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"User-Agent": os.getenv("USER_AGENT", "Mozilla/5.0")}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.head(url, allow_redirects=True) as response:
                value = response.headers.get("Content-Length")
                return int(value) if value and value.isdigit() else None
    except Exception:
        return None


async def download_if_small(option: VideoOption, target: Path) -> bool:
    timeout = aiohttp.ClientTimeout(total=180, connect=15, sock_read=45)
    headers = {"User-Agent": os.getenv("USER_AGENT", "Mozilla/5.0")}
    total = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(option.url, allow_redirects=True) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_UPLOAD_BYTES:
                    return False
                with target.open("wb") as output:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            output.close()
                            target.unlink(missing_ok=True)
                            return False
                        output.write(chunk)
        return target.exists() and target.stat().st_size > 0
    except Exception:
        target.unlink(missing_ok=True)
        log.exception("download failed")
        return False


def option_for(result: VideoResult, quality: str) -> VideoOption | None:
    return result.hd if quality == "hd" else result.sd


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلًا بيك\n\n"
        "أرسل رابط فيديو عام من فيسبوك، وأنا أجهز لك خيارات التحميل المتاحة.\n\n"
        "الروابط المدعومة: watch، videos، reel، share و fb.watch.\n"
        "الفيديوهات الخاصة أو المحذوفة ما تنفتح.",
        reply_markup=main_keyboard(),
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "طريقة الاستخدام\n\n"
        "1. انسخ رابط فيديو عام من فيسبوك.\n"
        "2. أرسله هنا.\n"
        "3. اختار HD أو SD.\n\n"
        "ملاحظة: استخدم البوت فقط مع المقاطع التي تملك حق تنزيلها أو لديك إذن بها.",
        reply_markup=main_keyboard(),
    )


async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✦ عن البوت\n\n"
        "بوت تحميل فيديوهات فيسبوك العامة بسرعة وسهولة.\n\n"
        "✦ المميزات:\n"
        "• استخراج روابط الفيديو العامة.\n"
        "• اختيار جودة HD أو SD.\n"
        "• إرسال الفيديو كملف داخل تيليگرام.\n"
        "• إرسال رابط مباشر إذا كان حجم الفيديو كبيرًا.\n"
        "• دعم روابط watch وvideos وreel وshare وfb.watch.\n"
        "• إظهار حالة المعالجة والإرسال للمستخدم.\n"
        "• لوحة أزرار ثابتة وسهلة الاستخدام.\n"
        "• حماية مؤقتة من كثرة الطلبات.\n\n"
        "✦ المالك والمطور: @bbayyy\n"
        "✦ الحد المؤقت: 10 طلبات لكل مستخدم بالدقيقة.",
        reply_markup=main_keyboard(),
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    button = update.message.text.strip()
    if button == "✦ المساعدة":
        await show_help(update, context)
        return
    if button == "✦ عن البوت":
        await show_about(update, context)
        return
    if button in {"✦ إرسال رابط", "✦ إعادة التشغيل"}:
        await update.message.reply_text(
            "أرسل رابط فيديو عام من فيسبوك هنا.",
            reply_markup=main_keyboard(),
        )
        return
    match = URL_RE.search(update.message.text)
    url = normalize_facebook_url(match.group(0) if match else "")
    if not url:
        await update.message.reply_text(
            "الرابط غير صالح. أرسل رابط فيديو عام يبدأ بـ facebook.com أو fb.watch.",
            reply_markup=main_keyboard(),
        )
        return
    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    if not allowed_request(user_id):
        await update.message.reply_text(
            "وصلت للحد المؤقت: 10 طلبات بالدقيقة. جرّب بعد قليل.",
            reply_markup=main_keyboard(),
        )
        return

    clean_expired_cache()
    waiting = await update.message.reply_text("✦ جارٍ تحليل الرابط... ⏳")
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        cached = cache.get(url)
        if cached and time.time() - cached[0] <= CACHE_TTL:
            result = cached[1]
        else:
            result = await asyncio.to_thread(extract_video, url)
            cache[url] = (time.time(), result)
        if not result.hd and not result.sd:
            raise ValueError("no video formats")

        buttons = []
        if result.hd:
            buttons.append(InlineKeyboardButton("✦ تحميل HD", callback_data="dl:" + _cache_key(url, result)))
        if result.sd:
            buttons.append(InlineKeyboardButton("✦ تحميل SD", callback_data="ds:" + _cache_key(url, result)))
        keyboard = InlineKeyboardMarkup([buttons])
        caption = f"✦ <b>{html.escape(result.title)}</b>\n\nاختار الجودة المطلوبة:"
        if result.thumbnail:
            try:
                await update.message.reply_photo(photo=result.thumbnail, caption=caption, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                await update.message.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)
        await waiting.delete()
    except Exception as exc:
        log.warning("extraction failed for %s: %s", url, exc)
        await waiting.edit_text("ما لگيت الفيديو. ممكن يكون خاص أو محذوف أو الرابط غير صحيح.")


def _cache_key(url: str, result: VideoResult) -> str:
    key = secrets.token_urlsafe(8)
    cache["key:" + key] = (time.time(), result)
    cache["url:" + key] = (time.time(), type("Source", (), {"source_url": url})())
    return key


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        prefix, key = query.data.split(":", 1)
        cached = cache.get("key:" + key)
        source = cache.get("url:" + key)
        if not cached or not source or time.time() - cached[0] > CACHE_TTL:
            await query.edit_message_text("انتهت صلاحية النتيجة. أرسل الرابط مرة ثانية.")
            return
        result: VideoResult = cached[1]
        quality = "hd" if prefix == "dl" else "sd"
        option = option_for(result, quality)
        if not option:
            await query.edit_message_text("هذه الجودة غير متوفرة لهذا الفيديو.")
            return
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_VIDEO)
        size = option.filesize or await fetch_video_size(option.url)
        if size and size > MAX_UPLOAD_BYTES:
            await query.message.reply_text(
                f"الفيديو أكبر من حد الإرسال داخل تيليگرام.\n\nرابط التحميل المباشر ({quality.upper()}):\n{option.url}",
                reply_markup=main_keyboard(),
            )
            return
        target = DOWNLOAD_DIR / f"{secrets.token_hex(12)}.mp4"
        downloaded = await download_if_small(option, target)
        if not downloaded:
            await query.message.reply_text(
                f"تعذر إرساله كملف أو حجمه أكبر من الحد.\n\nرابط التحميل المباشر:\n{option.url}",
                reply_markup=main_keyboard(),
            )
            return
        try:
            with target.open("rb") as video_file:
                await query.message.reply_video(
                    video=InputFile(video_file, filename="facebook-video.mp4"),
                    caption=result.title[:900],
                    supports_streaming=True,
                    reply_markup=main_keyboard(),
                )
        finally:
            target.unlink(missing_ok=True)
    except Exception:
        log.exception("callback failed")
        await query.message.reply_text(
            "صار خطأ أثناء تجهيز الفيديو. جرّب إرسال الرابط من جديد.",
            reply_markup=main_keyboard(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled bot error: %s", context.error, exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير مضبوط. انسخ .env.example إلى .env وضع التوكن داخله.")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg غير موجود؛ استخراج الروابط سيعمل، لكن الدمج المتقدم غير متاح.")
    start_health_server()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^(dl|ds):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_error_handler(error_handler)
    log.info("bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
