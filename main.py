import asyncio
import hmac
import logging
import os
import threading

from aiogram.types import Update
from flask import Flask, jsonify, request

import telegram_bot

app = Flask(__name__)
log = logging.getLogger("render-webhook")
_bot_loop = None
_bot_ready = threading.Event()
_startup_error = None
_worker_lock = threading.Lock()
_worker_thread = None


def _finish_update(future):
    try:
        future.result()
    except Exception as exc:
        # Avoid logging update payloads or credential-bearing request details.
        log.error("Telegram update handler failed (%s)", type(exc).__name__)


def _run_aiogram_loop():
    global _bot_loop, _startup_error
    loop = asyncio.new_event_loop()
    _bot_loop = loop
    asyncio.set_event_loop(loop)
    try:
        telegram_bot.reinitialize_process_state()
        loop.run_until_complete(telegram_bot.configure_bot())
        _bot_ready.set()
        loop.run_forever()
    except Exception as exc:
        _startup_error = type(exc).__name__
        log.error("Telegram bot startup failed (%s)", _startup_error)
    finally:
        if not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            try:
                loop.run_until_complete(telegram_bot.bot.session.close())
            except Exception:
                pass
            loop.close()


def _start_bot_worker():
    global _worker_thread, _startup_error
    if os.getenv("DISABLE_BOT_WORKER") == "1":
        return
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _bot_ready.clear()
        _startup_error = None
        _worker_thread = threading.Thread(
            target=_run_aiogram_loop,
            name="aiogram-webhook-loop",
            daemon=True,
        )
        _worker_thread.start()


@app.before_request
def start_bot_after_worker_fork():
    # Starting only after the first request keeps Gunicorn preload/fork safe.
    _start_bot_worker()


@app.get("/")
def home():
    return "Podcast Bot is running", 200


@app.get("/health")
def health():
    response = jsonify(
        ok=True,
        telegram=bool(telegram_bot.BOT_TOKEN),
        groq=bool(telegram_bot.GROQ_API_KEY),
        aiogram_ready=_bot_ready.is_set(),
        startup_error=_startup_error,
        max_video_minutes=telegram_bot.MAX_VIDEO_MINUTES,
    )
    return response, (200 if _bot_ready.is_set() else 503)


@app.post("/telegram/webhook")
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, telegram_bot.WEBHOOK_SECRET):
        return jsonify(ok=False), 403
    if not _bot_ready.is_set() or _bot_loop is None or not _bot_loop.is_running():
        return jsonify(ok=False, error="bot_not_ready"), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="invalid_update"), 400
    try:
        update = Update.model_validate(payload, context={"bot": telegram_bot.bot})
        future = asyncio.run_coroutine_threadsafe(
            telegram_bot.dp.feed_update(telegram_bot.bot, update), _bot_loop
        )
        future.add_done_callback(_finish_update)
    except Exception as exc:
        log.warning("Telegram update could not be queued (%s)", type(exc).__name__)
        return jsonify(ok=False, error="queue_failed"), 503
    return jsonify(ok=True), 200
