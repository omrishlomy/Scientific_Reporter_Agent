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


# One call per topic produces both the infographic card and the NotebookLM notes, so the
# richer document doesn't double the Groq calls against the free tier's daily budget.
TOPIC_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "gist": {"type": "string"},
        "background": {"type": "string"},
        "connections": {"type": "string"},
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "short_title": {"type": "string"},
                    "finding": {"type": "string"},
                    "question": {"type": "string"},
                    "approach": {"type": "string"},
                    "key_results": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "limitations": {"type": "string"},
                },
                "required": ["index", "short_title", "finding", "question", "approach",
                             "key_results", "why_it_matters", "limitations"],
                "additionalProperties": False,
            },
        },
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "definition": {"type": "string"}},
                "required": ["term", "definition"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["gist", "background", "connections", "papers", "glossary"],
    "additionalProperties": False,
}

NOTES_MAX_TOKENS = 3200


def safe_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = " ".join(name.split()).rstrip(". ")
    return name[:max_len].rstrip(". ")


def topics_label(names: list[str], limit: int = 60) -> str:
    """'Respiration and the brain, Psychedelics +2 more' -- short enough for a file name."""
    shown: list[str] = []
    for n in names:
        if shown and len(", ".join(shown + [n])) > limit:
            break
        shown.append(n)
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" +{rest} more" if rest else "")


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
    parts = line.split("|")
    count_str, digest_path, sources_path = parts[:3]
    papers_json_path = parts[3] if len(parts) > 3 else None
    count = int(count_str)
    log(chat_id, f"{count} paper(s) -> {digest_path}")

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
    from local_llm import run_prompt, estimate_tokens, budget
    import digest_compact
    import notebooklm_doc

    topics = digest_compact.parse_digest(digest_text)

    # Structured paper records (pmcid, open-access flag, earlier-paper flag) for the
    # NotebookLM document. Fall back to what the markdown digest carries if absent.
    if papers_json_path and pathlib.Path(papers_json_path).exists():
        harvest = json.loads(pathlib.Path(papers_json_path).read_text(encoding="utf-8"))
        records, lookback = harvest.get("topics") or [], int(harvest.get("lookback_days") or 7)
    else:
        records, lookback = [{"name": t["name"], "note": t.get("note", ""), "papers": [
            {"title": p["title"], "abstract": p["abstract"], "preprint": p["preprint"],
             "link": "", "backfilled": False} for p in t["papers"]]} for t in topics], 7

    # --- informative file names ---------------------------------------------------
    label = topics_label([t["name"] for t in topics])
    def named(kind: str, ext: str) -> pathlib.Path:
        return unique_path(out_dir, safe_filename(f"{today} {kind} - {label}"), ext)

    for old, kind in ((digest_path, "Paper list with links"), (sources_path, "Sources")):
        new = named(kind, ".md")
        pathlib.Path(old).rename(new)
        if old == digest_path:
            digest_path = str(new)
        else:
            sources_path = str(new)

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

    # --- 3. per-topic notes + infographic cards -------------------------------------
    # One call per topic returns both the NotebookLM notes and the card. Per topic keeps
    # each request under the per-minute budget, and one bad topic costs one topic rather
    # than the whole report. Names and counts come from the digest, not the model.
    notes_prompt = (HERE / "topic-notes-prompt.md").read_text(encoding="utf-8")
    infographic_prompt = (HERE / "infographic-prompt.md").read_text(encoding="utf-8")
    records_by_name = {rec["name"]: rec for rec in records}
    cards, notes, notes_failed = [], {}, []
    for t in topics:
        rec = records_by_name.get(t["name"])
        card = None
        if rec:
            try:
                # Shrink abstracts until the request fits, rather than failing a topic
                # that happens to have long titles or many papers.
                user = None
                for chars in (650, 500, 350, 220):
                    candidate = notebooklm_doc.numbered_topic_input(rec, chars)
                    if estimate_tokens(notes_prompt, candidate) + NOTES_MAX_TOKENS <= budget():
                        user = candidate
                        break
                if user is None:
                    raise RuntimeError("topic too large for one request even with short abstracts")
                raw = run_prompt(notes_prompt, user, max_tokens=NOTES_MAX_TOKENS,
                                 json_schema=TOPIC_NOTES_SCHEMA, reasoning_effort="low")
                data = json.loads(re.sub(r"```\s*$", "", re.sub(r"^```(json)?\s*", "", raw.strip())))
                if not isinstance(data.get("papers"), list):
                    raise RuntimeError("notes response missing papers")
                notes[t["name"]] = data
                ordered = sorted((e for e in data["papers"] if isinstance(e, dict)),
                                 key=lambda e: e.get("index", 0))
                card = {"name": t["name"], "count": t["count"], "gist": data.get("gist", ""),
                        "papers": [{"title": e.get("short_title", ""), "finding": e.get("finding", "")}
                                   for e in ordered]}
            except Exception as e:
                log(chat_id, f"topic notes failed for {t['name']!r}: {e}")
                notes_failed.append(t["name"])
        if card is None:
            # Cheaper card-only call, so a notes failure doesn't also cost the infographic.
            try:
                raw = run_prompt(infographic_prompt, digest_compact.compact([t], abstract_chars=700),
                                 max_tokens=1500, json_schema=INFOGRAPHIC_SCHEMA,
                                 reasoning_effort="low")
                raw = re.sub(r"```\s*$", "", re.sub(r"^```(json)?\s*", "", raw.strip()))
                items = json.loads(raw).get("topics") or []
                if items:
                    card = items[0]
                    card["name"], card["count"] = t["name"], t["count"]
            except Exception as e:
                log(chat_id, f"infographic data failed for topic {t['name']!r}: {e}")
        if card:
            cards.append(card)
    json_block = json.dumps({"topics": cards}, ensure_ascii=False, indent=2) if cards else None

    # --- write the brief file ----------------------------------------------------
    brief_path = named("Written brief", ".md")
    header = (f"# Written brief - {today}\n\n"
              f"Synthesis of {count} paper(s). Sources: {pathlib.Path(sources_path).name}\n\n---\n\n")
    body = brief_prose
    if json_block:
        body += f"\n\n```json\n{json_block}\n```"
    brief_path.write_text(header + body, encoding="utf-8")
    log(chat_id, f"brief -> {brief_path}")

    # --- 4. infographic -----------------------------------------------------------
    infographic_path = None
    if json_block:
        infographic_path = named("Infographic", ".png")
        r = sh([sys.executable, str(HERE / "make_infographic.py"),
                "--brief", str(brief_path), "--out", str(infographic_path), "--date", today])
        if r.returncode == 0 and infographic_path.exists():
            log(chat_id, f"infographic -> {infographic_path}")
        else:
            log(chat_id, f"infographic not generated: {r.stdout} {r.stderr}")
            infographic_path = None

    # --- 5. NotebookLM source document ------------------------------------------------
    full_texts: dict[str, list] = {}
    for rec in records:
        for p in rec["papers"]:
            if p.get("link"):
                sections = notebooklm_doc.fetch_full_text(p)
                if sections:
                    full_texts[p["link"]] = sections
    notebooklm_path = named("NotebookLM source", ".md")
    notebooklm_path.write_text(
        notebooklm_doc.build_document(today, label, records, brief_prose, notes, full_texts, lookback),
        encoding="utf-8")
    log(chat_id, f"notebooklm -> {notebooklm_path} ({len(full_texts)} full text(s))")

    # --- 6. notify ------------------------------------------------------------
    # Not "new": thin topics are topped up with earlier unseen papers, and the digest
    # and brief say which ones those are.
    subject = f"Research report - {count} paper(s) - {label}"
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
    if notes_failed:
        missing.append(f"plain-language notes for {len(notes_failed)} topic(s)")
    if missing:
        tg_message += ("\n\n(Couldn't generate " + " and ".join(missing) +
                       " this time -- the paper list and abstracts are complete.)")

    tg_cmd = [sys.executable, str(HERE / "telegram_notify.py"), "--chat-id", chat_id,
              "--message", tg_message,
              "--document", str(notebooklm_path),
              "For NotebookLM: add this file as a source, then choose Audio Overview.",
              "--document", digest_path, "All papers with abstracts and links."]
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
