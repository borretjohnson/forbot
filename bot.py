"""
Telegram-бот для извлечения и удаления EXIF-метаданных из фотографий.
Использует aiogram 3.x, Pillow, pillow-heif, piexif.

Установка зависимостей:
    pip install aiogram pillow pillow-heif piexif aiohttp-socks
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image
from PIL.ExifTags import TAGS

# HEIC/HEIF поддержка
try:
    from pillow_heif import register_heif_opener, open_heif
    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    HEIF_SUPPORTED = False

# piexif для парсинга сырых EXIF-байт из HEIC
try:
    import piexif
    PIEXIF_SUPPORTED = True
except ImportError:
    PIEXIF_SUPPORTED = False

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

TOKEN = os.getenv("TOKEN", "8674031134:AAFi01_IGAzuBEOrVAXrt3E1qqDpamyQZXg")  # токен из переменной окружения

# Прокси для обхода блокировок (Hugging Face, Render и др.)
# Используем публичный прокси или свой
PROXY_URL = os.getenv("PROXY_URL", "socks5://proxy.huggingface.co:1080")

# =============================================================================
# ИНИЦИАЛИЗАЦИЯ
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Импорт SOCKS прокси
try:
    from aiohttp_socks import ProxyConnector
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False

# Создаём сессию с прокси
if PROXY_URL and SOCKS_AVAILABLE:
    connector = ProxyConnector.from_url(PROXY_URL)
    _session = AiohttpSession(connector=connector, timeout=120)
else:
    _session = AiohttpSession(timeout=120)

bot = Bot(token=TOKEN, session=_session)
dp = Dispatcher()

file_storage: dict[str, str] = {}


# =============================================================================
# МОДЕЛЬ ДАННЫХ
# =============================================================================

@dataclass
class ExifData:
    make: Optional[str] = None
    model: Optional[str] = None
    datetime_original: Optional[str] = None
    datetime: Optional[str] = None
    gps: Optional[tuple] = None
    software: Optional[str] = None
    orientation: Optional[str] = None
    exposure_time: Optional[str] = None
    fnumber: Optional[str] = None
    iso: Optional[str] = None
    focal_length: Optional[str] = None
    flash: Optional[int] = None
    white_balance: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    lens_model: Optional[str] = None
    has_exif: bool = False


# =============================================================================
# GPS
# =============================================================================

def _dms_to_decimal(dms, ref: str) -> Optional[float]:
    """DMS (градусы/минуты/секунды) → десятичные градусы."""
    try:
        if not dms or len(dms) < 3:
            return None
        # piexif хранит как ((num, den), (num, den), (num, den))
        def to_float(v):
            if isinstance(v, tuple) and len(v) == 2:
                return v[0] / v[1] if v[1] != 0 else 0.0
            return float(v)
        val = to_float(dms[0]) + to_float(dms[1]) / 60.0 + to_float(dms[2]) / 3600.0
        if isinstance(ref, bytes):
            ref = ref.decode("ascii", errors="ignore")
        return round(-val if ref.upper() in ("S", "W") else val, 6)
    except Exception as e:
        logger.debug(f"_dms_to_decimal error: {e}")
        return None


def _gps_from_dict(gps: dict) -> Optional[tuple]:
    """Извлекает (lat, lon) из GPS-словаря (ключи числовые: 1,2,3,4)."""
    try:
        lat = _dms_to_decimal(gps.get(2), gps.get(1, "N"))
        lon = _dms_to_decimal(gps.get(4), gps.get(3, "E"))
        if lat is not None and lon is not None:
            return (lat, lon)
    except Exception as e:
        logger.debug(f"_gps_from_dict error: {e}")
    return None


def _gps_from_piexif_bytes(exif_bytes: bytes) -> Optional[tuple]:
    """
    Парсит сырые EXIF-байты через piexif и извлекает GPS.
    Именно так GPS хранится в HEIC: metadata[n]['data'] = b'Exif\\x00\\x00...'
    """
    if not PIEXIF_SUPPORTED:
        return None
    try:
        # piexif.load принимает байты с заголовком Exif\x00\x00 или без
        exif_dict = piexif.load(exif_bytes)
        gps_ifd: dict = exif_dict.get("GPS", {})
        if not gps_ifd:
            return None
        # В piexif ключи GPS IFD числовые: 1=LatRef, 2=Lat, 3=LonRef, 4=Lon
        return _gps_from_dict(gps_ifd)
    except Exception as e:
        logger.debug(f"_gps_from_piexif_bytes error: {e}")
    return None


def _extract_gps_heic(image_bytes: bytes) -> Optional[tuple]:
    """
    Извлекает GPS из HEIC через pillow-heif + piexif.
    pillow-heif хранит EXIF в heif_file[0].info['exif']
    в виде байт с заголовком b'Exif\\x00\\x00'.
    """
    if not HEIF_SUPPORTED:
        return None
    try:
        hf = open_heif(BytesIO(image_bytes))
        # Перебираем все изображения в файле (обычно одно)
        for img in hf:
            info = getattr(img, "info", {}) or {}
            exif_bytes = info.get("exif")
            if exif_bytes:
                logger.debug(f"HEIC: exif_bytes len={len(exif_bytes)}, header={exif_bytes[:6]}")
                gps = _gps_from_piexif_bytes(exif_bytes)
                if gps:
                    logger.info(f"GPS из HEIC (pillow-heif+piexif): {gps}")
                    return gps

        # Запасной вариант: ищем блок Exif в сырых байтах файла
        idx = image_bytes.find(b"Exif\x00\x00")
        if idx != -1:
            exif_bytes = image_bytes[idx:]
            gps = _gps_from_piexif_bytes(exif_bytes)
            if gps:
                logger.info(f"GPS из HEIC (raw scan): {gps}")
                return gps

    except Exception as e:
        logger.warning(f"_extract_gps_heic error: {e}")
    return None


# =============================================================================
# ИЗВЛЕЧЕНИЕ EXIF
# =============================================================================

def _fmt(value) -> str:
    """Форматирует дробное EXIF-значение."""
    if isinstance(value, tuple) and len(value) == 2:
        try:
            return f"{float(value[0]) / float(value[1]):.4g}"
        except Exception:
            return f"{value[0]}/{value[1]}"
    return str(value)


def _fill_from_piexif(result: ExifData, exif_bytes: bytes) -> None:
    """
    Дополняет ExifData из сырых EXIF-байт через piexif.
    Нужно для HEIC, где img.getexif() может не вернуть ExifIFD-поля.
    """
    if not PIEXIF_SUPPORTED:
        return
    try:
        exif_dict = piexif.load(exif_bytes)

        def tag_str(ifd: dict, tag_id: int) -> Optional[str]:
            v = ifd.get(tag_id)
            if v is None:
                return None
            if isinstance(v, bytes):
                return v.decode("utf-8", errors="ignore").strip("\x00").strip()
            return str(v)

        ifd0  = exif_dict.get("0th", {})
        exif  = exif_dict.get("Exif", {})

        if not result.make:
            result.make  = tag_str(ifd0, piexif.ImageIFD.Make)
        if not result.model:
            result.model = tag_str(ifd0, piexif.ImageIFD.Model)
        if not result.software:
            result.software = tag_str(ifd0, piexif.ImageIFD.Software)
        if not result.datetime_original:
            v = tag_str(exif, piexif.ExifIFD.DateTimeOriginal)
            if v:
                result.datetime_original = v
        if not result.lens_model:
            result.lens_model = tag_str(exif, piexif.ExifIFD.LensModel)

        if not result.iso:
            iso = exif.get(piexif.ExifIFD.ISOSpeedRatings) or exif.get(piexif.ExifIFD.PhotographicSensitivity)
            if iso:
                result.iso = str(iso)

        if not result.fnumber:
            fn = exif.get(piexif.ExifIFD.FNumber)
            if fn:
                result.fnumber = f"f/{_fmt(fn)}"

        if not result.exposure_time:
            exp = exif.get(piexif.ExifIFD.ExposureTime)
            if exp:
                result.exposure_time = _fmt(exp)

        if not result.focal_length:
            focal = exif.get(piexif.ExifIFD.FocalLength)
            if focal:
                result.focal_length = f"{_fmt(focal)} мм"

        if result.flash is None:
            result.flash = exif.get(piexif.ExifIFD.Flash)

        if result.white_balance is None:
            result.white_balance = exif.get(piexif.ExifIFD.WhiteBalance)

        if not result.width:
            result.width  = exif.get(piexif.ExifIFD.PixelXDimension)
        if not result.height:
            result.height = exif.get(piexif.ExifIFD.PixelYDimension)

        if not result.orientation:
            ori = ifd0.get(piexif.ImageIFD.Orientation)
            if ori:
                result.orientation = str(ori)

    except Exception as e:
        logger.debug(f"_fill_from_piexif error: {e}")


def extract_exif(image_bytes: bytes) -> ExifData:
    """Извлекает EXIF из байтов изображения (JPEG, PNG, HEIC, TIFF, WebP)."""
    result = ExifData()
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            fmt = img.format or ""
            is_heic = fmt.upper() in ("HEIF", "HEIC")
            logger.info(f"Формат: {fmt}, HEIC: {is_heic}")

            # ── Стандартный путь: getexif() + _getexif() ──────────────────────
            raw_exif = img.getexif()
            full: dict = {}
            try:
                raw2 = img._getexif()  # type: ignore[attr-defined]
                if raw2:
                    full = raw2
            except Exception:
                pass

            combined: dict = dict(raw_exif) if raw_exif else {}
            combined.update(full)

            # Именованный словарь
            named: dict = {
                TAGS.get(tid, str(tid)): v
                for tid, v in combined.items()
                if v is not None
            }

            if named:
                result.has_exif        = True
                result.make            = named.get("Make")
                result.model           = named.get("Model")
                result.datetime_original = named.get("DateTimeOriginal")
                if not result.datetime_original:
                    result.datetime    = named.get("DateTime")
                result.software        = named.get("Software")
                result.orientation     = named.get("Orientation")
                result.lens_model      = named.get("LensModel")
                result.width           = named.get("ExifImageWidth")  or named.get("ImageWidth")
                result.height          = named.get("ExifImageHeight") or named.get("ImageLength")
                result.flash           = named.get("Flash")
                result.white_balance   = named.get("WhiteBalance")

                exp = named.get("ExposureTime")
                if exp:
                    result.exposure_time = _fmt(exp)
                fn = named.get("FNumber")
                if fn:
                    result.fnumber = f"f/{_fmt(fn)}"
                iso = named.get("ISOSpeedRatings") or named.get("PhotographicSensitivity")
                if iso:
                    result.iso = str(iso)
                focal = named.get("FocalLength")
                if focal:
                    result.focal_length = f"{_fmt(focal)} мм"

                # GPS стандартным способом
                result.gps = _gps_from_dict(combined.get(34853, {})) if 34853 in combined else None

            # ── HEIC: дополнительный путь через pillow-heif + piexif ──────────
            if is_heic and HEIF_SUPPORTED:
                # GPS
                if not result.gps:
                    result.gps = _extract_gps_heic(image_bytes)

                # Остальные поля — через piexif если что-то не заполнено
                if PIEXIF_SUPPORTED:
                    try:
                        hf = open_heif(BytesIO(image_bytes))
                        for hi in hf:
                            exif_bytes = (getattr(hi, "info", {}) or {}).get("exif")
                            if exif_bytes:
                                _fill_from_piexif(result, exif_bytes)
                                result.has_exif = True
                                break
                    except Exception as e:
                        logger.debug(f"HEIC piexif fill error: {e}")

            # Если хоть что-то есть — считаем что EXIF есть
            if not result.has_exif and any([
                result.make, result.model, result.gps,
                result.datetime_original, result.datetime
            ]):
                result.has_exif = True

    except Exception as e:
        logger.error(f"extract_exif error: {e}", exc_info=True)
        raise

    return result


# =============================================================================
# ФОРМАТИРОВАНИЕ
# =============================================================================

def format_exif(data: ExifData) -> Optional[str]:
    if not data.has_exif:
        return None

    lines = ["<b>📷 EXIF-метаданные фотографии</b>\n"]

    if data.make or data.model:
        parts = [x.strip() for x in [data.make, data.model] if x]
        lines.append(f"🔹 <b>Устройство:</b> {' '.join(parts)}")

    if data.datetime_original:
        lines.append(f"🔹 <b>Дата съёмки:</b> {data.datetime_original}")
    elif data.datetime:
        lines.append(f"🔹 <b>Дата:</b> {data.datetime}")

    if data.gps:
        lat, lon = data.gps
        lines.append(f"🔹 <b>GPS:</b> <code>{lat}, {lon}</code>")
        lines.append(f"   <a href='https://maps.google.com/?q={lat},{lon}'>📍 Google Maps</a>")

    params = []
    if data.exposure_time:
        params.append(f"⏱ {data.exposure_time}с")
    if data.fnumber:
        params.append(f"🔆 {data.fnumber}")
    if data.iso:
        params.append(f"📊 ISO {data.iso}")
    if data.focal_length:
        params.append(f"🔭 {data.focal_length}")
    if params:
        lines.append(f"🔹 <b>Параметры:</b> {' │ '.join(params)}")

    if data.lens_model:
        lines.append(f"🔹 <b>Объектив:</b> {data.lens_model}")
    if data.width and data.height:
        lines.append(f"🔹 <b>Разрешение:</b> {data.width} × {data.height} px")
    if data.flash is not None:
        lines.append(f"🔹 <b>Вспышка:</b> {'сработала ⚡' if int(data.flash) & 1 else 'не использовалась'}")
    if data.white_balance is not None:
        lines.append(f"🔹 <b>Баланс белого:</b> {'ручной' if data.white_balance == 1 else 'авто'}")
    if data.software:
        lines.append(f"🔹 <b>Редактор:</b> {data.software}")
    if data.orientation:
        lines.append(f"🔹 <b>Ориентация:</b> {data.orientation}")

    return "\n".join(lines)


# =============================================================================
# УДАЛЕНИЕ EXIF
# =============================================================================

def remove_exif(image_bytes: bytes) -> tuple:
    """Удаляет EXIF. HEIC конвертируется в JPEG."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            had  = bool(img.getexif()) and len(img.getexif()) > 0
            fmt  = (img.format or "JPEG").upper()

            # HEIC → конвертируем в JPEG (Pillow не умеет сохранять HEIC)
            if fmt in ("HEIF", "HEIC"):
                rgb = img.convert("RGB")
                buf = BytesIO()
                rgb.save(buf, format="JPEG", quality=95, subsampling=0)
                buf.seek(0)
                return buf.getvalue(), True

            # Остальные форматы — пересоздаём без метаданных
            clean = Image.new(img.mode, img.size)
            clean.putdata(list(img.getdata()))
            buf = BytesIO()
            if fmt == "JPEG":
                clean.save(buf, format="JPEG", quality=95, subsampling=0)
            elif fmt == "PNG":
                clean.save(buf, format="PNG", compress_level=1)
            else:
                clean.save(buf, format=fmt)
            buf.seek(0)
            return buf.getvalue(), had

    except Exception as e:
        logger.error(f"remove_exif error: {e}", exc_info=True)
        raise


