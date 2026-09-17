"""
Sunrise Café — Telegram bot (v6)
--------------------------------
Restores the boxed monospace table look, but narrower and with
options moved to their own indented line underneath each item,
so no row ever exceeds ~26 characters and Telegram never has to
horizontally scroll the code block.

Secrets needed in Replit (Tools > Secrets):
    BOT_TOKEN       -> from @BotFather
    SELLER_GROUP_ID   -> your own numeric Telegram user ID

Also set WEB_APP_URL below to your hosted mini app URL.

Run:
    python cafe_bot.py
"""

import json
import logging
import os
from pathlib import Path
import re
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---- CONFIGURATION ----
# WEB_APP_URL now comes from a Replit secret instead of being hardcoded.
# Add a secret named WEB_APP_URL with your Netlify link as the value.
WEB_APP_URL = os.environ.get("WEB_APP_URL")
# ------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SELLER_GROUP_ID = os.environ.get("SELLER_GROUP_ID")

# Persistent daily order counter.
# On Railway, mount a Volume at /data so the sequence survives restarts/deployments.
COUNTER_FILE = os.environ.get("COUNTER_FILE", "/data/order_counters.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

orders = {}
daily_counters = {}

NAME_COL = 19
PRICE_COL = 7


def new_order_id(now):
    """Return YYYYMMDD-000001 style IDs with a daily persistent sequence."""
    date_str = now.strftime("%Y%m%d")

    counters = {}
    try:
        counter_path = Path(COUNTER_FILE)
        counter_path.parent.mkdir(parents=True, exist_ok=True)

        if counter_path.exists():
            with counter_path.open("r", encoding="utf-8") as f:
                counters = json.load(f)

        next_number = int(counters.get(date_str, 0)) + 1
        counters[date_str] = next_number

        # Keep only recent/current counters small; current date is what matters.
        with counter_path.open("w", encoding="utf-8") as f:
            json.dump(counters, f)

    except Exception:
        logger.exception("Could not persist order counter; using in-memory fallback.")
        daily_counters[date_str] = daily_counters.get(date_str, 0) + 1
        next_number = daily_counters[date_str]

    return f"{date_str}-{next_number:06d}"


def escape_md(text):
    if not text:
        return text
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


def order_keyboard():
    return ReplyKeyboardMarkup.from_button(
        KeyboardButton(
            text="🛒 Order",
            web_app=WebAppInfo(url=WEB_APP_URL),
        ),
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome to Sunrise Café! Tap the button below to order.",
        reply_markup=order_keyboard(),
    )


async def order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Tap the button below to open the menu:",
        reply_markup=order_keyboard(),
    )


def format_option_lines(opts):
    """Returns a list of short option lines, e.g.
    ['Iced - Normal', 'Sugar - 50%']
    instead of one long comma-joined string that can get truncated."""
    if not opts:
        return []
    lines = []
    temp = opts.get("temperature")
    ice = opts.get("ice")
    if temp and ice:
        lines.append(f"{temp} - {ice}")
    elif temp:
        lines.append(temp)
    elif ice:
        lines.append(ice)
    if opts.get("sugar"):
        lines.append(f"Sugar - {opts['sugar']}")
    return lines


