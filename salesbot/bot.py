"""Telegram auto-sales bot: catalog, orders, VietQR payment, auto delivery."""

import logging
import re
from html import escape
from urllib.parse import quote

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config, load_config
from .db import Database

log = logging.getLogger(__name__)

BTN_PRODUCTS = "🛍 Sản phẩm"
BTN_ORDERS = "📦 Đơn hàng của tôi"
BTN_SUPPORT = "💬 Hỗ trợ"
MAIN_MENU = ReplyKeyboardMarkup([[BTN_PRODUCTS], [BTN_ORDERS, BTN_SUPPORT]], resize_keyboard=True)
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = escape(update.effective_user.first_name or "bạn")
    await update.message.reply_text(
        f"👋 Xin chào <b>{name}</b>!\nChào mừng đến với cửa hàng. Chọn một mục bên dưới để bắt đầu.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = db(context).list_products()
    if not products:
        text, markup = "Hiện chưa có sản phẩm nào.", None
    else:
        text = "🛍 <b>Danh sách sản phẩm</b>\nChọn sản phẩm để xem chi tiết:"
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    f"{p['name']} — {money(p['price'])} (còn {db(context).sellable_quantity(p['id'])})",
                    callback_data=f"prod:{p['id']}",
                )]
                for p in products
            ]
        )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int) -> None:
    query = update.callback_query
    product = db(context).get_product(product_id)
    if product is None or not product["active"]:
        await query.edit_message_text("Sản phẩm không còn tồn tại.")
        return
    available = db(context).sellable_quantity(product_id)
    text = (
        f"<b>{escape(product['name'])}</b>\n\n{escape(product['description'])}\n\n"
        f"💰 Giá: <b>{money(product['price'])}</b>\n📦 Còn lại: <b>{available}</b>"
    )
    rows = []
    if available > 0:
        qty_buttons = [
            InlineKeyboardButton(f"Mua {q}", callback_data=f"buy:{product_id}:{q}")
            for q in range(1, min(available, MAX_QTY_BUTTONS) + 1)
        ]
        rows.append(qty_buttons)
    else:
        text += "\n\n⚠️ Sản phẩm tạm hết hàng."
    rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="list")])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, qty: int) -> None:
    query = update.callback_query
    user = update.effective_user
    config = cfg(context)
    order = db(context).create_order(config.order_prefix, user.id, user.username, product_id, qty)
    if order is None:
        await query.answer("Không đủ hàng, vui lòng chọn số lượng khác.", show_alert=True)
        await show_product(update, context, product_id)
        return

    caption = (
        f"🧾 <b>Đơn hàng {order['code']}</b>\n"
        f"Sản phẩm: {escape(order['product_name'])} × {qty}\n"
        f"Tổng tiền: <b>{money(order['amount'])}</b>\n\n"
        f"🏦 Ngân hàng: <b>{escape(config.bank_code)}</b>\n"
        f"💳 STK: <code>{escape(config.bank_account)}</code>\n"
        f"👤 Chủ TK: {escape(config.bank_account_name)}\n"
        f"📝 Nội dung CK: <code>{order['code']}</code>\n\n"
        f"Quét mã QR hoặc chuyển khoản đúng <b>số tiền</b> và <b>nội dung</b>. "
        f"Hàng sẽ được gửi tự động ngay khi thanh toán được xác nhận.\n"
        f"⏰ Đơn tự huỷ sau {config.order_timeout_minutes} phút."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Huỷ đơn", callback_data=f"cancel:{order['id']}")]])
    await query.message.reply_photo(
        qr_url(config, order["amount"], order["code"]),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )
    await notify_admins_new_order(context.application, order)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = db(context).list_user_orders(update.effective_user.id)
    if not orders:
        await update.message.reply_text("Bạn chưa có đơn hàng nào.")
        return
    lines = ["📦 <b>Đơn hàng gần đây</b>"]
    buttons = []
    for o in orders:
        lines.append(
            f"• <code>{o['code']}</code> — {escape(o['product_name'])} × {o['quantity']} — "
            f"{money(o['amount'])} — {STATUS_LABEL[o['status']]}"
        )
        if o["status"] == "paid":
            buttons.append([InlineKeyboardButton(f"📥 Xem lại hàng {o['code']}", callback_data=f"items:{o['id']}")])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = cfg(context).support_contact or "admin"
    await update.message.reply_text(f"💬 Cần hỗ trợ? Liên hệ {contact}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, *args = query.data.split(":")
    if action == "list":
        await query.answer()
        await show_products(update, context)
    elif action == "prod":
        await query.answer()
        await show_product(update, context, int(args[0]))
    elif action == "buy":
        await query.answer()
        await buy(update, context, int(args[0]), int(args[1]))
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


# ---------------------------------------------------------------- admin

ADMIN_HELP = """<b>Lệnh quản trị</b>
/addproduct Tên | Giá | Mô tả — thêm sản phẩm
/addstock ID — rồi mỗi dòng tiếp theo là 1 hàng (tài khoản, key, link…)
/price ID GIÁ — đổi giá
/hide ID, /show ID — ẩn/hiện sản phẩm
/allproducts — xem tất cả sản phẩm và tồn kho
/orders [pending|paid|cancelled] — xem đơn
/confirm MÃ_ĐƠN — xác nhận đã thanh toán và giao hàng
/cancel MÃ_ĐƠN — huỷ đơn
/stats — thống kê doanh thu"""


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
    count = db(context).add_stock(product_id, items)
    await update.message.reply_text(f"Đã nạp {count} hàng cho sản phẩm #{product_id}.")


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
        f"Đơn chờ thanh toán: {s['pending_orders']}\nKhách hàng: {s['customers']}"
    )


# ---------------------------------------------------------------- payment webhook

def build_webhook_app(app: Application) -> web.Application:
    """HTTP endpoint for bank-transfer notifications (SePay, Casso, ...).

    Finds the order code in the transfer description and delivers the order
    automatically when the received amount covers it.
    """
    config: Config = app.bot_data["config"]
    code_re = re.compile(rf"{re.escape(config.order_prefix)}[A-Z0-9]{{6}}")

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
            order = app.bot_data["db"].get_order_by_code(match.group(0))
            if order is None or order["status"] != "pending":
                continue
            if amount < order["amount"]:
                log.warning("Order %s underpaid: %s < %s", order["code"], amount, order["amount"])
                continue
            await deliver(app, order["id"])
        return web.json_response({"success": True})

    web_app = web.Application()
    web_app.router.add_post("/payment", handle)
    return web_app


# ---------------------------------------------------------------- wiring

async def post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    if config.webhook_enabled:
        runner = web.AppRunner(build_webhook_app(app))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.webhook_port).start()
        app.bot_data["web_runner"] = runner
        log.info("Payment webhook listening on :%s/payment", config.webhook_port)


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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("addproduct", add_product))
    app.add_handler(CommandHandler("addstock", add_stock))
    app.add_handler(CommandHandler("price", set_price))
    app.add_handler(CommandHandler("hide", hide_product))
    app.add_handler(CommandHandler("show", show_product_cmd))
    app.add_handler(CommandHandler("allproducts", all_products))
    app.add_handler(CommandHandler("orders", list_orders))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.Text([BTN_PRODUCTS]), show_products))
    app.add_handler(MessageHandler(filters.Text([BTN_ORDERS]), my_orders))
    app.add_handler(MessageHandler(filters.Text([BTN_SUPPORT]), support))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(expire_job, interval=60, first=10)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    config = load_config()
    build_application(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