# =============================================================================
# СКАЧИВАНИЕ
# =============================================================================

async def download_by_id(file_id: str, retries: int = 3) -> bytes:
    for attempt in range(1, retries + 1):
        try:
            tg_file = await bot.get_file(file_id)
            buf: BytesIO = await bot.download(tg_file)
            buf.seek(0)
            data = buf.read()
            logger.info(f"Downloaded {len(data):,} bytes  path={tg_file.file_path}")
            return data
        except Exception as e:
            if attempt >= retries:
                raise
            logger.warning(f"Download attempt {attempt}/{retries} failed: {e}")
            await asyncio.sleep(attempt)
    raise RuntimeError("download failed")


# =============================================================================
# КЛАВИАТУРЫ
# =============================================================================

def kb_remove(user_id: int, file_id: str) -> InlineKeyboardMarkup:
    file_storage[str(user_id)] = file_id
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить метаданные", callback_data=f"remove:{user_id}")
    ]])


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❓ Что такое EXIF?", callback_data="what_is_exif"),
        InlineKeyboardButton(text="📖 Помощь",          callback_data="help"),
    ]])


# =============================================================================
# КОМАНДЫ
# =============================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привет! Я EXIF Tool Bot</b>\n\n"
        "Показываю скрытые метаданные фотографий и умею их удалять.\n\n"
        "<b>Что покажу:</b>\n"
        "• 📷 Модель камеры / смартфона\n"
        "• 📅 Дату и время съёмки\n"
        "• 📍 GPS-координаты\n"
        "• ⚙️ Выдержка, диафрагма, ISO\n\n"
        "⚠️ <b>Важно:</b> отправляй фото <b>как файл</b> (📎 → Файл) —\n"
        "иначе Telegram сожмёт его и удалит все метаданные.",
        reply_markup=kb_start(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1️⃣ Нажми 📎 → <b>Файл</b> → выбери фото\n"
        "2️⃣ Бот покажет всю информацию из EXIF\n"
        "3️⃣ Нажми <b>«Удалить метаданные»</b> — получи чистое фото\n\n"
        "✅ Поддерживаются: JPEG, PNG, TIFF, WebP, HEIC\n"
        "🔒 Фото не сохраняются на сервере",
        parse_mode="HTML",
    )


