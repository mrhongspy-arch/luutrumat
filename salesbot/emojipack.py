"""Custom emoji pack builder: turns logo images into a Telegram custom emoji set.

The set is created by the bot but owned by an admin account (the one with
Telegram Premium), so the logos can be used on buttons and in messages.
"""

import asyncio
import io
import re
import unicodedata
from pathlib import PurePath

from PIL import Image, UnidentifiedImageError
from telegram import InputFile, InputSticker
from telegram.constants import StickerFormat, StickerType
from telegram.error import BadRequest, RetryAfter

SIZE = 100          # custom emoji must be exactly 100x100
MAX_PACK = 200      # Telegram's limit for custom emoji sets
MAX_INITIAL = 50    # createNewStickerSet accepts at most 50 stickers


def to_emoji_png(data: bytes) -> bytes:
    """Fit any image into a transparent 100x100 PNG, keeping its proportions."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("không đọc được ảnh") from e
    image = image.convert("RGBA")
    image.thumbnail((SIZE, SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(image, ((SIZE - image.width) // 2, (SIZE - image.height) // 2), image)
    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def logo_name(filename: str) -> str:
    """'chat_gpt.png' -> 'chat gpt'."""
    return re.sub(r"[_\-]+", " ", PurePath(filename).stem).strip()


def match_key(name: str) -> str:
    """Loose key so 'Chat GPT', 'chatgpt' and 'ChatGPT' match."""
    text = unicodedata.normalize("NFKD", name.lower())
    return "".join(c for c in text if c.isalnum())


def pack_name(owner_id: int, bot_username: str) -> str:
    # Must start with a letter and end with _by_<bot username>.
    return f"logo{owner_id}_by_{bot_username}"


async def _call(func, *args, **kwargs):
    """Call the Bot API, waiting out flood limits."""
    for _ in range(5):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return await func(*args, **kwargs)


async def add_logos(bot, owner_id: int, pack: str, title: str, logos: list[tuple[str, bytes, str]]) -> list[tuple[str, str]]:
    """Add (name, png, fallback_emoji) logos to the pack, creating it if needed.

    Returns [(name, custom_emoji_id)] in the same order."""
    try:
        existing = await _call(bot.get_sticker_set, pack)
        before = len(existing.stickers)
    except BadRequest:  # STICKERSET_INVALID: not created yet
        existing, before = None, 0
    if before + len(logos) > MAX_PACK:
        raise ValueError(f"Bộ emoji chỉ chứa tối đa {MAX_PACK} logo (đang có {before}).")

    stickers = []
    for name, png, emoji in logos:
        uploaded = await _call(
            bot.upload_sticker_file, owner_id, InputFile(png, filename=f"{match_key(name) or 'logo'}.png"),
            StickerFormat.STATIC,
        )
        stickers.append(InputSticker(uploaded.file_id, [emoji], StickerFormat.STATIC))

    if existing is None:
        await _call(
            bot.create_new_sticker_set, owner_id, pack, title, stickers[:MAX_INITIAL],
            sticker_type=StickerType.CUSTOM_EMOJI,
        )
        stickers = stickers[MAX_INITIAL:]
    for sticker in stickers:
        await _call(bot.add_sticker_to_set, owner_id, pack, sticker)

    added = (await _call(bot.get_sticker_set, pack)).stickers[before:before + len(logos)]
    return [(name, sticker.custom_emoji_id) for (name, _, _), sticker in zip(logos, added)]
