"""
Cloud orchestration for the reporter, run by .github/workflows/reporter.yml on a
GitHub Actions schedule. Python port of run-reporter.ps1 for the Linux runner:
  1. fetch_papers.py       -> digest-DATE.md + sources-DATE.md, under --out
  2. local_llm (synthesis) -> brief prose (open-weights model, not `claude -p`)
  3. local_llm (extraction)-> per-topic JSON, read from the RAW DIGEST (not the brief)
  4. make_infographic.py   -> infographic-DATE.png
  5. telegram_notify.py    -> message + infographic photo + digest document

State (seen.json) is committed back to the repo by the workflow YAML, not by this
script -- this script only writes local files and reports what happened via stdout,
same separation of concerns as the PowerShell version had between run-reporter.ps1
and the scheduled task.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from datetime import date

HERE = pathlib.Path(__file__).parent
OUT_DIR = pathlib.Path("reporter-output")   # relative -- lives inside the checked-out repo

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


def log(msg: str) -> None:
    print(f"[run_reporter_cloud] {msg}", file=sys.stderr)


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def unique_path(stem: str, ext: str) -> pathlib.Path:
    p = OUT_DIR / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = OUT_DIR / f"{stem} ({n}){ext}"
        n += 1
    return p


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    # --- 1. harvest -------------------------------------------------------------
    r = sh([sys.executable, str(HERE / "fetch_papers.py"), "--out", str(OUT_DIR)])
    print(r.stdout)
    if r.returncode != 0:
        log(f"fetch_papers.py failed: {r.stderr}")
        return r.returncode

    line = next((l for l in r.stdout.splitlines() if re.match(r"^\d+\|", l)), None)
    if not line:
        log(f"unexpected fetch_papers.py output: {r.stdout!r}")
        return 1
    count_str, digest_path, sources_path = line.split("|", 2)
    count = int(count_str)
    log(f"{count} new paper(s) -> {digest_path}")

    if count == 0:
        sh([sys.executable, str(HERE / "telegram_notify.py"),
            "--message", "Weekly paper digest: nothing new this week."])
        return 0

    digest_text = pathlib.Path(digest_path).read_text(encoding="utf-8")

    # --- 2. synthesis (Groq-hosted open-weights model) --------------------------
    from local_llm import run_prompt   # imported here so --help works without GROQ_API_KEY set

    synthesis_prompt = (HERE / "synthesis-prompt.md").read_text(encoding="utf-8")
    try:
        # Generous budget: unlike the retired CPU-inference path, more tokens here
        # cost seconds on Groq's hardware, not extra tens of minutes -- the previous
        # tight budgets were sized to limit wall-clock time on a bottleneck that no
        # longer exists, and 1200 tokens was measured to truncate the JSON below.
        brief_prose = run_prompt(synthesis_prompt, digest_text, max_tokens=2000)
        brief_prose = re.sub(r"^\s*#\s+[^\n]*\n+", "", brief_prose)   # drop a stray H1
    except Exception as e:
        log(f"synthesis failed: {e}; continuing without a brief")
        brief_prose = ""

    # --- 3. infographic data: grounded in the RAW DIGEST, not the brief ---------
    # Mirrors run-reporter.ps1's design: the infographic's job is comprehension of
    # what the papers found, not a restatement of the brief's critical synthesis.
    infographic_prompt = (HERE / "infographic-prompt.md").read_text(encoding="utf-8")
    json_block = None
    for attempt in (1, 2):
        try:
            json_raw = run_prompt(infographic_prompt, digest_text, max_tokens=3000,
                                   json_schema=INFOGRAPHIC_SCHEMA)
            json_raw = re.sub(r"^```(json)?\s*", "", json_raw.strip())
            json_raw = re.sub(r"```\s*$", "", json_raw)
            json.loads(json_raw)   # validate before trusting it -- syntax only; strict
                                    # mode already guarantees the schema itself
            json_block = json_raw
            break
        except Exception as e:
            log(f"infographic-data attempt {attempt} failed: {e}")

    # --- write the brief file ----------------------------------------------------
    brief_path = unique_path(f"brief-{today}", ".md")
    header = (f"# Weekly brief - {today}\n\n"
              f"Synthesis of {count} new paper(s). Sources: {pathlib.Path(sources_path).name}\n\n---\n\n")
    body = brief_prose
    if json_block:
        body += f"\n\n```json\n{json_block}\n```"
    brief_path.write_text(header + body, encoding="utf-8")
    log(f"brief -> {brief_path}")

    # --- 4. infographic -----------------------------------------------------------
    infographic_path = None
    if json_block:
        infographic_path = unique_path(f"infographic-{today}", ".png")
        r = sh([sys.executable, str(HERE / "make_infographic.py"),
                "--brief", str(brief_path), "--out", str(infographic_path), "--date", today])
        if r.returncode == 0 and infographic_path.exists():
            log(f"infographic -> {infographic_path}")
        else:
            log(f"infographic not generated: {r.stdout} {r.stderr}")
            infographic_path = None

    # --- 5. notify ------------------------------------------------------------
    subject = f"Weekly paper digest - {count} new paper(s)"
    tg_message = subject
    if brief_prose:
        preview = brief_prose[:3500] + "..." if len(brief_prose) > 3500 else brief_prose
        tg_message = f"{subject}\n\n{preview}"

    tg_cmd = [sys.executable, str(HERE / "telegram_notify.py"), "--message", tg_message,
              "--document", digest_path]
    if infographic_path:
        tg_cmd += ["--photo", str(infographic_path)]
    r = sh(tg_cmd)
    log(f"telegram: exit {r.returncode} {r.stderr}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
