"""Telegram auto-sales bot: catalog, orders, VietQR payment, auto delivery."""

import asyncio
import logging
import re
import socket
from datetime import datetime, time as dtime
from html import escape
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .backup import make_backup, prune
from .config import Config, load_config
from .db import Database

log = logging.getLogger(__name__)

BTN_BUY = "🛒 Mua hàng"
BTN_HISTORY = "📋 Lịch sử"
BTN_CONTACT = "📞 Liên hệ"
BTN_WALLET = "💳 Ví của tôi"
MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_BUY, BTN_HISTORY], [BTN_CONTACT, BTN_WALLET]], resize_keyboard=True, is_persistent=True
)
# Buttons of the previous menu, still on some customers' screens.
OLD_BTN_PRODUCTS = "🛍 Sản phẩm"
OLD_BTN_ORDERS = "📦 Đơn hàng của tôi"
OLD_BTN_SUPPORT = "💬 Hỗ trợ"
TOPUP_AMOUNTS = (50_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000)
MIN_TOPUP, MAX_TOPUP = 10_000, 50_000_000
MAX_QTY_BUTTONS = 5
STATUS_LABEL = {"pending": "⏳ Chờ thanh toán", "paid": "✅ Đã giao", "cancelled": "❌ Đã huỷ"}


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


def db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_user is not None and update.effective_user.id in cfg(context).admin_ids


def qr_url(config: Config, amount: int, note: str) -> str:
    return (
        f"https://img.vietqr.io/image/{quote(config.bank_code)}-{quote(config.bank_account)}"
        f"-compact2.png?amount={amount}&addInfo={quote(note)}&accountName={quote(config.bank_account_name)}"
    )


# ---------------------------------------------------------------- customer

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember everyone who talks to the bot privately so announcements can reach them."""
    user, chat = update.effective_user, update.effective_chat
    if user and chat and chat.type == "private":
        db(context).upsert_user(user.id, user.username, user.first_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = escape(update.effective_user.first_name or "bạn")
    await update.message.reply_text(
        f"👋 Xin chào <b>{name}</b>!\nChào mừng đến với cửa hàng. Chọn chức năng ở menu bên dưới 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )
    # Deep link from a channel post: t.me/<bot>?start=p<product_id>
    if context.args and re.fullmatch(r"p\d+", context.args[0]):
        product_id = int(context.args[0][1:])
        if product_view(context, product_id, False):
            await show_view(update, lambda icons: product_view(context, product_id, icons), reply=True)
            return
    await show_products(update, context)


async def legacy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons of the old reply keyboard: hand out the new menu, then do what was asked."""
    await update.message.reply_text("🔄 Menu đã được cập nhật 👇", reply_markup=MAIN_MENU)
    target = {OLD_BTN_PRODUCTS: show_products, OLD_BTN_ORDERS: show_history, OLD_BTN_SUPPORT: show_contact}
    await target[update.message.text](update, context)


# ---------------------------------------------------------------- views
# Each *_view returns (html_text, inline_markup). ``icons`` says whether custom
# emoji logos may be used; show_view retries with icons=False if Telegram refuses.

OTHER_CATEGORY = 0  # callback id for products that are not in any category


def logo_button(label: str, callback: str, icons: bool, *sources) -> InlineKeyboardButton:
    """Button with the first available logo (custom emoji) among ``sources``,
    or, without logos, the first plain emoji."""
    sources = [s for s in sources if s is not None]
    icon_id = next((s["icon_id"] for s in sources if s["icon_id"]), "")
    if icons and icon_id:
        return InlineKeyboardButton(label, callback_data=callback, icon_custom_emoji_id=icon_id)
    plain = next((s["emoji"] for s in sources if s["emoji"]), "")
    return InlineKeyboardButton(f"{plain} {label}" if plain else label, callback_data=callback)


def logo_html(icons: bool, *sources) -> str:
    """Logo to put in front of a title inside a message."""
    sources = [s for s in sources if s is not None]
    plain = next((s["emoji"] for s in sources if s["emoji"]), "")
    icon_id = next((s["icon_id"] for s in sources if s["icon_id"]), "")
    if icons and icon_id:
        return f'<tg-emoji emoji-id="{icon_id}">{plain or "⭐"}</tg-emoji> '
    return f"{plain} " if plain else ""


def in_pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def back_button(callback: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("◀️ Quay lại", callback_data=callback)]


def home_view(context: ContextTypes.DEFAULT_TYPE, icons: bool):
    buttons = [
        InlineKeyboardButton(BTN_BUY, callback_data="list"),
        InlineKeyboardButton(BTN_HISTORY, callback_data="hist"),
        InlineKeyboardButton(BTN_CONTACT, callback_data="contact"),
        InlineKeyboardButton(BTN_WALLET, callback_data="wallet"),
    ]
    return "🏠 <b>Menu chính</b>\nChọn chức năng:", InlineKeyboardMarkup(in_pairs(buttons))


def product_label(context: ContextTypes.DEFAULT_TYPE, product) -> str:
    return f"{product['name']} — {money(product['price'])} (còn {db(context).sellable_quantity(product['id'])})"


def catalog_view(context: ContextTypes.DEFAULT_TYPE, icons: bool):
    """Category menu, or a flat product list when no categories are set up."""
    database = db(context)
    categories = database.list_categories()
    if not categories:
        products = database.list_products()
        rows = [[logo_button(product_label(context, p), f"prod:{p['id']}", icons, p)] for p in products]
        rows.append(back_button("home"))
        text = "🛍 <b>Danh sách sản phẩm</b>\nChọn sản phẩm để xem chi tiết:" if products else "Hiện chưa có sản phẩm nào."
        return text, InlineKeyboardMarkup(rows)
    buttons = []
    for c in categories:
        products = database.products_in_category(c["id"])
        if products:
            stock = sum(database.sellable_quantity(p["id"]) for p in products)
            buttons.append(logo_button(f"{c['name']} ({stock})", f"cat:{c['id']}", icons, c))
    others = database.products_in_category(None)
    if others:
        stock = sum(database.sellable_quantity(p["id"]) for p in others)
        buttons.append(InlineKeyboardButton(f"📦 Khác ({stock})", callback_data=f"cat:{OTHER_CATEGORY}"))
    rows = in_pairs(buttons) + [back_button("home")]
    text = "📂 <b>Chọn nhóm sản phẩm:</b>" if buttons else "Hiện chưa có sản phẩm nào."
    return text, InlineKeyboardMarkup(rows)


