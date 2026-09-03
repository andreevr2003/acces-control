import asyncio
import logging
import os
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
ESP_URL = os.environ["ESP_URL"].rstrip("/")
ACCESS_KEY = os.environ["ACCESS_KEY"]


def esp_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["X-Access-Key"] = ACCESS_KEY
    return requests.request(method, ESP_URL + path, headers=headers, timeout=10, **kwargs)


async def send_event_to_telegram(event: dict[str, Any]) -> None:
    event_type = event.get("type", "event")
    uid = event.get("uid", "")
    status = event.get("status", "")
    message = event.get("message", "")
    caption = f"{message}\nUID: {uid}\nStatus: {status}".strip()

    bot = telegram_app.bot
    if event_type == "button":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Deschide usa", callback_data="open_door")]]
        )
        photo = event.get("photo")
        if photo:
            await bot.send_photo(
                ALLOWED_CHAT_ID, photo=photo, caption=caption, reply_markup=keyboard
            )
        else:
            await bot.send_message(ALLOWED_CHAT_ID, caption, reply_markup=keyboard)
    elif event_type in {"card", "enroll"}:
        started = time.monotonic()
        photo = event.get("photo")
        if photo:
            await bot.send_photo(ALLOWED_CHAT_ID, photo=photo, caption=caption)
        else:
            await bot.send_message(ALLOWED_CHAT_ID, caption + "\nPoza indisponibila.")
        logging.info("Fotografie eveniment procesata in %.2fs", time.monotonic() - started)
    else:
        await bot.send_message(ALLOWED_CHAT_ID, caption)


@app.post("/event")
def event() -> tuple[Any, int]:
    if request.headers.get("X-Access-Key") != ACCESS_KEY:
        return jsonify(error="unauthorized"), 401
    payload = request.form.to_dict()
    uploaded_photo = request.files.get("photo")
    if uploaded_photo:
        payload["photo"] = uploaded_photo.read()
    elif request.is_json:
        payload = request.get_json(silent=False)
    if not isinstance(payload, dict):
        return jsonify(error="invalid event"), 400
    future = asyncio.run_coroutine_threadsafe(
        send_event_to_telegram(payload), telegram_loop
    )
    future.add_done_callback(
        lambda completed: logging.error("Eroare trimitere Telegram: %s", completed.exception())
        if completed.exception()
        else None
    )
    return jsonify(ok=True), 202


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID:
        await update.message.reply_text(
            "Control acces:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Inregistrare cheie noua", callback_data="enroll")],
                    [InlineKeyboardButton("Deschide usa", callback_data="open_door")],
                ]
            ),
        )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message and query.message.chat_id != ALLOWED_CHAT_ID:
        return
    await query.answer()
    command = query.data
    try:
        response = await asyncio.to_thread(
            esp_request, "POST", "/api/command", json={"command": command}
        )
    except requests.RequestException:
        await query.edit_message_text(
            "ESP32 nu este accesibil momentan. Verifica alimentarea si Wi-Fi."
        )
        return
    if response.ok:
        confirmation = f"Comanda trimisa: {command}"
        if query.message and query.message.photo:
            await query.edit_message_caption(caption=confirmation)
        else:
            await query.edit_message_text(confirmation)
    else:
        error_message = (
            f"ESP32 nu a acceptat comanda: HTTP {response.status_code}"
        )
        if query.message and query.message.photo:
            await query.edit_message_caption(caption=error_message)
        else:
            await query.edit_message_text(error_message)


def run_flask() -> None:
    app.run(host="0.0.0.0", port=8080, threaded=True)


telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler(["start", "menu"], start))
telegram_app.add_handler(CallbackQueryHandler(callback))
telegram_loop = asyncio.new_event_loop()


def run_telegram() -> None:
    asyncio.set_event_loop(telegram_loop)
    telegram_app.run_polling(close_loop=False)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_telegram()
