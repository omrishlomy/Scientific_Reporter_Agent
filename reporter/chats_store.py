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
import os
import pathlib
import shutil
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).parent

# Where chat state lives. On GitHub Actions this is the checked-out repo (state is
# committed back to git). On Railway the container filesystem is ephemeral, so DATA_DIR
# points at a mounted volume instead -- anything written outside it is lost on redeploy.
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", str(HERE)))
CHATS_DIR = DATA_DIR / "chats"

# Chats shipped in the repo, used to seed an empty volume on first boot.
BUNDLED_CHATS_DIR = HERE / "chats"

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


# --- per-chat schedule + delivery state ---------------------------------------------
# Kept in meta.json rather than topics.yaml on purpose: topics.yaml is hand-editable and
# comment-preserving, this is machine-written bookkeeping. Mixing them would mean the bot
# rewriting a documented file every time it sends a report.

DEFAULT_INTERVAL_DAYS = 7
DEFAULT_HOUR = 8
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 90


def load_meta(chat_id: str) -> dict:
    p = meta_path(chat_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt metadata must not take a chat out of service permanently.
            pass
    return {}


def save_meta(chat_id: str, meta: dict) -> None:
    meta_path(chat_id).parent.mkdir(parents=True, exist_ok=True)
    meta_path(chat_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def update_meta(chat_id: str, **fields) -> dict:
    meta = load_meta(chat_id)
    meta.update(fields)
    save_meta(chat_id, meta)
    return meta


def get_interval_days(chat_id: str) -> int:
    try:
        v = int(load_meta(chat_id).get("interval_days", DEFAULT_INTERVAL_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_DAYS
    return max(MIN_INTERVAL_DAYS, min(MAX_INTERVAL_DAYS, v))


def get_hour(chat_id: str) -> int:
    try:
        v = int(load_meta(chat_id).get("hour", DEFAULT_HOUR))
    except (TypeError, ValueError):
        return DEFAULT_HOUR
    return v if 0 <= v <= 23 else DEFAULT_HOUR


def is_paused(chat_id: str) -> bool:
    return bool(load_meta(chat_id).get("paused", False))


def last_report_at(chat_id: str) -> datetime | None:
    raw = load_meta(chat_id).get("last_report_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def mark_report_started(chat_id: str) -> None:
    """Stamped when a run BEGINS, not when it finishes. If a run crashes halfway, the
    chat waits for its next interval instead of retrying on every scheduler tick --
    a crash loop that re-ran an expensive pipeline forever would be far worse than
    missing one digest."""
    update_meta(chat_id, last_report_at=datetime.now(timezone.utc).isoformat())


def next_due_at(chat_id: str) -> datetime:
    """When this chat's next scheduled digest is due (UTC)."""
    last = last_report_at(chat_id)
    if last is None:
        return datetime.now(timezone.utc)   # never sent -> due now
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last + timedelta(days=get_interval_days(chat_id))


def is_due(chat_id: str, now: datetime | None = None) -> bool:
    if is_paused(chat_id):
        return False
    # Never reported: due immediately. Checked explicitly rather than via next_due_at,
    # which returns "now" for this case -- and its now() is evaluated microseconds after
    # the caller's, so `now >= next_due_at()` came out False and a brand new chat waited
    # a whole tick for its first report.
    if last_report_at(chat_id) is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now >= next_due_at(chat_id)


def seed_from_bundle() -> int:
    """Copies repo-bundled chats into the volume the first time it's used, so migrating
    from the GitHub Actions setup doesn't lose existing topics or dedup history. Only
    copies chats that aren't already in the volume -- the volume always wins, since it
    is the live state and the bundled copy is a frozen snapshot from build time."""
    if CHATS_DIR.resolve() == BUNDLED_CHATS_DIR.resolve() or not BUNDLED_CHATS_DIR.exists():
        return 0
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in BUNDLED_CHATS_DIR.iterdir():
        if not src.is_dir() or not (src / "topics.yaml").exists():
            continue
        dest = CHATS_DIR / src.name
        if dest.exists():
            continue
        shutil.copytree(src, dest)
        copied += 1
    return copied