def category_view(context: ContextTypes.DEFAULT_TYPE, category_id: int, icons: bool):
    database = db(context)
    category = database.get_category(category_id) if category_id != OTHER_CATEGORY else None
    products = database.products_in_category(None if category is None else category_id)
    title = (logo_html(icons, category) + escape(category["name"])) if category else "📦 Khác"
    rows = [[logo_button(product_label(context, p), f"prod:{p['id']}", icons, p, category)] for p in products]
    rows.append(back_button("list"))
    hint = "Chọn sản phẩm để xem chi tiết:" if products else "Nhóm này chưa có sản phẩm."
    return f"<b>{title}</b>\n{hint}", InlineKeyboardMarkup(rows)


def product_view(context: ContextTypes.DEFAULT_TYPE, product_id: int, icons: bool):
    """Text and buttons for one product, or None if it is gone or hidden."""
    database = db(context)
    product = database.get_product(product_id)
    if product is None or not product["active"]:
        return None
    category = database.get_category(product["category_id"]) if product["category_id"] else None
    available = database.sellable_quantity(product_id)
    text = (
        f"{logo_html(icons, product, category)}<b>{escape(product['name'])}</b>\n\n{escape(product['description'])}\n\n"
        f"💰 Giá: <b>{money(product['price'])}</b>\n📦 Còn lại: <b>{available}</b>"
    )
    rows = []
    if available > 0:
        rows.append([
            InlineKeyboardButton(f"Mua {q}", callback_data=f"buy:{product_id}:{q}")
            for q in range(1, min(available, MAX_QTY_BUTTONS) + 1)
        ])
    else:
        text += "\n\n⚠️ Sản phẩm tạm hết hàng."
    back = "list"
    if product["category_id"]:
        back = f"cat:{product['category_id']}"
    elif database.list_categories():
        back = f"cat:{OTHER_CATEGORY}"
    rows.append(back_button(back))
    return text, InlineKeyboardMarkup(rows)


def history_view(context: ContextTypes.DEFAULT_TYPE, user_id: int, icons: bool):
    orders = db(context).list_user_orders(user_id)
    if not orders:
        return "📋 <b>Lịch sử mua hàng</b>\nBạn chưa có đơn hàng nào.", InlineKeyboardMarkup([back_button("home")])
    lines = ["📋 <b>Lịch sử mua hàng</b>"]
    rows = []
    for o in orders:
        lines.append(
            f"• <code>{o['code']}</code> — {escape(o['product_name'])} × {o['quantity']} — "
            f"{money(o['amount'])} — {STATUS_LABEL[o['status']]}"
        )
        if o["status"] == "paid":
            rows.append([InlineKeyboardButton(f"📥 Xem lại hàng {o['code']}", callback_data=f"items:{o['id']}")])
    rows.append(back_button("home"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def contact_view(context: ContextTypes.DEFAULT_TYPE, icons: bool):
    contact = escape(cfg(context).support_contact or "admin")
    return f"📞 <b>Liên hệ</b>\nCần hỗ trợ? Nhắn cho {contact}", InlineKeyboardMarkup([back_button("home")])


TX_LABEL = {"deposit": "Nạp tiền", "purchase": "Mua hàng", "adjust": "Điều chỉnh"}


def wallet_view(context: ContextTypes.DEFAULT_TYPE, user_id: int, icons: bool):
    database = db(context)
    lines = [f"💳 <b>Ví của tôi</b>\nSố dư: <b>{money(database.get_balance(user_id))}</b>"]
    history = database.wallet_history(user_id, 5)
    if history:
        lines.append("\n<b>Giao dịch gần đây</b>")
        for tx in history:
            sign = "+" if tx["amount"] > 0 else "−"
            lines.append(f"• {sign}{money(abs(tx['amount']))} — {TX_LABEL.get(tx['kind'], tx['kind'])} {escape(tx['ref'])}")
    lines.append("\nDùng số dư để mua hàng ngay, không cần chuyển khoản từng đơn.")
    rows = [[InlineKeyboardButton("➕ Nạp tiền", callback_data="topup")], back_button("home")]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def topup_view(context: ContextTypes.DEFAULT_TYPE, icons: bool):
    buttons = [InlineKeyboardButton(money(a), callback_data=f"dep:{a}") for a in TOPUP_AMOUNTS]
    text = (
        "➕ <b>Nạp tiền vào ví</b>\nChọn số tiền muốn nạp:\n\n"
        f"Hoặc gõ <code>/nap SỐ_TIỀN</code> để nạp số khác (tối thiểu {money(MIN_TOPUP)}), ví dụ <code>/nap 150000</code>"
    )
    return text, InlineKeyboardMarkup(in_pairs(buttons) + [back_button("wallet")])


async def show_view(update: Update, build, reply: bool = False) -> None:
    """Show ``build(icons)`` by editing the pressed message, or as a new message.

    Custom-emoji logos need the bot owner to have Telegram Premium; if Telegram
    refuses them, the same view is sent again with plain emoji."""
    for icons in (True, False):
        text, markup = build(icons)
        try:
            if update.callback_query and not reply:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            if "no text in the message" in str(e).lower():  # pressed under a photo: answer in a new message
                reply = True
                continue
            if not icons:
                raise
            log.info("Custom emoji refused, falling back to plain emoji: %s", e)


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_view(update, lambda icons: catalog_view(context, icons))


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_view(update, lambda icons: history_view(context, update.effective_user.id, icons))


async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_view(update, lambda icons: contact_view(context, icons))


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_view(update, lambda icons: wallet_view(context, update.effective_user.id, icons))


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, reply: bool = False) -> None:
    if product_view(context, product_id, False) is None:
        await update.effective_message.reply_text("Sản phẩm không còn tồn tại.")
        return
    await show_view(update, lambda icons: product_view(context, product_id, icons), reply=reply)


def payment_caption(config: Config, title: str, code: str, amount: int, details: str) -> str:
    return (
        f"🧾 <b>{title} {code}</b>\n{details}"
        f"Số tiền: <b>{money(amount)}</b>\n\n"
        f"🏦 Ngân hàng: <b>{escape(config.bank_code)}</b>\n"
        f"💳 STK: <code>{escape(config.bank_account)}</code>\n"
        f"👤 Chủ TK: {escape(config.bank_account_name)}\n"
        f"📝 Nội dung CK: <code>{code}</code>\n\n"
        f"Quét mã QR hoặc chuyển khoản đúng <b>số tiền</b> và <b>nội dung</b>.\n"
        f"⏰ Tự huỷ sau {config.order_timeout_minutes} phút nếu chưa thanh toán."
    )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, qty: int) -> None:
    query = update.callback_query
    user = update.effective_user
    config = cfg(context)
    order = db(context).create_order(config.order_prefix, user.id, user.username, product_id, qty)
    if order is None:
        await query.answer("Không đủ hàng, vui lòng chọn số lượng khác.", show_alert=True)
        await show_product(update, context, product_id)
        return
    await query.answer()
    caption = payment_caption(
        config, "Đơn hàng", order["code"], order["amount"],
        f"Sản phẩm: {escape(order['product_name'])} × {qty}\n",
    ) + "\nHàng sẽ được gửi tự động ngay khi thanh toán được xác nhận."
    rows = []
    balance = db(context).get_balance(user.id)
    if balance >= order["amount"]:
        rows.append([InlineKeyboardButton(f"💳 Trả bằng ví (số dư {money(balance)})", callback_data=f"payw:{order['id']}")])
    rows.append([InlineKeyboardButton("❌ Huỷ đơn", callback_data=f"cancel:{order['id']}")])
    await query.message.reply_photo(
        qr_url(config, order["amount"], order["code"]),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await notify_admins_new_order(context.application, order)


async def pay_with_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    query = update.callback_query
    items, error = db(context).pay_with_wallet(order_id, update.effective_user.id)
    if items is None:
        message = {
            "not_enough": "Số dư ví không đủ. Hãy nạp thêm hoặc chuyển khoản theo mã QR.",
            "out_of_stock": "Sản phẩm vừa hết hàng, đơn chưa bị trừ tiền.",
        }.get(error, "Đơn này không còn chờ thanh toán.")
        await query.answer(message, show_alert=True)
        return
    await query.answer("Thanh toán thành công!")
    order = db(context).get_order(order_id)
    await query.edit_message_caption(
        f"✅ Đơn {order['code']} đã thanh toán bằng ví.\n"
        f"Số dư còn lại: {money(db(context).get_balance(order['user_id']))}"
    )
    await send_items(context.application, order, items)
    await report_to_admins(
        context.application, f"✅ Đã giao đơn {order['code']} ({money(order['amount'])}) — trả bằng ví."
    )


async def create_topup(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> None:
    user = update.effective_user
    config = cfg(context)
    deposit = db(context).create_deposit(config.deposit_prefix, user.id, user.username, amount)
    caption = payment_caption(config, "Nạp tiền", deposit["code"], amount, "") + (
        "\nTiền sẽ được cộng vào ví ngay khi nhận được chuyển khoản."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Huỷ nạp", callback_data=f"depx:{deposit['id']}")]])
    await update.effective_message.reply_photo(
        qr_url(config, amount, deposit["code"]), caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    buyer = f"@{user.username}" if user.username else str(user.id)
    admin_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Đã nhận tiền", callback_data=f"dok:{deposit['id']}"),
        InlineKeyboardButton("❌ Huỷ", callback_data=f"dno:{deposit['id']}"),
    ]])
    for admin_id in config.admin_ids:
        await context.bot.send_message(
            admin_id,
            f"💳 Yêu cầu nạp tiền <code>{deposit['code']}</code> — <b>{money(amount)}</b>\nKhách: {escape(buyer)}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_markup,
        )


async def topup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    digits = re.sub(r"\D", "", " ".join(context.args))
    if not digits or not MIN_TOPUP <= int(digits) <= MAX_TOPUP:
        await update.message.reply_text(
            f"Cách dùng: /nap SỐ_TIỀN (từ {money(MIN_TOPUP)} đến {money(MAX_TOPUP)}), ví dụ /nap 150000"
        )
        return
    await create_topup(update, context, int(digits))


async def credit_deposit(app: Application, deposit_id: int, received: int | None = None) -> bool:
    """Add a pending deposit to the customer's wallet and tell everyone."""
    database: Database = app.bot_data["db"]
    deposit = database.confirm_deposit(deposit_id, received)
    if deposit is None:
        return False
    balance = database.get_balance(deposit["user_id"])
    try:
        await app.bot.send_message(
            deposit["user_id"],
            f"✅ Nạp thành công <b>{money(deposit['credited'])}</b> (mã {deposit['code']}).\n"
            f"💳 Số dư ví: <b>{money(balance)}</b>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        log.warning("Could not notify user %s about deposit %s", deposit["user_id"], deposit["code"])
    await report_to_admins(app, f"💳 Đã cộng {money(deposit['credited'])} cho nạp {deposit['code']}.")
    return True


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, *args = query.data.split(":")
    simple = {
        "home": lambda: show_view(update, lambda icons: home_view(context, icons)),
        "list": lambda: show_products(update, context),
        "hist": lambda: show_history(update, context),
        "contact": lambda: show_contact(update, context),
        "wallet": lambda: show_wallet(update, context),
        "topup": lambda: show_view(update, lambda icons: topup_view(context, icons)),
    }
    if action in simple:
        await query.answer()
        await simple[action]()
    elif action == "cat":
        await query.answer()
        await show_view(update, lambda icons: category_view(context, int(args[0]), icons))
    elif action == "prod":
        await query.answer()
        await show_product(update, context, int(args[0]))
    elif action == "view":
        # "Mua ngay" under an announcement: open the product in a new message,
        # leaving the announcement itself untouched.
        await query.answer()
        await show_product(update, context, int(args[0]), reply=True)
    elif action == "buy":
        await buy(update, context, int(args[0]), int(args[1]))
    elif action == "payw":
        await pay_with_wallet(update, context, int(args[0]))
    elif action == "dep":
        await query.answer()
        await create_topup(update, context, int(args[0]))
    elif action == "depx":
        deposit = db(context).get_deposit(int(args[0]))
        if deposit and deposit["user_id"] == update.effective_user.id and db(context).cancel_deposit(deposit["id"]):
            await query.answer("Đã huỷ yêu cầu nạp.")
            await query.edit_message_caption(f"❌ Yêu cầu nạp {deposit['code']} đã huỷ.")
        else:
            await query.answer("Không thể huỷ yêu cầu này.", show_alert=True)
    elif action == "cancel":
        order = db(context).get_order(int(args[0]))
        if order and order["user_id"] == update.effective_user.id and db(context).cancel_order(order["id"]):
            await query.answer("Đã huỷ đơn.")
            await query.edit_message_caption(f"❌ Đơn {order['code']} đã bị huỷ.")
        else:
            await query.answer("Không thể huỷ đơn này.", show_alert=True)
    elif action == "items":
        order = db(context).get_order(int(args[0]))
        if order and order["user_id"] == update.effective_user.id and order["status"] == "paid":
            await query.answer()
            await send_items(context.application, order, db(context).delivered_items(order["id"]))
        else:
            await query.answer("Không tìm thấy đơn.", show_alert=True)
    elif action == "adm_ok" and is_admin(update, context):
        ok = await deliver(context.application, int(args[0]))
        await query.answer("Đã giao hàng." if ok else "Đơn không còn chờ thanh toán hoặc hết hàng.", show_alert=not ok)
        if ok:
            await query.edit_message_reply_markup(None)
    elif action == "adm_no" and is_admin(update, context):
        order = db(context).get_order(int(args[0]))
        if order and db(context).cancel_order(order["id"]):
            await context.bot.send_message(order["user_id"], f"❌ Đơn {order['code']} đã bị huỷ bởi cửa hàng.")
            await query.answer("Đã huỷ đơn.")
            await query.edit_message_reply_markup(None)
        else:
            await query.answer("Không thể huỷ đơn này.", show_alert=True)
    elif action == "dok" and is_admin(update, context):
        ok = await credit_deposit(context.application, int(args[0]))
        await query.answer("Đã cộng tiền vào ví." if ok else "Yêu cầu không còn chờ duyệt.", show_alert=not ok)
        if ok:
            await query.edit_message_reply_markup(None)
    elif action == "dno" and is_admin(update, context):
        deposit = db(context).get_deposit(int(args[0]))
        if deposit and db(context).cancel_deposit(deposit["id"]):
            await context.bot.send_message(deposit["user_id"], f"❌ Yêu cầu nạp {deposit['code']} đã bị huỷ bởi cửa hàng.")
            await query.answer("Đã huỷ.")
            await query.edit_message_reply_markup(None)
        else:
            await query.answer("Không thể huỷ yêu cầu này.", show_alert=True)
    else:
        await query.answer()


# ---------------------------------------------------------------- delivery

async def send_items(app: Application, order, items: list[str]) -> None:
    body = "\n".join(f"<code>{escape(item)}</code>" for item in items)
    await app.bot.send_message(
        order["user_id"],
        f"🎉 <b>Thanh toán thành công đơn {order['code']}</b>\n"
        f"Sản phẩm: {escape(order['product_name'])} × {order['quantity']}\n\n{body}\n\n"
        "Cảm ơn bạn đã mua hàng! 💖",
        parse_mode=ParseMode.HTML,
    )


async def deliver(app: Application, order_id: int) -> bool:
    """Mark the order paid and send the goods to the buyer."""
    database: Database = app.bot_data["db"]
    items = database.mark_paid(order_id)
    if items is None:
        return False
    order = database.get_order(order_id)
    await send_items(app, order, items)
    for admin_id in app.bot_data["config"].admin_ids:
        await app.bot.send_message(admin_id, f"✅ Đã giao đơn {order['code']} ({money(order['amount'])}).")
    return True


async def notify_admins_new_order(app: Application, order) -> None:
    buyer = f"@{order['username']}" if order["username"] else str(order["user_id"])
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Đã nhận tiền", callback_data=f"adm_ok:{order['id']}"),
        InlineKeyboardButton("❌ Huỷ", callback_data=f"adm_no:{order['id']}"),
    ]])
    for admin_id in app.bot_data["config"].admin_ids:
        await app.bot.send_message(
            admin_id,
            f"🆕 Đơn mới <code>{order['code']}</code>\n{escape(order['product_name'])} × {order['quantity']}"
            f" — <b>{money(order['amount'])}</b>\nKhách: {escape(buyer)}",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def expire_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for order in db(context).expire_orders(cfg(context).order_timeout_minutes):
        try:
            await context.bot.send_message(order["user_id"], f"⌛ Đơn {order['code']} đã hết hạn thanh toán và bị huỷ.")
        except Exception:  # user blocked the bot, etc.
            log.warning("Could not notify user %s about expired order", order["user_id"])
    for deposit in db(context).expire_deposits(cfg(context).order_timeout_minutes):
        try:
            await context.bot.send_message(deposit["user_id"], f"⌛ Yêu cầu nạp {deposit['code']} đã hết hạn và bị huỷ.")
        except Exception:
            log.warning("Could not notify user %s about expired deposit", deposit["user_id"])


# ---------------------------------------------------------------- announcements

async def broadcast(app: Application, send) -> tuple[int, int]:
    """Call ``send(chat_id)`` for every active user, respecting Telegram rate limits.

    Returns (delivered, failed). Users who blocked the bot are skipped next time.
    """
    database: Database = app.bot_data["db"]
    delivered = failed = 0
    for user_id in database.active_user_ids():
        for _ in range(2):
            try:
                await send(user_id)
                delivered += 1
            except RetryAfter as e:
                wait = e.retry_after
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                await asyncio.sleep(wait + 1)
                continue
            except Forbidden:
                database.set_user_blocked(user_id)
                failed += 1
            except TelegramError as e:
                log.warning("Broadcast to %s failed: %s", user_id, e)
                failed += 1
            break
        await asyncio.sleep(0.05)  # stay under ~30 messages/second
    return delivered, failed


async def report_to_admins(app: Application, text: str) -> None:
    for admin_id in app.bot_data["config"].admin_ids:
        await app.bot.send_message(admin_id, text)


# Icons in the restock notice. Admins can swap them for (animated) custom emoji
# with /setemoji; stored as HTML so <tg-emoji> tags survive.
EMOJI_SLOTS = {
    "badge": ("🔥 <b>TIN NÓNG</b>", "nhãn đầu tin (TIN NÓNG)"),
    "stock": ("📦", "biểu tượng dòng số lượng"),
    "price": ("💰", "biểu tượng dòng giá"),
}
TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)


def emoji(database: Database, slot: str) -> str:
    return database.get_setting(f"emoji_{slot}", EMOJI_SLOTS[slot][0])


def plain_emoji(html_text: str) -> str:
    """Replace custom emoji with their fallback characters (for chats that reject them)."""
    return TG_EMOJI_RE.sub(r"\1", html_text)


def product_logo(database: Database, product) -> str:
    if "icon_id" not in product.keys():  # sample products in previews
        return ""
    category = database.get_category(product["category_id"]) if product["category_id"] else None
    return logo_html(True, product, category)


def restock_text(database: Database, product, available: int) -> str:
    return (
        f"{emoji(database, 'badge')} {product_logo(database, product)}<b>{escape(product['name'])}</b> đã có hàng lại!\n\n"
        f"{emoji(database, 'stock')} Số lượng còn: <b>{available}</b>\n"
        f"{emoji(database, 'price')} Giá: <b>{money(product['price'])}</b>"
    )


async def send_html(bot, chat_id, text: str, **kwargs):
    """Send HTML, retrying without custom emoji if Telegram refuses them
    (they need the bot owner to have Telegram Premium)."""
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest:
        if not TG_EMOJI_RE.search(text):
            raise
        return await bot.send_message(chat_id, plain_emoji(text), parse_mode=ParseMode.HTML, **kwargs)


