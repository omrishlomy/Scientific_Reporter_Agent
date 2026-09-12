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

Primary interface is *ASTERISK* topics -- send `*psychedelics* *neurofeedback*` and each
becomes a topic, with the Europe PMC query drafted via Groq (topic-draft-prompt.md).
Every reply carries a "Generate report now" inline button.

Commands (per chat):
  /topics                      list this chat's topics
  /addtopic <name> | <query>   add a topic with an exact Europe PMC query (power user)
  /removetopic <name>          disable a topic
  /report                      generate a report immediately (same as the button)
  /help                        usage

LATENCY WARNING: "now" means "next time this workflow runs". Measured over 21 real
scheduled runs, GitHub fires this every 120-280 minutes despite the */5 cron -- it
throttles scheduled workflows hard on free public repos. Button presses and topic
messages therefore sit in Telegram's queue for hours. Genuinely instant replies need a
webhook on a persistent HTTPS endpoint, which Actions cannot provide.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import threading

import requests
import yaml
from ruamel.yaml import YAML

import chats_store

# Round-trip YAML for WRITES. PyYAML's safe_load -> dump cycle silently destroys every
# comment and reflows block scalars into escaped one-liners; the first real topic-add
# wiped the whole header out of a chat's topics.yaml, including a warning about query
# syntax that existed because of a past bug. ruamel preserves comments and layout, which
# matters because these files are documented as hand-editable too.
_yaml_rt = YAML()
_yaml_rt.preserve_quotes = True
_yaml_rt.width = 4096          # don't re-wrap long query lines
_yaml_rt.indent(mapping=2, sequence=4, offset=2)


def load_topics_rt(path: pathlib.Path):
    with path.open(encoding="utf-8") as f:
        return _yaml_rt.load(f)


def save_topics_rt(path: pathlib.Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        _yaml_rt.dump(data, f)

HERE = pathlib.Path(__file__).parent
OFFSET_FILE = HERE / "telegram-offset.json"
TOPIC_DRAFT_PROMPT_FILE = HERE / "topic-draft-prompt.md"

TOPIC_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        # Without an explicit decline path, strict schema mode FORCES the model to
        # return some name+query for any input -- a real first-contact message of
        # "hi" produced {"name": "General query", "query": "TITLE:*"}, which matches
        # essentially every paper in Europe PMC and would have flooded the digest.
        "is_topic": {"type": "boolean"},
        "name": {"type": "string"},
        "query": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["is_topic", "name", "query", "reason"],
    "additionalProperties": False,
}

WELCOME_TEXT = (
    "Hi, I'm your Scientific Reporter.\n\n"
    "Every week I search newly published research (Europe PMC -- journals and "
    "bioRxiv/medRxiv preprints) on the topics you pick, and send you:\n"
    "  - a short written brief of what's new and how it connects\n"
    "  - an infographic summary you can read in 10 seconds\n"
    "  - the full digest file, to drop into NotebookLM for a deep dive\n\n"
    "To set your topics, send them wrapped in asterisks:\n"
    "*respiration and the brain* *psychedelics* *neurofeedback*\n\n"
    "Add more any time the same way -- just send *your new topic*.\n\n"
    "Reports go out automatically once a week -- send /schedule to make that daily, "
    "monthly, or anything in between. Use the button below (or /report) for one right "
    "now.\n\n"
    "/status to see your setup  |  /help for everything else"
)

HELP_TEXT = (
    "How to use me:\n\n"
    "ADD TOPICS -- send them wrapped in asterisks, as many at once as you like:\n"
    "  *sleep and memory* *vagus nerve stimulation*\n"
    "I'll build the search query for each one.\n\n"
    "Commands:\n"
    "/topics - list your topics\n"
    "/removetopic <name> - turn a topic off\n"
    "/report - generate a report right now\n"
    "/schedule - how often reports arrive (default: every week)\n"
    "    /schedule daily | weekly | 2w | 10d | monthly\n"
    "/pause and /resume - stop/restart scheduled reports (topics are kept)\n"
    "/status - topics, schedule, last and next report\n"
    "/help - this message\n\n"
    "Power user: /addtopic <name> | <exact Europe PMC query> skips the query drafting,\n"
    "  e.g. /addtopic Vagus nerve | (TITLE:\"vagus nerve\" OR TITLE:VNS) AND TITLE:brain"
)

