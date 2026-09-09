"""
Interactive topic management, run every ~5 minutes by
.github/workflows/telegram-bot.yml. Short-polls Telegram for new messages and applies
simple structured commands to topics.yaml -- this is the practical answer to "let the
bot manage topics" on a GitHub Actions runner, which is ephemeral and can't hold an
always-on long-poll connection the way a real server could.

Deliberately does NOT use the local LLM here: this job is meant to run every 5 minutes
and reply within seconds, and loading a multi-GB model just to check "is there a new
message" would make it slow and expensive for no benefit. Query drafting from a plain
description (rather than typing raw Europe PMC syntax) is a reasonable future addition,
but it belongs in a heavier, less-frequent job -- shipping the simple, reliable version
first rather than bundling it in here.

SECURITY: only ever acts on messages from TELEGRAM_CHAT_ID (the owner's own chat).
Anyone who discovers the bot's public username can message it, and a bot token is not
a secret in the way an API key is -- so a message from any other chat is logged and
ignored, never actioned, regardless of what it says.

Commands:
  /topics                      list current topics
  /addtopic <name> | <query>   add a topic (Europe PMC query syntax -- see topics.yaml)
  /removetopic <name>          disable a topic (kept in the file, enabled: false)
  /help                        usage
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import requests
import yaml

HERE = pathlib.Path(__file__).parent
TOPICS_FILE = HERE / "topics.yaml"
OFFSET_FILE = HERE / "telegram-offset.json"

HELP_TEXT = (
    "Commands:\n"
    "/topics - list current topics\n"
    "/addtopic <name> | <query> - add a topic\n"
    "  example: /addtopic Vagus nerve | (TITLE:\"vagus nerve\" OR TITLE:VNS) AND (TITLE:brain OR TITLE:stimulation)\n"
    "/removetopic <name> - disable a topic\n"
    "/help - this message"
)


def load_offset() -> int:
    if OFFSET_FILE.exists():
        return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("last_update_id", 0)
    return 0


def save_offset(update_id: int) -> None:
    OFFSET_FILE.write_text(json.dumps({"last_update_id": update_id}), encoding="utf-8")


def send(token: str, chat_id: str, text: str) -> None:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  data={"chat_id": chat_id, "text": text[:4090]}, timeout=30)


def handle_topics(reply) -> None:
    cfg = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8")) or {}
    topics = cfg.get("topics", [])
    if not topics:
        reply("No topics configured.")
        return
    lines = []
    for t in topics:
        status = "on" if t.get("enabled", True) else "off (disabled)"
        lines.append(f"- {t.get('name', 'Unnamed')} [{status}]")
    reply("Current topics:\n" + "\n".join(lines))


def handle_addtopic(arg: str, reply) -> None:
    if "|" not in arg:
        reply("Format: /addtopic <name> | <query>\n\n"
              "Example: /addtopic Vagus nerve | (TITLE:\"vagus nerve\" OR TITLE:VNS) AND (TITLE:brain OR TITLE:stimulation)\n\n"
              "The query uses Europe PMC syntax -- see the comments at the top of topics.yaml.")
        return
    name, query = (p.strip() for p in arg.split("|", 1))
    if not name or not query:
        reply("Both a name and a query are needed -- see /help.")
        return

    cfg = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8")) or {"settings": {}, "topics": []}
    existing = [t for t in cfg.get("topics", []) if t.get("name", "").lower() == name.lower()]
    if existing:
        reply(f"A topic named '{name}' already exists. Use /removetopic first if you want to replace it.")
        return

    cfg.setdefault("topics", []).append({"name": name, "enabled": True, "query": query})
    TOPICS_FILE.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    reply(f"Added topic '{name}'. It'll show up in next week's digest.")


def handle_removetopic(arg: str, reply) -> None:
    name = arg.strip()
    if not name:
        reply("Format: /removetopic <name>")
        return
    cfg = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8")) or {}
    topics = cfg.get("topics", [])
    match = next((t for t in topics if t.get("name", "").lower() == name.lower()), None)
    if not match:
        reply(f"No topic named '{name}' found. Send /topics to see current names.")
        return
    match["enabled"] = False
    TOPICS_FILE.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    reply(f"Disabled topic '{name}'. (Left in the file, in case you want it back -- edit topics.yaml to remove it entirely.)")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not owner_chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 1

    offset = load_offset()
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset + 1, "timeout": 0}, timeout=30)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    changed = False
    max_update_id = offset

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message")
        if not msg or "text" not in msg:
            continue

        chat_id = str(msg["chat"]["id"])
        if chat_id != str(owner_chat_id):
            # Never act on a message from anyone but the configured owner chat --
            # the bot's username is not secret, so this is a real access boundary.
            print(f"IGNORED message from unauthorized chat {chat_id}", file=sys.stderr)
            continue

        text = msg["text"].strip()
        reply = lambda t: send(token, owner_chat_id, t)

        if text in ("/help", "/start"):
            reply(HELP_TEXT)
        elif text == "/topics":
            handle_topics(reply)
        elif text.startswith("/addtopic"):
            handle_addtopic(text[len("/addtopic"):].strip(), reply)
            changed = True
        elif text.startswith("/removetopic"):
            handle_removetopic(text[len("/removetopic"):].strip(), reply)
            changed = True
        else:
            reply("Unknown command. Send /help for the list.")

    if max_update_id > offset:
        save_offset(max_update_id)

    # Signal to the workflow YAML whether topics.yaml needs to be committed.
    print("CHANGED=1" if changed else "CHANGED=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