async def announce_product(app: Application, product_id: int) -> None:
    """Post a restock notice for a product to all users and the shop channel."""
    database: Database = app.bot_data["db"]
    config: Config = app.bot_data["config"]
    product = database.get_product(product_id)
    available = database.sellable_quantity(product_id)
    if product is None or not product["active"] or available == 0:
        return
    text = restock_text(database, product, available)
    if config.channel_id:
        link = f"https://t.me/{app.bot.username}?start=p{product_id}"
        try:
            await send_html(
                app.bot, config.channel_id, text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua ngay", url=link)]]),
            )
        except TelegramError as e:
            await report_to_admins(app, f"⚠️ Không đăng được lên kênh {config.channel_id}: {e}")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua ngay", callback_data=f"view:{product_id}")]])
    delivered, failed = await broadcast(
        app, lambda chat_id: send_html(app.bot, chat_id, text, reply_markup=markup)
    )
    await report_to_admins(app, f"📣 Đã báo có hàng {product['name']}: gửi {delivered} người, lỗi {failed}.")


def auto_restock_enabled(app: Application) -> bool:
    default = "1" if app.bot_data["config"].auto_restock_notify else "0"
    return app.bot_data["db"].get_setting("auto_restock", default) == "1"


def after_restock(app: Application, product_id: int, was_sold_out: bool) -> bool:
    """Announce a product that just came back in stock; True if an announcement started."""
    if not (was_sold_out and auto_restock_enabled(app)):
        return False
    if app.bot_data["db"].sellable_quantity(product_id) == 0:
        return False
    app.create_task(announce_product(app, product_id))
    return True


def uploads_dir(app: Application) -> Path:
    path = Path(app.bot_data["config"].db_path).resolve().parent / "uploads"
    path.mkdir(exist_ok=True)
    return path


async def send_post(app: Application, post, chat_ids: list[int] | None = None) -> tuple[int, int]:
    """Send a promotional post (text, optional image and Mua ngay button).

    Goes to every user, or only to ``chat_ids`` (used for test sends)."""
    markup = None
    if post["product_id"]:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 Mua ngay", callback_data=f"view:{post['product_id']}")]]
        )
    photo = uploads_dir(app) / post["image"] if post["image"] else None
    file_id = None  # upload the image once, then reuse Telegram's copy

    async def send(chat_id: int) -> None:
        nonlocal file_id
        if photo is None:
            await send_html(app.bot, chat_id, post["text"], reply_markup=markup)
            return
        message = await app.bot.send_photo(
            chat_id, file_id or photo, caption=post["text"], parse_mode=ParseMode.HTML, reply_markup=markup
        )
        file_id = file_id or message.photo[-1].file_id

    if chat_ids is None:
        return await broadcast(app, send)
    delivered = failed = 0
    for chat_id in chat_ids:
        try:
            await send(chat_id)
            delivered += 1
        except TelegramError as e:
            log.warning("Test post to %s failed: %s", chat_id, e)
            failed += 1
            raise
    return delivered, failed


async def run_due_posts(app: Application) -> None:
    database: Database = app.bot_data["db"]
    for post in database.claim_due_posts():
        try:
            delivered, failed = await send_post(app, post)
        except Exception:
            log.exception("Post %s failed", post["id"])
            delivered, failed = 0, len(database.active_user_ids())
        database.finish_post(post["id"], delivered, failed)
        await report_to_admins(app, f"📣 Đã gửi tin khuyến mãi #{post['id']}: {delivered} người nhận, lỗi {failed}.")


async def post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_due_posts(context.application)


# ---------------------------------------------------------------- backups

def backup_dir(app: Application) -> Path:
    config: Config = app.bot_data["config"]
    path = Path(config.backup_dir).expanduser()
    if not path.is_absolute():
        path = Path(config.db_path).resolve().parent / path
    return path


def create_backup(app: Application) -> Path:
    config: Config = app.bot_data["config"]
    path = make_backup(app.bot_data["db"], config.db_path, backup_dir(app))
    prune(backup_dir(app), config.backup_keep)
    return path


async def send_backup(app: Application, chat_ids) -> None:
    path = create_backup(app)
    for chat_id in chat_ids:
        with path.open("rb") as f:
            await app.bot.send_document(
                chat_id, f, filename=path.name,
                caption=f"💾 Bản sao lưu {datetime.now():%H:%M %d/%m/%Y}. Giữ kín: có token bot và dữ liệu khách.",
            )


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        create_backup(context.application)
    except Exception:
        log.exception("Backup failed")
        await report_to_admins(context.application, "⚠️ Sao lưu tự động bị lỗi, xem bot.log.")


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await send_backup(context.application, context.application.bot_data["config"].admin_ids)
    except Exception:
        log.exception("Daily backup failed")
        await report_to_admins(context.application, "⚠️ Không gửi được bản sao lưu hằng ngày, xem bot.log.")


# ---------------------------------------------------------------- admin

ADMIN_HELP = """<b>Lệnh quản trị</b>
/addproduct Tên | Giá | Mô tả — thêm sản phẩm
/addstock ID — rồi mỗi dòng tiếp theo là 1 hàng (tài khoản, key, link…)
/price ID GIÁ — đổi giá
/hide ID, /show ID — ẩn/hiện sản phẩm
/allproducts — xem tất cả sản phẩm và tồn kho
/orders [pending|paid|cancelled] — xem đơn
/notify ID — gửi thông báo "có hàng lại" của sản phẩm cho mọi khách
/broadcast [ID] — trả lời (reply) một tin nhắn/ảnh bằng lệnh này để gửi nó cho mọi khách; thêm ID để gắn nút 🛒 Mua ngay
/setemoji badge|stock|price EMOJI — đổi biểu tượng trong tin "có hàng lại" (hỗ trợ emoji động)
/confirm MÃ_ĐƠN — xác nhận đã thanh toán và giao hàng
/cancel MÃ_ĐƠN — huỷ đơn
/stats — thống kê doanh thu
/web — địa chỉ trang quản trị
/backup — gửi ngay bản sao lưu dữ liệu
/emojiid EMOJI — lấy mã của emoji động (dùng làm logo nhóm sản phẩm)"""


def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update, context):
            return
        try:
            await handler(update, context)
        except (ValueError, IndexError):
            await update.message.reply_text("Sai cú pháp. Gõ /admin để xem hướng dẫn.")
    return wrapper


@admin_only
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)


@admin_only
async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split("|")]
    name, price = parts[0], int(re.sub(r"\D", "", parts[1]))
    if not name:
        raise ValueError
    description = parts[2] if len(parts) > 2 else ""
    product_id = db(context).add_product(name, price, description)
    await update.message.reply_text(
        f"Đã thêm sản phẩm #{product_id}. Nạp hàng bằng:\n/addstock {product_id}\nhang_1\nhang_2"
    )


@admin_only
async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    first_line, _, rest = update.message.text.partition("\n")
    product_id = int(first_line.split()[1])
    items = [line.strip() for line in rest.splitlines() if line.strip()]
    if db(context).get_product(product_id) is None:
        await update.message.reply_text("Không tìm thấy sản phẩm.")
        return
    if not items:
        await update.message.reply_text("Hãy ghi mỗi hàng trên một dòng, sau dòng /addstock ID.")
        return
    was_sold_out = db(context).sellable_quantity(product_id) == 0
    count = db(context).add_stock(product_id, items)
    await update.message.reply_text(f"Đã nạp {count} hàng cho sản phẩm #{product_id}.")
    if after_restock(context.application, product_id, was_sold_out):
        await update.message.reply_text("📣 Sản phẩm vừa có hàng lại, đang gửi thông báo cho khách…")


@admin_only
async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    product_id = int(context.args[0])
    if db(context).sellable_quantity(product_id) == 0:
        await update.message.reply_text("Sản phẩm không tồn tại, đang ẩn hoặc đã hết hàng.")
        return
    await update.message.reply_text("📣 Đang gửi thông báo…")
    context.application.create_task(announce_product(context.application, product_id))