# =============================================================================
# ОБРАБОТЧИК ФОТО / ДОКУМЕНТОВ
# =============================================================================

@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_media(message: types.Message):
    if message.photo:
        file_id = message.photo[-1].file_id
        await message.answer(
            "⚠️ <b>Фото отправлено со сжатием.</b>\n"
            "Telegram удаляет метаданные. Отправь <b>как файл</b>: 📎 → Файл",
            parse_mode="HTML",
        )
    else:
        file_id = message.document.file_id

    status = await message.answer("⏳ Читаю метаданные…")
    try:
        image_bytes = await download_by_id(file_id)
        exif_data   = extract_exif(image_bytes)
        exif_text   = format_exif(exif_data)

        if exif_text:
            await message.answer(
                exif_text,
                reply_markup=kb_remove(message.from_user.id, file_id),
                parse_mode="HTML",
            )
        else:
            tip = "\n\n💡 Попробуй отправить <b>как файл</b> (📎 → Файл)." if message.photo else ""
            await message.answer(
                "ℹ️ <b>Метаданные EXIF не найдены.</b>\n"
                "Они уже удалены или фото изначально без EXIF." + tip,
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(f"handle_media error: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Не удалось обработать файл.</b>\n"
            "Убедись, что это JPEG / PNG / TIFF / WebP / HEIC и попробуй снова.",
            parse_mode="HTML",
        )
    finally:
        await status.delete()


# =============================================================================
# CALLBACKS
# =============================================================================

@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(callback: types.CallbackQuery):
    user_id = callback.data.split(":", 1)[1]
    file_id = file_storage.get(user_id)

    if not file_id:
        await callback.answer("⚠️ Сессия истекла. Отправь фото ещё раз.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer("⏳ Удаляю метаданные…")
    try:
        image_bytes             = await download_by_id(file_id)
        cleaned_bytes, had_exif = remove_exif(image_bytes)

        caption = (
            "✅ <b>Готово! Метаданные удалены.</b>\n"
            "GPS, дата и информация о камере больше не содержатся в файле."
            if had_exif else
            "ℹ️ <b>Метаданных и так не было.</b> Файл отправлен без изменений."
        )
        await callback.message.answer_document(
            document=BufferedInputFile(cleaned_bytes, filename="clean_photo.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
        logger.info(f"EXIF removed for user {user_id}")

    except Exception as e:
        logger.error(f"cb_remove error: {e}", exc_info=True)
        await callback.message.answer(
            "❌ <b>Ошибка при удалении метаданных.</b>\n"
            "Попробуй отправить фото заново.",
            parse_mode="HTML",
        )
    finally:
        await status.delete()
        file_storage.pop(user_id, None)


@dp.callback_query(F.data == "help")
async def cb_help(callback: types.CallbackQuery):
    await callback.message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1️⃣ 📎 → <b>Файл</b> → выбери фото\n"
        "2️⃣ Бот покажет EXIF-данные\n"
        "3️⃣ Нажми <b>«Удалить метаданные»</b> для очистки\n\n"
        "🔒 Фото не сохраняются на сервере",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "what_is_exif")
async def cb_what_is_exif(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔍 <b>Что такое EXIF?</b>\n\n"
        "EXIF — скрытые метаданные, которые камера / смартфон\n"
        "автоматически записывает в каждое фото:\n\n"
        "• 📷 Модель устройства\n"
        "• 📅 Точные дата и время\n"
        "• 📍 GPS-координаты места съёмки\n"
        "• ⚙️ Выдержка, диафрагма, ISO\n"
        "• 🛠 Программа редактирования\n\n"
        "⚠️ Публикуя фото с EXIF, ты можешь случайно раскрыть своё местоположение!",
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# ЗАПУСК С АВТО-ПЕРЕПОДКЛЮЧЕНИЕМ
# =============================================================================

async def main():
    logger.info("🚀 EXIF Tool Bot запускается…")
    logger.info(f"   pillow-heif: {'✅' if HEIF_SUPPORTED else '❌ не установлен'}")
    logger.info(f"   piexif:      {'✅' if PIEXIF_SUPPORTED else '❌ не установлен'}")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Polling упал: {e}. Переподключение через 5 сек…")
            await asyncio.sleep(5)
    await bot.session.close()
    logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass