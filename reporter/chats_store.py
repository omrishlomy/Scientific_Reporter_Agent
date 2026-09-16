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
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

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
DEFAULT_WEEKDAY = 0            # Monday
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 90

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

try:
    LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Jerusalem"))
except Exception:
    LOCAL_TZ = timezone.utc

# Scheduled reports land on fixed calendar slots counted from this Monday: weekly means
# every Monday (or the chosen weekday) at the chosen hour, every-2-weeks means alternate
# ones, and so on. The first version instead counted N days from the LAST report of any
# kind, so tapping "Generate now" on a Saturday silently moved the weekly report to the
# following Saturday -- the Monday report the user expected never came.
SLOT_EPOCH = date(2024, 1, 1)


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


def get_weekday(chat_id: str) -> int:
    try:
        v = int(load_meta(chat_id).get("weekday", DEFAULT_WEEKDAY))
    except (TypeError, ValueError):
        return DEFAULT_WEEKDAY
    return v if 0 <= v <= 6 else DEFAULT_WEEKDAY


def is_paused(chat_id: str) -> bool:
    return bool(load_meta(chat_id).get("paused", False))


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def last_report_at(chat_id: str) -> datetime | None:
    """Most recent report of any kind, scheduled or on demand. Display only -- it does
    not affect when the next scheduled report goes out."""
    return _parse_ts(load_meta(chat_id).get("last_report_at"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mark_report_started(chat_id: str) -> None:
    """On-demand report. Deliberately leaves the schedule untouched."""
    update_meta(chat_id, last_report_at=_now().isoformat())


def mark_scheduled_started(chat_id: str) -> None:
    """Stamped when a scheduled run BEGINS, not when it finishes: a run that crashes
    halfway costs that slot instead of re-running an expensive pipeline on every tick."""
    stamp = _now().isoformat()
    update_meta(chat_id, last_scheduled_at=stamp, last_report_at=stamp)


def mark_schedule_changed(chat_id: str) -> None:
    """Changing the schedule or resuming must not fire a report for a slot that already
    passed -- the next report is the next slot from now."""
    update_meta(chat_id, schedule_changed_at=_now().isoformat())


def _slot(d: date, hour: int) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=LOCAL_TZ)


def latest_slot(chat_id: str, now: datetime | None = None) -> datetime:
    """The most recent scheduled slot at or before `now` (returned in UTC)."""
    now = now or _now()
    local_now = now.astimezone(LOCAL_TZ)
    interval, hour = get_interval_days(chat_id), get_hour(chat_id)
    anchor = SLOT_EPOCH + timedelta(days=get_weekday(chat_id))
    d = anchor + timedelta(days=((local_now.date() - anchor).days // interval) * interval)
    slot = _slot(d, hour)
    if slot > local_now:
        slot = _slot(d - timedelta(days=interval), hour)
    return slot.astimezone(timezone.utc)


def _baseline(chat_id: str) -> datetime | None:
    """Slots at or before this moment are considered handled: the chat didn't exist yet,
    a scheduled report already went out for them, or the schedule was changed after."""
    meta = load_meta(chat_id)
    stamps = [_parse_ts(meta.get(k)) for k in
              ("registered_at", "last_scheduled_at", "schedule_changed_at")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def is_due(chat_id: str, now: datetime | None = None) -> bool:
    """True when a scheduled slot has passed without a report. Because it compares
    against the latest slot rather than an exact time, a slot missed while the service
    was down or asleep still fires on the next tick instead of being skipped."""
    if is_paused(chat_id):
        return False
    now = now or _now()
    base = _baseline(chat_id)
    return base is None or latest_slot(chat_id, now) > base


def next_due_at(chat_id: str, now: datetime | None = None) -> datetime:
    """When the next scheduled report goes out (UTC). If one is overdue, that slot."""
    now = now or _now()
    last = latest_slot(chat_id, now)
    if is_due(chat_id, now):
        return last
    d = last.astimezone(LOCAL_TZ).date() + timedelta(days=get_interval_days(chat_id))
    return _slot(d, get_hour(chat_id)).astimezone(timezone.utc)


def describe_schedule(chat_id: str) -> str:
    interval, hour, wd = get_interval_days(chat_id), get_hour(chat_id), get_weekday(chat_id)
    at = f"at {hour:02d}:00"
    if interval == 1:
        return f"every day {at}"
    if interval == 7:
        return f"every {WEEKDAY_NAMES[wd]} {at}"
    if interval == 14:
        return f"every other {WEEKDAY_NAMES[wd]} {at}"
    if interval % 7 == 0:
        return f"every {interval // 7} weeks on {WEEKDAY_NAMES[wd]} {at}"
    return f"every {interval} days {at}"


def format_local(ts: datetime) -> str:
    return ts.astimezone(LOCAL_TZ).strftime("%a %d %b, %H:%M")


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