@admin_only
async def set_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    database = db(context)
    # text_html keeps custom emoji as <tg-emoji emoji-id="..."> tags.
    match = re.match(r"/\S+\s+(\w+)\s*(.*)", update.message.text_html, re.S)
    slot = match.group(1).lower() if match else ""
    if slot not in EMOJI_SLOTS:
        lines = [f"• <code>{name}</code> — {label}: {emoji(database, name)}" for name, (_, label) in EMOJI_SLOTS.items()]
        await update.message.reply_text(
            "Cách dùng: <code>/setemoji badge</code> rồi chèn emoji (có thể là emoji động).\n"
            "Gõ <code>/setemoji badge reset</code> để về mặc định.\n\nHiện tại:\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
        return
    value = match.group(2).strip()
    if not value:
        raise ValueError
    if value.lower() == "reset":
        database.delete_setting(f"emoji_{slot}")
    else:
        database.set_setting(f"emoji_{slot}", value)
    sample = {"name": "Sản phẩm mẫu", "price": 190000}
    preview = restock_text(database, sample, 4)
    try:
        await context.bot.send_message(update.effective_chat.id, preview, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        await update.message.reply_text(
            f"⚠️ Telegram không cho bot dùng emoji này ({e}).\n"
            "Emoji động chỉ dùng được khi tài khoản đã tạo bot (chủ bot) có Telegram Premium. "
            "Thông báo sẽ tự dùng emoji thường thay thế."
        )
        await send_html(context.bot, update.effective_chat.id, preview)
        return
    await update.message.reply_text("✅ Đã lưu. Tin \"có hàng lại\" sẽ trông như trên.")


@admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source = update.message.reply_to_message
    if source is None:
        await update.message.reply_text(
            "Cách dùng: soạn tin (chữ hoặc ảnh kèm chú thích) gửi cho bot, "
            "sau đó bấm Trả lời (Reply) vào tin đó và gõ /broadcast\n"
            "Muốn kèm nút 🛒 Mua ngay cho sản phẩm #3 thì gõ /broadcast 3"
        )
        return
    markup = None
    if context.args:
        product_id = int(context.args[0])
        if db(context).get_product(product_id) is None:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua ngay", callback_data=f"view:{product_id}")]])
    app = context.application

    async def run() -> None:
        delivered, failed = await broadcast(
            app, lambda chat_id: app.bot.copy_message(chat_id, source.chat_id, source.message_id, reply_markup=markup)
        )
        await report_to_admins(app, f"📣 Đã gửi thông báo: {delivered} người nhận, lỗi {failed}.")

    await update.message.reply_text(f"📣 Đang gửi cho {len(db(context).active_user_ids())} khách…")
    app.create_task(run())


@admin_only
async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    product_id, price = int(context.args[0]), int(re.sub(r"\D", "", context.args[1]))
    ok = db(context).set_product_price(product_id, price)
    await update.message.reply_text("Đã cập nhật giá." if ok else "Không tìm thấy sản phẩm.")


@admin_only
async def hide_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = db(context).set_product_active(int(context.args[0]), False)
    await update.message.reply_text("Đã ẩn sản phẩm." if ok else "Không tìm thấy sản phẩm.")


@admin_only
async def show_product_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = db(context).set_product_active(int(context.args[0]), True)
    await update.message.reply_text("Đã hiện sản phẩm." if ok else "Không tìm thấy sản phẩm.")


@admin_only
async def all_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = db(context).list_products(only_active=False)
    lines = [
        f"#{p['id']} {escape(p['name'])} — {money(p['price'])} — kho {p['stock']}"
        f"{'' if p['active'] else ' (ẩn)'}"
        for p in products
    ]
    await update.message.reply_text("\n".join(lines) or "Chưa có sản phẩm.", parse_mode=ParseMode.HTML)


@admin_only
async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = context.args[0] if context.args else None
    orders = db(context).list_orders(status)
    lines = [
        f"<code>{o['code']}</code> {escape(o['product_name'])} × {o['quantity']} — "
        f"{money(o['amount'])} — {STATUS_LABEL[o['status']]}"
        for o in orders
    ]
    await update.message.reply_text("\n".join(lines) or "Không có đơn.", parse_mode=ParseMode.HTML)


@admin_only
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order = db(context).get_order_by_code(context.args[0])
    if order is None:
        await update.message.reply_text("Không tìm thấy đơn.")
        return
    ok = await deliver(context.application, order["id"])
    if not ok:
        await update.message.reply_text("Đơn không còn chờ thanh toán hoặc không đủ hàng.")


@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order = db(context).get_order_by_code(context.args[0])
    if order and db(context).cancel_order(order["id"]):
        await context.bot.send_message(order["user_id"], f"❌ Đơn {order['code']} đã bị huỷ bởi cửa hàng.")
        await update.message.reply_text("Đã huỷ đơn.")
    else:
        await update.message.reply_text("Không thể huỷ đơn này.")


@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db(context).stats()
    await update.message.reply_text(
        f"📊 Doanh thu: {money(s['revenue'])}\nĐơn đã giao: {s['paid_orders']}\n"
        f"Đơn chờ thanh toán: {s['pending_orders']}\nKhách đã mua: {s['customers']}\n"
        f"Người dùng bot: {s['users']}"
    )


# ---------------------------------------------------------------- payment webhook

def add_payment_route(web_app: web.Application, app: Application) -> None:
    """HTTP endpoint for bank-transfer notifications (SePay, Casso, ...).

    Finds the order or top-up code in the transfer description: orders are
    delivered when the received amount covers them, top-ups credit the amount
    actually received.
    """
    config: Config = app.bot_data["config"]
    prefixes = "|".join(re.escape(p) for p in (config.order_prefix, config.deposit_prefix))
    code_re = re.compile(rf"(?:{prefixes})[A-Z0-9]{{6}}")

    def authorized(request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        token = header.split(" ", 1)[1] if " " in header else header
        token = token or request.headers.get("Secure-Token", "") or request.query.get("secret", "")
        return bool(config.webhook_secret) and token == config.webhook_secret

    async def handle(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"success": False}, status=401)
        payload = await request.json()
        # SePay sends one transaction; Casso sends {"data": [transactions]}.
        transactions = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
        for tx in transactions:
            if tx.get("transferType", "in") != "in":
                continue
            description = str(tx.get("content") or tx.get("description") or "").upper()
            amount = int(tx.get("transferAmount") or tx.get("amount") or 0)
            match = code_re.search(description.replace(" ", ""))
            if not match:
                continue
            deposit = app.bot_data["db"].get_deposit_by_code(match.group(0))
            if deposit is not None:
                if deposit["status"] == "pending" and amount > 0:
                    await credit_deposit(app, deposit["id"], received=amount)
                continue
            order = app.bot_data["db"].get_order_by_code(match.group(0))
            if order is None or order["status"] != "pending":
                continue
            if amount < order["amount"]:
                log.warning("Order %s underpaid: %s < %s", order["code"], amount, order["amount"])
                continue
            await deliver(app, order["id"])
        return web.json_response({"success": True})

    web_app.router.add_post("/payment", handle)


def lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


@admin_only
async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("💾 Đang tạo bản sao lưu…")
    await send_backup(context.application, [update.effective_chat.id])


@admin_only
async def emoji_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the ids of the custom emoji in this message (or the replied-to one)."""
    found = []
    for message in (update.message, update.message.reply_to_message):
        if message is None:
            continue
        for entities in (message.parse_entities([MessageEntity.CUSTOM_EMOJI]),
                         message.parse_caption_entities([MessageEntity.CUSTOM_EMOJI])):
            found += [(text, e.custom_emoji_id) for e, text in entities.items()]
    if not found:
        await update.message.reply_text(
            "Gõ /emojiid rồi chèn emoji động (emoji của gói emoji Premium) vào cùng tin nhắn, "
            "hoặc trả lời một tin có emoji động bằng /emojiid."
        )
        return
    lines = [f"{text}  <code>{eid}</code>" for text, eid in found]
    await update.message.reply_text(
        "Mã emoji (chạm để sao chép), dán vào ô \"Mã logo\" ở trang quản trị → Danh mục:\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def web_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = cfg(context)
    if not context.application.bot_data.get("admin_enabled"):
        await update.message.reply_text(
            "Trang quản trị chưa bật. Thêm ADMIN_PASSWORD=mật_khẩu_của_bạn (ít nhất 8 ký tự) vào .env rồi khởi động lại bot."
        )
        return
    links = [f"http://localhost:{config.web_port}/admin (trên chính máy chạy bot)"]
    ip = lan_address()
    if ip:
        links.append(f"http://{ip}:{config.web_port}/admin (máy khác cùng Wi-Fi)")
    links.append(f"http://<tên-máy-trong-Tailscale>:{config.web_port}/admin (khi ở ngoài, qua Tailscale)")
    await update.message.reply_text("🖥 Trang quản trị:\n" + "\n".join(links), link_preview_options=LinkPreviewOptions(is_disabled=True))


# ---------------------------------------------------------------- wiring

async def post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    try:  # the blue "Menu" button next to the message box
        await app.bot.set_my_commands([BotCommand("start", "🏠 Mở menu chính"), BotCommand("nap", "💳 Nạp tiền vào ví")])
    except Exception as e:
        log.warning("Could not set bot commands: %s", e)
    web_app = web.Application(client_max_size=20 * 1024 * 1024)
    if config.webhook_enabled:
        add_payment_route(web_app, app)
    if len(config.admin_password) >= 8:
        from .web import setup_admin

        setup_admin(web_app, app)
        app.bot_data["admin_enabled"] = True
    elif config.admin_password:
        log.warning("ADMIN_PASSWORD phải có ít nhất 8 ký tự; trang quản trị chưa được bật.")
    if config.webhook_enabled or app.bot_data.get("admin_enabled"):
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.web_port).start()
        app.bot_data["web_runner"] = runner
        log.info("Web server listening on port %s", config.web_port)


async def post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def build_application(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["db"] = Database(config.db_path)

    app.add_handler(TypeHandler(Update, track_user), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("addproduct", add_product))
    app.add_handler(CommandHandler("addstock", add_stock))
    app.add_handler(CommandHandler("price", set_price))
    app.add_handler(CommandHandler("hide", hide_product))
    app.add_handler(CommandHandler("show", show_product_cmd))
    app.add_handler(CommandHandler("allproducts", all_products))
    app.add_handler(CommandHandler("orders", list_orders))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("setemoji", set_emoji))
    app.add_handler(CommandHandler("web", web_link))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("emojiid", emoji_id))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("nap", topup_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_BUY]), show_products))
    app.add_handler(MessageHandler(filters.Text([BTN_HISTORY]), show_history))
    app.add_handler(MessageHandler(filters.Text([BTN_CONTACT]), show_contact))
    app.add_handler(MessageHandler(filters.Text([BTN_WALLET]), show_wallet))
    app.add_handler(MessageHandler(filters.Text([OLD_BTN_PRODUCTS, OLD_BTN_ORDERS, OLD_BTN_SUPPORT]), legacy_button))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(expire_job, interval=60, first=10)
    app.job_queue.run_repeating(post_job, interval=30, first=15)
    app.job_queue.run_repeating(backup_job, interval=3600, first=60)
    if 0 <= config.backup_telegram_hour <= 23:
        local_tz = datetime.now().astimezone().tzinfo
        app.job_queue.run_daily(daily_backup_job, dtime(config.backup_telegram_hour, 0, tzinfo=local_tz))
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs every request URL, which contains the bot token; the scheduler logs every minute.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    config = load_config()
    # Python 3.14+ no longer creates a loop implicitly, which run_polling expects.
    asyncio.set_event_loop(asyncio.new_event_loop())
    build_application(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