# Inline keyboard attached to bot replies. callback_data is what comes back in the
# callback_query update when the button is tapped.
REPORT_BUTTON = {"inline_keyboard": [[{"text": "Generate report now", "callback_data": "gen_report"}]]}


def load_offset() -> int:
    if OFFSET_FILE.exists():
        return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("last_update_id", 0)
    return 0


def save_offset(update_id: int) -> None:
    OFFSET_FILE.write_text(json.dumps({"last_update_id": update_id}), encoding="utf-8")


def send(token: str, chat_id: str, text: str, button: bool = True) -> None:
    payload = {"chat_id": chat_id, "text": text[:4090]}
    if button:
        payload["reply_markup"] = json.dumps(REPORT_BUTTON)
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data=payload, timeout=30)
    except requests.RequestException as e:
        print(f"WARN send to {chat_id} failed: {e}", file=sys.stderr)
        return

    if r.status_code == 403:
        # "Forbidden: bot was blocked by the user" / kicked from the group. Nothing we
        # send will ever arrive again, so stop scheduling work for this chat instead of
        # generating a full report for it every interval forever. /resume un-does this
        # if they unblock and come back.
        print(f"INFO chat {chat_id} blocked the bot; auto-pausing", file=sys.stderr)
        try:
            chats_store.update_meta(chat_id, paused=True, paused_reason="blocked_by_user")
        except Exception:
            pass
    elif r.status_code != 200:
        print(f"WARN sendMessage {r.status_code} for {chat_id}: {r.text[:200]}", file=sys.stderr)


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Stops the button's spinner in the client. Best-effort only: callback ids expire
    within seconds, and this poller may not run for hours, so this will usually fail by
    the time we get here -- which is harmless, the report still gets sent."""
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                      data={"callback_query_id": callback_id, "text": text[:200]}, timeout=15)
    except requests.RequestException:
        pass


def handle_topics(chat_id: str, reply) -> None:
    cfg = yaml.safe_load(chats_store.topics_path(chat_id).read_text(encoding="utf-8")) or {}
    topics = cfg.get("topics") or []
    if not topics:
        reply("No topics yet. Send them wrapped in asterisks, e.g.\n*psychedelics* *neurofeedback*")
        return
    lines = [f"- {t.get('name', 'Unnamed')}" + ("" if t.get("enabled", True) else "  (off)")
             for t in topics]
    reply("Your topics:\n" + "\n".join(lines))


def add_topic(chat_id: str, name: str, query: str) -> tuple[bool, str]:
    """Returns (added, message)."""
    path = chats_store.topics_path(chat_id)
    cfg = load_topics_rt(path) or {"settings": {}, "topics": []}
    if any(t.get("name", "").lower() == name.lower() for t in cfg.get("topics", [])):
        return False, f"A topic named '{name}' already exists. Use /removetopic first to replace it."
    if cfg.get("topics") is None:
        cfg["topics"] = []
    cfg["topics"].append({"name": name, "enabled": True, "query": query})
    save_topics_rt(path, cfg)
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
    cfg = load_topics_rt(path) or {}
    match = next((t for t in (cfg.get("topics") or []) if t.get("name", "").lower() == name.lower()), None)
    if not match:
        reply(f"No topic named '{name}' found. Send /topics to see current names.")
        return
    match["enabled"] = False
    save_topics_rt(path, cfg)
    reply(f"Disabled topic '{name}'.")


def is_too_broad(query: str) -> bool:
    """Defence in depth against a catch-all query slipping through. The prompt tells the
    model never to emit one, but a wrong query here is expensive -- it silently fills a
    whole weekly digest with unrelated papers, and the user has no obvious way to tell
    why. Cheap to check, so check."""
    q = query.strip()
    if not q:
        return True
    # A bare wildcard on any field: TITLE:*, ABSTRACT:*, *, etc.
    if re.fullmatch(r'(\w+:)?\*', q):
        return True
    # Needs at least one real search term of substance, not just field names/operators.
    terms = re.findall(r'[A-Za-z]{3,}', re.sub(r'\b(TITLE|ABSTRACT|AUTH|SRC|AND|OR|NOT)\b', '', q))
    return len(terms) == 0


def handle_natural_language(chat_id: str, text: str, reply) -> None:
    from local_llm import run_prompt   # imported lazily -- most polls never reach here
    try:
        prompt = TOPIC_DRAFT_PROMPT_FILE.read_text(encoding="utf-8")
        raw = run_prompt(prompt, text, max_tokens=300, json_schema=TOPIC_DRAFT_SCHEMA)
        draft = json.loads(raw)
    except Exception as e:
        print(f"WARN topic drafting failed: {e}", file=sys.stderr)
        reply("Couldn't turn that into a search automatically -- try /addtopic <name> | <query> "
              "with an exact query instead (see /help), or rephrase.")
        return

    if not draft.get("is_topic"):
        reason = (draft.get("reason") or "").strip()
        reply(f"That didn't look like a research topic to follow"
              f"{': ' + reason if reason else '.'}\n\n"
              "Try describing a subject, e.g. \"vagus nerve stimulation for epilepsy\", "
              "or send /help for the command list.")
        return

    name, query = draft.get("name", "").strip(), draft.get("query", "").strip()
    if not name or is_too_broad(query):
        print(f"WARN rejected over-broad drafted query: {query!r}", file=sys.stderr)
        reply("I couldn't build a specific enough search from that -- it would have matched "
              "almost everything. Try being more specific, or use /addtopic <name> | <query>.")
        return

    _, msg = add_topic(chat_id, name, query)
    reply(msg)


INTERVAL_WORDS = {
    "daily": 1, "day": 1, "every day": 1,
    "weekly": 7, "week": 7, "every week": 7,
    "biweekly": 14, "fortnightly": 14, "two weeks": 14,
    "monthly": 30, "month": 30,
}


def parse_interval(arg: str) -> int | None:
    """Accepts '3d', '2w', '10 days', 'weekly', 'monthly', or a bare number of days.
    Returns days, or None if it can't be understood."""
    a = arg.strip().lower()
    if not a:
        return None
    if a in INTERVAL_WORDS:
        return INTERVAL_WORDS[a]
    m = re.fullmatch(r"(\d+)\s*(d|day|days|w|wk|week|weeks|m|month|months)?", a)
    if not m:
        return None
    n, unit = int(m.group(1)), (m.group(2) or "d")
    if unit.startswith("w"):
        n *= 7
    elif unit.startswith("m"):
        n *= 30
    if not (chats_store.MIN_INTERVAL_DAYS <= n <= chats_store.MAX_INTERVAL_DAYS):
        return None
    return n