def truncate(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def build_items_table(items):
    """Boxed monospace table, capped at NAME_COL + PRICE_COL chars wide.

    A couple of trailing spaces are added to every line so Telegram's
    built-in "copy code" icon (which it overlays on the top-right
    corner of any code block) lands on blank padding instead of
    overlapping the "Price" header text.
    """
    pad = "  "
    lines = ["```", f"{'Item':<{NAME_COL}}{'Price':>{PRICE_COL}}{pad}"]
    for item in items:
        qty = item.get("qty", 1)
        name = item.get("name", "Item")
        unit_price = item.get("unitPrice", 0)
        line_total = unit_price * qty
        name_field = truncate(f"{qty}x {name}", NAME_COL)
        price_field = f"${line_total:.2f}"
        lines.append(f"{name_field:<{NAME_COL}}{price_field:>{PRICE_COL}}{pad}")
        for opt_line in format_option_lines(item.get("options")):
            lines.append("  " + truncate(opt_line, NAME_COL + PRICE_COL - 2))
    lines.append("```")
    return "\n".join(lines)


def build_receipt(order_id, order_record):
    lines = [
        "🧾 *Sunrise Café — Receipt*",
        f"Order #{order_id}",
        order_record["placed_at"].strftime("%b %d, %Y %I:%M %p"),
        "",
        build_items_table(order_record["items"]),
        "",
        f"*Total: ${order_record['total']:.2f}*",
        "",
        "Thank you! We'll let you know when it's ready. ☕",
    ]
    return "\n".join(lines)


def build_owner_alert(order_id, order_record, user):
    name = escape_md(user.full_name)
    customer_link = f"[{name}](tg://user?id={user.id})"
    lines = [
        f"🔔 *New order #{order_id}*",
        f"🕒 {order_record['placed_at'].strftime('%I:%M %p')}",
        f"From: {customer_link}",
        "",
        build_items_table(order_record["items"]),
        "",
        f"*Total: ${order_record['total']:.2f}*",
        "",
        "📌 Status: *New Order*",
    ]
    return "\n".join(lines)


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.effective_message.web_app_data.data
    logger.info("Received order data: %s", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        await update.message.reply_text(
            "Got your order, but couldn't read it — please try again.",
            reply_markup=order_keyboard(),
        )
        return

    user = update.effective_user
    now = datetime.now()
    order_id = new_order_id(now)

    order_record = {
        "chat_id": update.effective_chat.id,
        "customer_name": user.full_name,
        "customer_username": user.username,
        "items": parsed.get("items", []),
        "total": parsed.get("total", 0),
        "placed_at": now,
        "not_accepted_at": None,
        "accepted_at": None,
        "in_progress_at": None,
        "ready_at": None,
        "status": "pending",
    }
    orders[order_id] = order_record

    receipt = build_receipt(order_id, order_record)
    await update.message.reply_text(
        receipt,
        parse_mode="Markdown",
        reply_markup=order_keyboard(),
    )

    if SELLER_GROUP_ID:
        alert_text = build_owner_alert(order_id, order_record, user)
        keyboard = order_status_keyboard(order_id, order_record["status"])
        try:
            await context.bot.send_message(
                chat_id=int(SELLER_GROUP_ID),
                text=alert_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to notify seller group")


def order_status_keyboard(order_id, current_status="pending"):
    """Build seller buttons based on the current order status."""

    # Brand-new order: seller can accept or decline it.
    if current_status == "pending":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Accept Order",
                    callback_data=f"status:accepted:{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Order Not Accepted",
                    callback_data=f"status:not_accepted:{order_id}"
                )
            ],
        ])

    # Declined order: show only one recovery action.
    if current_status == "not_accepted":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Change to Accepted",
                    callback_data=f"status:accepted:{order_id}"
                )
            ]
        ])

    # Accepted order: Accepted is visibly completed and cannot trigger again.
    if current_status == "accepted":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Accepted ✓", callback_data="noop")],
            [
                InlineKeyboardButton(
                    "👨‍🍳 Order in Progress",
                    callback_data=f"status:in_progress:{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "☕ Order is Ready",
                    callback_data=f"status:ready:{order_id}"
                )
            ],
        ])

    # In progress: accepted + progress are completed.
    if current_status == "in_progress":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Accepted ✓", callback_data="noop")],
            [InlineKeyboardButton("👨‍🍳 In Progress ✓", callback_data="noop")],
            [
                InlineKeyboardButton(
                    "☕ Order is Ready",
                    callback_data=f"status:ready:{order_id}"
                )
            ],
        ])

    # Ready: everything is completed.
    if current_status == "ready":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Accepted ✓", callback_data="noop")],
            [InlineKeyboardButton("👨‍🍳 In Progress ✓", callback_data="noop")],
            [InlineKeyboardButton("☕ Ready ✓", callback_data="noop")],
        ])

    return InlineKeyboardMarkup([])


def status_label(status):
    return {
        "pending": "New Order",
        "not_accepted": "Order Not Accepted",
        "accepted": "Accepted",
        "in_progress": "Order in Progress",
        "ready": "Order is Ready",
    }.get(status, status)


