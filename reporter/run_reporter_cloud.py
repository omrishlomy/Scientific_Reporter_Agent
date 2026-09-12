"""
Cloud orchestration for the reporter, run by .github/workflows/reporter.yml on a
GitHub Actions schedule. MULTI-TENANT: runs the full pipeline once per registered chat
(see chats_store.py), each with its own topics, dedup history, and Telegram send --
two chats with overlapping topics must each independently see a paper neither of them
has been sent yet, so state cannot be shared globally.

Per chat:
  1. fetch_papers.py       -> digest-DATE.md + sources-DATE.md, under this chat's --out
  2. local_llm (synthesis) -> brief prose (Groq-hosted open-weights model)
  3. local_llm (extraction)-> per-topic JSON, read from the RAW DIGEST (not the brief)
  4. make_infographic.py   -> infographic-DATE.png
  5. telegram_notify.py    -> message + infographic photo + digest document, to that
                               chat's chat_id specifically

State (each chat's seen.json) is committed back to the repo by the workflow YAML, not
by this script -- this script only writes local files and reports what happened via
stdout/stderr, same separation of concerns the PowerShell version had between
run-reporter.ps1 and the scheduled task.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import yaml
from datetime import date

import chats_store

HERE = pathlib.Path(__file__).parent
OUT_ROOT = pathlib.Path("reporter-output")   # relative -- lives inside the checked-out repo

# Passed to Groq as a strict response_format -- see local_llm.run_prompt's docstring
# for why: this makes gpt-oss-120b's output structurally guaranteed to match (right
# field names, right nesting), rather than hoping the prompt's instructions are
# followed. Strict mode requires every field listed in "required" and
# "additionalProperties": false at every object level -- both Claude and the earlier
# local Qwen model drifted from the requested field names under prompt-only
# instructions, so this is a real fix, not an extra layer of hope.
INFOGRAPHIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "gist": {"type": "string"},
                    "papers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "finding": {"type": "string"},
                            },
                            "required": ["title", "finding"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "count", "gist", "papers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def log(chat_id: str, msg: str) -> None:
    print(f"[run_reporter_cloud][{chat_id}] {msg}", file=sys.stderr)


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def unique_path(out_dir: pathlib.Path, stem: str, ext: str) -> pathlib.Path:
    p = out_dir / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = out_dir / f"{stem} ({n}){ext}"
        n += 1
    return p


def has_enabled_topics(chat_id: str) -> bool:
    cfg = yaml.safe_load(chats_store.topics_path(chat_id).read_text(encoding="utf-8")) or {}
    return any(t.get("enabled", True) for t in cfg.get("topics", []))


def process_chat(chat_id: str, today: str) -> None:
    if not has_enabled_topics(chat_id):
        log(chat_id, "no enabled topics yet; skipping this week")
        return

    out_dir = OUT_ROOT / chat_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. harvest -------------------------------------------------------------
    r = sh([sys.executable, str(HERE / "fetch_papers.py"),
            "--out", str(out_dir),
            "--topics-file", str(chats_store.topics_path(chat_id)),
            "--seen-file", str(chats_store.seen_path(chat_id))])
    if r.returncode != 0:
        log(chat_id, f"fetch_papers.py failed: {r.stderr}")
        return

    line = next((l for l in r.stdout.splitlines() if re.match(r"^\d+\|", l)), None)
    if not line:
        log(chat_id, f"unexpected fetch_papers.py output: {r.stdout!r}")
        return
    count_str, digest_path, sources_path = line.split("|", 2)
    count = int(count_str)
    log(chat_id, f"{count} new paper(s) -> {digest_path}")

    if count == 0:
        # Only reachable now if a topic matches nothing unseen even several years back
        # (fetch_papers tops thin topics up from earlier papers), so the useful thing to
        # say is that the topics are probably too narrow -- not "nothing new this week".
        sh([sys.executable, str(HERE / "telegram_notify.py"), "--chat-id", chat_id,
            "--message", "I couldn't find any papers you haven't already been sent, even "
                         "searching several years back. Your topics may be too narrow -- "
                         "send /topics to review them, or add broader ones like "
                         "*sleep and memory*."])
        return

    digest_text = pathlib.Path(digest_path).read_text(encoding="utf-8")

    # The LLM calls get a COMPACT view of the digest, never the raw file. A normal week is
    # ~13K tokens; Groq's free tier caps a request at 8K tokens/minute, so the raw digest
    # was rejected outright and both the brief and the infographic silently disappeared.
    # The full digest still goes to the user for NotebookLM.
    from local_llm import run_prompt   # imported here so --help works without GROQ_API_KEY set
    import digest_compact

    topics = digest_compact.parse_digest(digest_text)

    # --- 2. synthesis -----------------------------------------------------------
    synthesis_prompt = (HERE / "synthesis-prompt.md").read_text(encoding="utf-8")
    try:
        brief_prose = run_prompt(synthesis_prompt,
                                 digest_compact.compact(topics, abstract_chars=350),
                                 max_tokens=2500, reasoning_effort="medium")
        brief_prose = re.sub(r"^\s*#\s+[^\n]*\n+", "", brief_prose)   # drop a stray H1
    except Exception as e:
        log(chat_id, f"synthesis failed: {e}; continuing without a brief")
        brief_prose = ""

    # --- 3. infographic data: one call per topic ---------------------------------
    # Per topic keeps each request small, and means one bad topic costs one card rather
    # than the whole infographic. Counts and names come from the digest itself, not the
    # model, so a card can never disagree with the digest it summarises.
    infographic_prompt = (HERE / "infographic-prompt.md").read_text(encoding="utf-8")
    cards = []
    for t in topics:
        try:
            raw = run_prompt(infographic_prompt, digest_compact.compact([t], abstract_chars=700),
                             max_tokens=1500, json_schema=INFOGRAPHIC_SCHEMA,
                             reasoning_effort="low")
            raw = re.sub(r"```\s*$", "", re.sub(r"^```(json)?\s*", "", raw.strip()))
            items = json.loads(raw).get("topics") or []
            if items:
                card = items[0]
                card["name"], card["count"] = t["name"], t["count"]
                cards.append(card)
        except Exception as e:
            log(chat_id, f"infographic data failed for topic {t['name']!r}: {e}")
    json_block = json.dumps({"topics": cards}, ensure_ascii=False, indent=2) if cards else None

    # --- write the brief file ----------------------------------------------------
    brief_path = unique_path(out_dir, f"brief-{today}", ".md")
    header = (f"# Weekly brief - {today}\n\n"
              f"Synthesis of {count} new paper(s). Sources: {pathlib.Path(sources_path).name}\n\n---\n\n")
    body = brief_prose
    if json_block:
        body += f"\n\n```json\n{json_block}\n```"
    brief_path.write_text(header + body, encoding="utf-8")
    log(chat_id, f"brief -> {brief_path}")

    # --- 4. infographic -----------------------------------------------------------
    infographic_path = None
    if json_block:
        infographic_path = unique_path(out_dir, f"infographic-{today}", ".png")
        r = sh([sys.executable, str(HERE / "make_infographic.py"),
                "--brief", str(brief_path), "--out", str(infographic_path), "--date", today])
        if r.returncode == 0 and infographic_path.exists():
            log(chat_id, f"infographic -> {infographic_path}")
        else:
            log(chat_id, f"infographic not generated: {r.stdout} {r.stderr}")
            infographic_path = None

    # --- 5. notify ------------------------------------------------------------
    # Not "new": thin topics are topped up with earlier unseen papers, and the digest
    # and brief say which ones those are.
    subject = f"Paper digest - {count} paper(s)"
    tg_message = subject
    if brief_prose:
        preview = brief_prose[:3300] + "..." if len(brief_prose) > 3300 else brief_prose
        tg_message = f"{subject}\n\n{preview}"

    # Say so when a part is missing. Before this, a failed brief or infographic just
    # vanished and the report looked complete -- which hid a rate-limit failure that had
    # been breaking both on every run.
    missing = []
    if not brief_prose:
        missing.append("the written brief")
    if not infographic_path:
        missing.append("the infographic")
    elif len(cards) < len(topics):
        missing.append(f"infographic cards for {len(topics) - len(cards)} topic(s)")
    if missing:
        tg_message += ("\n\n(Couldn't generate " + " and ".join(missing) +
                       " this time -- the full digest file below is complete.)")

    tg_cmd = [sys.executable, str(HERE / "telegram_notify.py"), "--chat-id", chat_id,
              "--message", tg_message, "--document", digest_path]
    if infographic_path:
        tg_cmd += ["--photo", str(infographic_path)]
    r = sh(tg_cmd)
    log(chat_id, f"telegram: exit {r.returncode} {r.stderr}")


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    chat_ids = chats_store.list_chat_ids()
    if not chat_ids:
        print("No registered chats yet -- message the bot on Telegram first.", file=sys.stderr)
        return 0

    for chat_id in chat_ids:
        try:
            process_chat(chat_id, today)
        except Exception as e:
            # One chat's failure must not take down everyone else's digest.
            log(chat_id, f"unhandled error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