def describe_interval(days: int) -> str:
    return {1: "every day", 7: "every week", 14: "every 2 weeks",
            30: "every month"}.get(days, f"every {days} days")


def handle_schedule(chat_id: str, arg: str, reply) -> None:
    if not arg.strip():
        days = chats_store.get_interval_days(chat_id)
        nxt = chats_store.next_due_at(chat_id)
        reply(f"Reports: {describe_interval(days)}.\n"
              f"Next one due: {nxt:%Y-%m-%d %H:%M} UTC\n\n"
              "Change it with /schedule <interval>, e.g.\n"
              "  /schedule daily\n  /schedule 3d\n  /schedule 2w\n  /schedule monthly")
        return

    days = parse_interval(arg)
    if days is None:
        reply(f"Didn't understand '{arg.strip()}'.\n\n"
              "Try: /schedule daily | weekly | 2w | 10d | monthly\n"
              f"(anything from {chats_store.MIN_INTERVAL_DAYS} to "
              f"{chats_store.MAX_INTERVAL_DAYS} days)")
        return

    chats_store.update_meta(chat_id, interval_days=days)
    nxt = chats_store.next_due_at(chat_id)
    reply(f"Done -- reports now go out {describe_interval(days)}.\n"
          f"Next one due: {nxt:%Y-%m-%d %H:%M} UTC")


def handle_pause(chat_id: str, reply) -> None:
    chats_store.update_meta(chat_id, paused=True)
    reply("Paused -- no scheduled reports until you send /resume.\n"
          "Your topics are kept, and you can still use /report any time.")


