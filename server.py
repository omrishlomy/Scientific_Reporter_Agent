"""
Railway entrypoint for the Scientific Reporter bot.

This replaces the GitHub Actions polling setup. The reason is latency, measured not
assumed: Actions fired the poller every 120-280 minutes despite a */5 cron (GitHub
throttles scheduled workflows on free public repos), so every topic added and every
"generate report" button press waited hours. A persistent service can receive a
Telegram *webhook* instead, which arrives the instant the user hits send.

IMPORTANT: Telegram allows either getUpdates or a webhook, never both -- whichever ran
second gets a 409. Deploying this therefore requires the Actions poller to be off; its
schedule is disabled in .github/workflows/telegram-bot.yml for exactly this reason.

Endpoints:
  GET  /                  service info
  GET  /health            healthcheck for Railway
  POST /telegram/webhook  Telegram updates (secret-token protected)

Weekly digests run in-process via APScheduler, so one service covers both jobs.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

sys.path.insert(0, str(Path(__file__).parent / "reporter"))

import bot_poll          # noqa: E402
import chats_store       # noqa: E402
import run_reporter_cloud  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reporter")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Telegram echoes this back in X-Telegram-Bot-Api-Secret-Token on every webhook call.
# Without checking it, the endpoint is a public URL that anyone could POST fake updates
# to -- which here would mean adding topics or triggering report generation at will.
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")   # e.g. https://<service>.up.railway.app
REPORT_CRON_DAY = os.environ.get("REPORT_CRON_DAY", "mon")
REPORT_CRON_HOUR = int(os.environ.get("REPORT_CRON_HOUR", "8"))
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Jerusalem")

app = FastAPI(title="Scientific Reporter")
scheduler = BackgroundScheduler(timezone=TIMEZONE)


def run_weekly_digest() -> None:
    today = date.today().isoformat()
    chat_ids = chats_store.list_chat_ids()
    log.info("weekly digest starting for %d chat(s)", len(chat_ids))
    for chat_id in chat_ids:
        try:
            run_reporter_cloud.process_chat(chat_id, today)
        except Exception:
            # One chat's failure must not cancel everyone else's digest.
            log.exception("weekly digest failed for chat %s", chat_id)
    log.info("weekly digest finished")


def register_webhook() -> None:
    if not (TOKEN and PUBLIC_URL):
        log.warning("PUBLIC_URL or TELEGRAM_BOT_TOKEN missing; webhook NOT registered")
        return
    url = f"{PUBLIC_URL.rstrip('/')}/telegram/webhook"
    payload = {"url": url, "drop_pending_updates": False,
               "allowed_updates": '["message","callback_query"]'}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/setWebhook",
                          data=payload, timeout=30)
        log.info("setWebhook -> %s %s", r.status_code, r.text[:300])
    except requests.RequestException:
        log.exception("setWebhook failed")


@app.on_event("startup")
def on_startup() -> None:
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TOKEN))
    log.info("DATA_DIR: %s", chats_store.DATA_DIR)

    seeded = chats_store.seed_from_bundle()
    if seeded:
        log.info("seeded %d chat(s) from the repo bundle into the volume", seeded)
    log.info("registered chats: %s", chats_store.list_chat_ids())

    register_webhook()

    scheduler.add_job(run_weekly_digest, "cron", day_of_week=REPORT_CRON_DAY,
                      hour=REPORT_CRON_HOUR, minute=0, id="weekly_digest",
                      replace_existing=True, misfire_grace_time=3600)
    scheduler.start()
    log.info("weekly digest scheduled: %s %02d:00 %s", REPORT_CRON_DAY, REPORT_CRON_HOUR, TIMEZONE)


@app.on_event("shutdown")
def on_shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def root():
    return {
        "service": "Scientific Reporter",
        "chats": len(chats_store.list_chat_ids()),
        "weekly": f"{REPORT_CRON_DAY} {REPORT_CRON_HOUR:02d}:00 {TIMEZONE}",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        log.warning("rejected webhook call with bad/missing secret token")
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()

    # Telegram retries any webhook call that doesn't return promptly, which would run a
    # report twice. Acknowledge immediately and do the work in the background instead --
    # generating a report takes ~a minute.
    background.add_task(_safe_handle, update)
    return {"ok": True}


def _safe_handle(update: dict) -> None:
    try:
        bot_poll.handle_update(TOKEN, update)
    except Exception:
        log.exception("failed handling update %s", update.get("update_id"))
