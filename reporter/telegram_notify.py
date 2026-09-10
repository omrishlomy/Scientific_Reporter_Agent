"""
Telegram sender for the cloud (GitHub Actions) path -- a Python port of
send-telegram.ps1, since Actions runners are Linux and the PowerShell version's
credential storage (DPAPI) is Windows-only anyway. Reads the bot token from an
environment variable (a GitHub Actions secret), not a local encrypted file, since
there is no persistent "this machine" to scope DPAPI to on an ephemeral runner.

    python telegram_notify.py --chat-id 123456 --message "text" [--photo path.png] [--document path.md]

--chat-id is required in multi-tenant mode (one send per registered chat). Falls back
to the TELEGRAM_CHAT_ID env var if --chat-id is omitted, for any single-chat/local
testing use.

Exits 0 on success, 1 on failure, 2 if TELEGRAM_BOT_TOKEN or a chat id are unset
(so a missing secret degrades the run to "no notification" rather than failing it).
"""
from __future__ import annotations

import argparse
import os
import sys

import requests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", required=True)
    ap.add_argument("--photo")
    ap.add_argument("--document")
    ap.add_argument("--chat-id", help="defaults to TELEGRAM_CHAT_ID env var if omitted")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = args.chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("WARN TELEGRAM_BOT_TOKEN / chat id not set; skipping", file=sys.stderr)
        return 2

    base = f"https://api.telegram.org/bot{token}"

    try:
        # Telegram caps message text at 4096 chars.
        text = args.message[:4090] + "..." if len(args.message) > 4096 else args.message
        r = requests.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=30)
        r.raise_for_status()

        if args.photo and os.path.exists(args.photo):
            with open(args.photo, "rb") as f:
                r = requests.post(f"{base}/sendPhoto", data={"chat_id": chat_id},
                                   files={"photo": f}, timeout=60)
                r.raise_for_status()

        if args.document and os.path.exists(args.document):
            with open(args.document, "rb") as f:
                r = requests.post(f"{base}/sendDocument", data={"chat_id": chat_id},
                                   files={"document": f}, timeout=60)
                r.raise_for_status()

        return 0
    except requests.RequestException as e:
        print(f"WARN telegram send failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