def handle_resume(chat_id: str, reply) -> None:
    chats_store.update_meta(chat_id, paused=False)
    nxt = chats_store.next_due_at(chat_id)
    reply(f"Resumed -- {describe_interval(chats_store.get_interval_days(chat_id))}.\n"
          f"Next report due: {nxt:%Y-%m-%d %H:%M} UTC")


def handle_status(chat_id: str, reply) -> None:
    cfg = yaml.safe_load(chats_store.topics_path(chat_id).read_text(encoding="utf-8")) or {}
    topics = [t for t in (cfg.get("topics") or []) if t.get("enabled", True)]
    last = chats_store.last_report_at(chat_id)
    paused = chats_store.is_paused(chat_id)
    lines = [
        f"Topics: {len(topics)} active" + (f" ({', '.join(t['name'] for t in topics[:5])}" +
                                            (", ..." if len(topics) > 5 else "") + ")" if topics else ""),
        f"Schedule: {describe_interval(chats_store.get_interval_days(chat_id))}"
        + (" -- PAUSED" if paused else ""),
        f"Last report: {last:%Y-%m-%d %H:%M} UTC" if last else "Last report: none yet",
    ]
    if not paused:
        lines.append(f"Next report: {chats_store.next_due_at(chat_id):%Y-%m-%d %H:%M} UTC")
    reply("\n".join(lines))


def parse_asterisk_topics(text: str) -> list[str]:
    """Pulls *topic* segments out of a message. Multiple per message is the normal case
    -- `*sleep* *psychedelics*` should add two topics, not one."""
    return [t.strip() for t in re.findall(r"\*([^*]+)\*", text) if t.strip()]


def draft_one_topic(text: str):
    """Returns (ok, name, query, reason) for a single plain-language topic description."""
    from local_llm import run_prompt
    prompt = TOPIC_DRAFT_PROMPT_FILE.read_text(encoding="utf-8")
    raw = run_prompt(prompt, text, max_tokens=300, json_schema=TOPIC_DRAFT_SCHEMA)
    draft = json.loads(raw)
    if not draft.get("is_topic"):
        return False, "", "", (draft.get("reason") or "doesn't look like a research topic")
    name, query = draft.get("name", "").strip(), draft.get("query", "").strip()
    if not name or is_too_broad(query):
        return False, "", "", "couldn't build a specific enough search from that"
    return True, name, query, ""


def handle_asterisk_topics(chat_id: str, wanted: list[str], reply) -> None:
    added, skipped = [], []
    for raw_topic in wanted:
        try:
            ok, name, query, reason = draft_one_topic(raw_topic)
        except Exception as e:
            print(f"WARN drafting failed for {raw_topic!r}: {e}", file=sys.stderr)
            skipped.append(f"{raw_topic} - couldn't build a query for it")
            continue
        if not ok:
            skipped.append(f"{raw_topic} - {reason}")
            continue
        was_added, msg = add_topic(chat_id, name, query)
        (added if was_added else skipped).append(name if was_added else f"{name} - already on your list")

    lines = []
    if added:
        lines.append("Added:\n" + "\n".join(f"  - {a}" for a in added))
    if skipped:
        lines.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
    if not lines:
        lines.append("Nothing to add.")
    if added:
        lines.append("\nNext report goes out automatically this week -- or tap the button "
                     "below for one now.")
    reply("\n\n".join(lines))


# A report takes ~a minute. Without this, tapping the button twice (or tapping it while
# the scheduled run is mid-flight) starts two pipelines for the same chat, which race on
# that chat's seen.json and can deliver a duplicate or half-empty digest.
_running: set[str] = set()
_running_lock = threading.Lock()


