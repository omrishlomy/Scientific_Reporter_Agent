"""
Telegram sender used by the report pipeline.

    python telegram_notify.py --chat-id 123456 --message "text" [--photo path.png]
        [--document path.md ["caption"]] [--document other.md ["caption"]] ...

Sends the message, then the photo, then each document in order. --chat-id falls back to
the TELEGRAM_CHAT_ID env var for single-chat/local testing.

Exits 0 on success, 1 on failure, 2 if TELEGRAM_BOT_TOKEN or a chat id are unset
(so a missing secret degrades the run to "no notification" rather than failing it).
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

CAPTION_LIMIT = 1024   # Telegram's limit for photo/document captions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", required=True)
    ap.add_argument("--photo")
    ap.add_argument("--document", action="append", nargs="+", default=[],
                    metavar=("PATH", "CAPTION"), help="repeatable; optional caption after the path")
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
                                  files={"photo": (os.path.basename(args.photo), f)}, timeout=60)
                r.raise_for_status()

        for entry in args.document:
            path, caption = entry[0], " ".join(entry[1:]).strip()
            if not os.path.exists(path):
                print(f"WARN document not found, skipping: {path}", file=sys.stderr)
                continue
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:CAPTION_LIMIT]
            with open(path, "rb") as f:
                # Explicit filename: that's what the user sees and saves in Telegram.
                r = requests.post(f"{base}/sendDocument", data=data,
                                  files={"document": (os.path.basename(path), f)}, timeout=120)
                r.raise_for_status()

        return 0
    except requests.RequestException as e:
        print(f"WARN telegram send failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
