import asyncio
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import IPv4Network
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
ACCESS_KEY = os.environ["ACCESS_KEY"]
ESP_SCAN_INTERVAL = 60
esp_url = ""
esp_url_lock = threading.Lock()


def esp_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["X-Access-Key"] = ACCESS_KEY
    with esp_url_lock:
        target = esp_url
    if not target:
        raise requests.ConnectionError("ESP32 nu a fost detectat in reteaua locala")
    return requests.request(method, target + path, headers=headers, timeout=3, **kwargs)


def default_local_network() -> IPv4Network:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        local_ip = probe.getsockname()[0]
    finally:
        probe.close()
    return IPv4Network(f"{local_ip}/24", strict=False)


def probe_esp(ip: str) -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.15)
    try:
        if sock.connect_ex((ip, 80)) != 0:
            return None
        response = requests.get(
            f"http://{ip}/api/status",
            headers={"X-Access-Key": ACCESS_KEY},
            timeout=1,
        )
        if response.ok:
            return f"http://{ip}"
    except requests.RequestException:
        return None
    finally:
        sock.close()
    return None


def discover_esp32() -> None:
    global esp_url
    try:
        local_network = default_local_network()
        networks = [local_network, IPv4Network("10.31.0.0/16")]
        candidates = {
            ip
            for network in networks
            for ip in network.hosts()
        }
        logging.info("Scanez %d adrese pentru ESP32 (inclusiv 10.31.x.x)", len(candidates))
        with ThreadPoolExecutor(max_workers=256) as executor:
            futures = [executor.submit(probe_esp, str(ip)) for ip in candidates]
            for future in as_completed(futures):
                found = future.result()
                if found:
                    with esp_url_lock:
                        esp_url = found
                    logging.info("ESP32 detectat automat la %s", found)
                    return
        logging.warning("ESP32 nu a fost gasit pe %s", network)
    except (OSError, ValueError) as error:
        logging.error("Nu pot determina reteaua locala pentru scanare: %s", error)


def discovery_loop() -> None:
    while True:
        discover_esp32()
        time.sleep(ESP_SCAN_INTERVAL)


async def send_event_to_telegram(event: dict[str, Any]) -> None:
    started = time.monotonic()
    event_type = event.get("type", "event")
    uid = event.get("uid", "")
    status = event.get("status", "")
    message = event.get("message", "")
    caption = f"{message}\nUID: {uid}\nStatus: {status}".strip()

    bot = telegram_app.bot
    if event_type == "button":
        telegram_started = time.monotonic()
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
        logging.info("Notificare buton Telegram trimisa in %.2fs", time.monotonic() - telegram_started)
    elif event_type in {"card", "enroll"}:
        photo = event.get("photo")
        if not photo:
            logging.warning("Evenimentul %s nu contine fotografie; cer fallback de la ESP32.", event_type)
            try:
                fallback = await asyncio.to_thread(esp_request, "GET", "/api/photo")
                if fallback.ok:
                    photo = fallback.content
            except requests.RequestException as error:
                logging.error("Fallback fotografie ESP32 esuat: %s", error)
        if photo:
            await bot.send_photo(ALLOWED_CHAT_ID, photo=photo, caption=caption)
        else:
            await bot.send_message(ALLOWED_CHAT_ID, caption + "\nPoza indisponibila.")
        logging.info("Fotografie eveniment trimisa in %.2fs", time.monotonic() - started)
    else:
        await bot.send_message(ALLOWED_CHAT_ID, caption)
    logging.info("Eveniment %s finalizat in %.2fs", event_type, time.monotonic() - started)


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
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    run_telegram()