def generate_report_now(chat_id: str, reply, scheduled: bool = False) -> None:
    """Runs the full pipeline for one chat. process_chat sends the brief/infographic/
    digest to the chat itself, so nothing extra to deliver here."""
    cfg = yaml.safe_load(chats_store.topics_path(chat_id).read_text(encoding="utf-8")) or {}
    if not any(t.get("enabled", True) for t in (cfg.get("topics") or [])):
        if not scheduled:
            reply("You have no topics yet, so there's nothing to report on.\n\n"
                  "Send them wrapped in asterisks, e.g. *psychedelics* *neurofeedback*")
        return

    with _running_lock:
        if chat_id in _running:
            if not scheduled:
                reply("A report is already being generated for this chat -- hang on.")
            return
        _running.add(chat_id)

    try:
        if not scheduled:
            reply("Working on it -- searching for new papers. This takes a minute.")
        chats_store.mark_report_started(chat_id)
        from datetime import date
        import run_reporter_cloud
        run_reporter_cloud.process_chat(chat_id, date.today().isoformat())
    except Exception as e:
        print(f"ERROR report failed for {chat_id}: {e}", file=sys.stderr)
        reply(f"Report generation failed: {e}")
    finally:
        with _running_lock:
            _running.discard(chat_id)


def handle_update(token: str, upd: dict) -> None:
    """Processes a single Telegram update. Shared by both transports: the GitHub Actions
    poller (getUpdates) and the Railway webhook. Keeping one implementation means the two
    can never drift apart in behaviour -- only in how the update arrives."""

    # --- button presses ----------------------------------------------------------
    cb = upd.get("callback_query")
    if cb:
        chat = (cb.get("message") or {}).get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        answer_callback(token, cb.get("id", ""), "Starting...")
        chats_store.register_chat(chat_id, chat.get("title") or chat.get("first_name", ""))
        reply = lambda t: send(token, chat_id, t)
        if cb.get("data") == "gen_report":
            generate_report_now(chat_id, reply)
        return

    # --- text messages -----------------------------------------------------------
    msg = upd.get("message")
    if not msg or "text" not in msg:
        return

    chat_id = str(msg["chat"]["id"])
    chat_name = msg["chat"].get("title") or msg["chat"].get("username") or msg["chat"].get("first_name", "")
    text = msg["text"].strip()
    reply = lambda t: send(token, chat_id, t)

    is_new = chats_store.register_chat(chat_id, chat_name)
    if is_new:
        # New chat: always greet first, then still act on the message if it actually
        # carried topics, so `*sleep*` as a first contact isn't ignored.
        reply(WELCOME_TEXT)
        if not parse_asterisk_topics(text):
            return

    topics_in_msg = parse_asterisk_topics(text)

    if text in ("/help", "/start"):
        reply(HELP_TEXT if text == "/help" else WELCOME_TEXT)
    elif text == "/topics":
        handle_topics(chat_id, reply)
    elif text == "/status":
        handle_status(chat_id, reply)
    elif text.startswith("/schedule"):
        handle_schedule(chat_id, text[len("/schedule"):], reply)
    elif text == "/pause":
        handle_pause(chat_id, reply)
    elif text == "/resume":
        handle_resume(chat_id, reply)
    elif text in ("/report", "/reportnow"):
        generate_report_now(chat_id, reply)
    elif text.startswith("/addtopic"):
        handle_addtopic(chat_id, text[len("/addtopic"):].strip(), reply)
    elif text.startswith("/removetopic"):
        handle_removetopic(chat_id, text[len("/removetopic"):].strip(), reply)
    elif text.startswith("/"):
        reply("Unknown command. Send /help for the list.")
    elif topics_in_msg:
        handle_asterisk_topics(chat_id, topics_in_msg, reply)
    else:
        # Anything else gets the greeting + instructions rather than being guessed at
        # as a topic -- guessing is how a plain "hi" once became a TITLE:* topic that
        # would have matched every paper in the database.
        reply(WELCOME_TEXT)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1

    offset = load_offset()
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                             params={"offset": offset + 1, "timeout": 0}, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code in (401, 404):
            print(f"ERROR Telegram rejected the bot token (HTTP {resp.status_code}) -- "
                  f"check the TELEGRAM_BOT_TOKEN repo secret for a typo or stray whitespace. "
                  f"Response: {resp.text[:300]}", file=sys.stderr)
        else:
            print(f"ERROR getUpdates failed: {e}. Response: {resp.text[:300]}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as e:
        print(f"ERROR getUpdates request failed: {e}", file=sys.stderr)
        return 1

    updates = resp.json().get("result", [])

    max_update_id = offset

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        handle_update(token, upd)

    if max_update_id > offset:
        save_offset(max_update_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