def customer_status_message(order_id, status):
    messages = {
        "not_accepted": (
            f"❌ Sorry, your order #{order_id} was not accepted by Sunrise Café."
        ),
        "accepted": (
            f"✅ Your order #{order_id} has been accepted by Sunrise Café."
        ),
        "in_progress": (
            f"👨‍🍳 Your order #{order_id} is now being prepared."
        ),
        "ready": (
            f"☕ Your order #{order_id} is ready for pickup!"
        ),
    }
    return messages[status]


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    sender_id = str(query.message.chat.id)
    if not SELLER_GROUP_ID or sender_id != str(SELLER_GROUP_ID):
        await query.answer("This action is for café staff only.", show_alert=True)
        return

    if query.data == "noop":
        await query.answer("This step has already been completed.", show_alert=False)
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Invalid order action.", show_alert=True)
        return

    _, new_status, order_id = parts
    record = orders.get(order_id)

    if not record:
        await query.answer(
            "Order not found (may have been cleared after a bot restart).",
            show_alert=True
        )
        return

    valid_statuses = {"not_accepted", "accepted", "in_progress", "ready"}
    if new_status not in valid_statuses:
        await query.answer("Invalid status.", show_alert=True)
        return

    current_status = record.get("status", "pending")

    # Define exactly which status changes are allowed.
    allowed_transitions = {
        "pending": {"accepted", "not_accepted"},
        "not_accepted": {"accepted"},
        "accepted": {"in_progress"},
        "in_progress": {"ready"},
        "ready": set(),
    }

    if new_status == current_status:
        await query.answer(
            f"Already marked: {status_label(current_status)}",
            show_alert=False
        )
        return

    if new_status not in allowed_transitions.get(current_status, set()):
        next_steps = allowed_transitions.get(current_status, set())
        if next_steps:
            next_text = " or ".join(status_label(s) for s in next_steps)
            await query.answer(
                f"Current status is {status_label(current_status)}. Next: {next_text}.",
                show_alert=True
            )
        else:
            await query.answer("This order is already completed.", show_alert=True)
        return

    now = datetime.now()
    record["status"] = new_status

    if new_status == "not_accepted":
        record["not_accepted_at"] = now
    elif new_status == "accepted":
        record["accepted_at"] = now
    elif new_status == "in_progress":
        record["in_progress_at"] = now
    elif new_status == "ready":
        record["ready_at"] = now

    # Notify customer for every valid status change.
    try:
        await context.bot.send_message(
            chat_id=record["chat_id"],
            text=customer_status_message(order_id, new_status),
        )
    except Exception:
        logger.exception("Failed to notify customer for order %s", order_id)
        await query.answer(
            "Status updated, but customer notification failed.",
            show_alert=True
        )
        return

    await query.answer(f"Customer notified: {status_label(new_status)}")

    # Keep the original order details but replace our previous status footer.
    original_text = query.message.text_markdown or query.message.text
    original_text = re.sub(
        r"\n\n📌 Status: .*?(?=\n\n|$)",
        "",
        original_text,
        flags=re.DOTALL,
    )

    status_time = now.strftime("%I:%M %p")

    if new_status == "not_accepted":
        status_icon = "❌"
    elif new_status == "accepted":
        status_icon = "✅"
    elif new_status == "in_progress":
        status_icon = "👨‍🍳"
    else:
        status_icon = "☕"

    updated_text = (
        f"{original_text}\n\n"
        f"📌 Status: {status_icon} *{status_label(new_status)}*\n"
        f"🕒 Updated at {status_time}\n"
        f"📨 Customer notified."
    )

    await query.edit_message_text(
        updated_text,
        parse_mode="Markdown",
        reply_markup=order_status_keyboard(order_id, new_status),
    )



async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current Telegram chat/group ID for easy Replit setup."""
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Chat type: {chat.type}\n\n"
        "Use this Chat ID as SELLER_GROUP_ID in your Replit Secrets."
    )

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN secret is not set.")
    if not WEB_APP_URL:
        raise SystemExit("WEB_APP_URL secret is not set.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("order", order))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(CallbackQueryHandler(status_callback, pattern=r"^status:"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
