"""
Shared chat-registry helpers for multi-tenant operation. A chat is "registered" by
the existence of reporter/chats/<chat_id>/ -- there is no separate index file to fall
out of sync with reality, since the filesystem itself is the source of truth. Used by
both bot_poll.py (registers new chats, edits their topics) and run_reporter_cloud.py
(enumerates chats to run the weekly digest for each).

Each chat gets its own topics.yaml and seen.json, because dedup has to be per-chat:
two chats can both be interested in "psychedelics" and both independently need to see
a paper neither of them has been sent yet, regardless of what the other chat has seen.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).parent
CHATS_DIR = HERE / "chats"

DEFAULT_TOPICS_YAML = """\
# Topics for this chat's weekly paper digest.
# Managed via Telegram commands (/addtopic, /removetopic, /topics) -- edit by hand
# too if you prefer, same format either way.

settings:
  lookback_days: 7
  max_per_topic: 8
  preprints: true

topics: []
"""


def topics_path(chat_id: str) -> pathlib.Path:
    return CHATS_DIR / str(chat_id) / "topics.yaml"


def seen_path(chat_id: str) -> pathlib.Path:
    return CHATS_DIR / str(chat_id) / "seen.json"


def meta_path(chat_id: str) -> pathlib.Path:
    return CHATS_DIR / str(chat_id) / "meta.json"


def is_registered(chat_id: str) -> bool:
    return topics_path(chat_id).exists()


def register_chat(chat_id: str, chat_name: str = "") -> bool:
    """Creates the chat's directory + a fresh empty topics.yaml if it doesn't already
    exist. Returns True if this call actually created it (i.e. this is a brand new
    chat -- the caller uses this to decide whether to send an onboarding message)."""
    if is_registered(chat_id):
        return False
    chat_dir = CHATS_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    topics_path(chat_id).write_text(DEFAULT_TOPICS_YAML, encoding="utf-8")
    seen_path(chat_id).write_text("[]\n", encoding="utf-8")
    meta_path(chat_id).write_text(json.dumps({
        "chat_name": chat_name,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    return True


def list_chat_ids() -> list[str]:
    if not CHATS_DIR.exists():
        return []
    return sorted(p.name for p in CHATS_DIR.iterdir() if p.is_dir() and (p / "topics.yaml").exists())
