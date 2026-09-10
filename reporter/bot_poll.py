"""
Interactive topic management, run every ~5 minutes by
.github/workflows/telegram-bot.yml. Short-polls Telegram for new messages and applies
commands per-chat -- the practical shape of "an interactive bot" on Actions, which is
ephemeral and can't hold an always-on long-poll connection the way a real server could.

MULTI-TENANT: any chat that messages the bot is auto-registered (see chats_store.py)
with its own topics.yaml and seen.json, and gets its own weekly digest. There is no
allowlist -- the bot's username is not a secret once shared, so anyone who has it can
register a chat and use it. That's an accepted, low-stakes tradeoff here (Groq's free
tier and Europe PMC cost nothing per use); add a `chats_store.py` allowlist check if
that ever needs to change.

Commands (per chat):
  /topics                      list this chat's topics
  /addtopic <name> | <query>   add a topic with an exact Europe PMC query
  /removetopic <name>          disable a topic
  /help                        usage
  <anything else>              treated as a plain-language topic description -- drafted
                                into a query via Groq (see topic-draft-prompt.md) and
                                added directly. This is now cheap and fast because Groq
                                hosts the model; the original reporter design avoided
                                any LLM call in this fast poller specifically because
                                the model was a slow self-hosted one -- that constraint
                                no longer applies now that synthesis runs on Groq too.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import requests
import yaml

import chats_store

HERE = pathlib.Path(__file__).parent
OFFSET_FILE = HERE / "telegram-offset.json"
TOPIC_DRAFT_PROMPT_FILE = HERE / "topic-draft-prompt.md"

TOPIC_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "query": {"type": "string"},
    },
    "required": ["name", "query"],
    "additionalProperties": False,
}

HELP_TEXT = (
    "Commands:\n"
    "/topics - list your topics\n"
    "/addtopic <name> | <query> - add a topic with an exact Europe PMC query\n"
    "  example: /addtopic Vagus nerve | (TITLE:\"vagus nerve\" OR TITLE:VNS) AND (TITLE:brain OR TITLE:stimulation)\n"
    "/removetopic <name> - disable a topic\n"
    "/help - this message\n\n"
    "Or just tell me what you're interested in, in plain English, and I'll set up the "
    "search for you (e.g. \"psychedelics and consciousness\")."
)

WELCOME_TEXT = (
    "Welcome! I'll send you a weekly digest of new research papers, based on topics "
    "you choose.\n\n"
    "Tell me what you're interested in -- plain English is fine (e.g. \"neural "
    "interfaces for paralysis\" or \"sleep and memory consolidation\") and I'll set up "
    "the search. Add as many as you like, any time.\n\n"
    "Send /help for other commands."
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


def handle_topics(chat_id: str, reply) -> None:
    cfg = yaml.safe_load(chats_store.topics_path(chat_id).read_text(encoding="utf-8")) or {}
    topics = cfg.get("topics", [])
    if not topics:
        reply("No topics yet -- just tell me what you're interested in, in plain English.")
        return
    lines = [f"- {t.get('name', 'Unnamed')} [{'on' if t.get('enabled', True) else 'off (disabled)'}]"
             for t in topics]
    reply("Your topics:\n" + "\n".join(lines))


def add_topic(chat_id: str, name: str, query: str) -> tuple[bool, str]:
    """Returns (added, message)."""
    path = chats_store.topics_path(chat_id)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {"settings": {}, "topics": []}
    if any(t.get("name", "").lower() == name.lower() for t in cfg.get("topics", [])):
        return False, f"A topic named '{name}' already exists. Use /removetopic first to replace it."
    cfg.setdefault("topics", []).append({"name": name, "enabled": True, "query": query})
    path.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    return True, f"Added topic '{name}':\n{query}\n\nIf that's not quite right, /removetopic {name} and try again with different wording."


def handle_addtopic(chat_id: str, arg: str, reply) -> None:
    if "|" not in arg:
        reply("Format: /addtopic <name> | <query>\n\n"
              "Or just describe the topic in plain English without the /addtopic command "
              "and I'll draft the query for you.")
        return
    name, query = (p.strip() for p in arg.split("|", 1))
    if not name or not query:
        reply("Both a name and a query are needed -- see /help.")
        return
    _, msg = add_topic(chat_id, name, query)
    reply(msg)


def handle_removetopic(chat_id: str, arg: str, reply) -> None:
    name = arg.strip()
    if not name:
        reply("Format: /removetopic <name>")
        return
    path = chats_store.topics_path(chat_id)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    match = next((t for t in cfg.get("topics", []) if t.get("name", "").lower() == name.lower()), None)
    if not match:
        reply(f"No topic named '{name}' found. Send /topics to see current names.")
        return
    match["enabled"] = False
    path.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    reply(f"Disabled topic '{name}'.")


def handle_natural_language(chat_id: str, text: str, reply) -> None:
    from local_llm import run_prompt   # imported lazily -- most polls never reach here
    try:
        prompt = TOPIC_DRAFT_PROMPT_FILE.read_text(encoding="utf-8")
        raw = run_prompt(prompt, text, max_tokens=300, json_schema=TOPIC_DRAFT_SCHEMA)
        draft = json.loads(raw)
        _, msg = add_topic(chat_id, draft["name"], draft["query"])
        reply(msg)
    except Exception as e:
        print(f"WARN topic drafting failed: {e}", file=sys.stderr)
        reply("Couldn't turn that into a search automatically -- try /addtopic <name> | <query> "
              "with an exact query instead (see /help), or rephrase.")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1

    offset = load_offset()
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset + 1, "timeout": 0}, timeout=30)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    max_update_id = offset

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message")
        if not msg or "text" not in msg:
            continue

        chat_id = str(msg["chat"]["id"])
        chat_name = msg["chat"].get("title") or msg["chat"].get("username") or msg["chat"].get("first_name", "")
        text = msg["text"].strip()
        reply = lambda t: send(token, chat_id, t)

        is_new = chats_store.register_chat(chat_id, chat_name)
        if is_new:
            reply(WELCOME_TEXT)
            if text in ("/start", "/help"):
                continue   # don't also treat "/start" itself as a topic description

        if text in ("/help", "/start"):
            reply(HELP_TEXT)
        elif text == "/topics":
            handle_topics(chat_id, reply)
        elif text.startswith("/addtopic"):
            handle_addtopic(chat_id, text[len("/addtopic"):].strip(), reply)
        elif text.startswith("/removetopic"):
            handle_removetopic(chat_id, text[len("/removetopic"):].strip(), reply)
        elif text.startswith("/"):
            reply("Unknown command. Send /help for the list.")
        else:
            handle_natural_language(chat_id, text, reply)

    if max_update_id > offset:
        save_offset(max_update_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
