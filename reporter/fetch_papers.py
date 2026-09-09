"""
Weekly paper digest.

Pulls new papers per topic from Europe PMC and writes one markdown file designed to
be dropped straight into NotebookLM as a source. Abstracts are kept verbatim and in
full -- NotebookLM does the synthesis, so pre-summarising here would only throw away
the material it needs.

Europe PMC indexes PubMed *and* bioRxiv/medRxiv, so one keyless API covers published
work and preprints. No API key, no dependencies beyond pyyaml.

Papers already reported are recorded in seen.json and never repeat.

    python fetch_papers.py                 # normal weekly run
    python fetch_papers.py --dry-run       # print to stdout, record nothing
    python fetch_papers.py --days 30       # widen the window (useful for a first run)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

# Abstracts routinely contain math and Greek (>=, alpha, mu). The Windows console
# defaults to cp1252 and raises on those, so force UTF-8 on both streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).parent
TOPICS_FILE = HERE / "topics.yaml"
SEEN_FILE = HERE / "seen.json"
DEFAULT_OUT = pathlib.Path(r"G:\My Drive\Research\Digests")

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Europe PMC asks for identification and is generous in return. Be polite.
UA = "ArziLab-paper-digest/1.0 (mailto:omrishlomy44@gmail.com)"
TIMEOUT = 30


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            # A corrupt seen-list must not stop the digest; worst case we repeat a week.
            print("WARN seen.json unreadable, starting fresh", file=sys.stderr)
    return set()


def save_seen(seen: set[str]) -> None:
    # Keep it bounded: 5000 ids is years of history at this volume.
    trimmed = sorted(seen)[-5000:]
    SEEN_FILE.write_text(json.dumps(trimmed, indent=0), encoding="utf-8")


def search(query: str, since: dt.date, limit: int, preprints: bool) -> list[dict]:
    """One Europe PMC query, newest first, restricted to a date window."""
    date_clause = f'(FIRST_PDATE:[{since.isoformat()} TO {dt.date.today().isoformat()}])'
    full = f"({query.strip()}) AND {date_clause}"
    if not preprints:
        full += ' AND (SRC:"MED")'

    params = {
        "query": full,
        "format": "json",
        "pageSize": str(min(limit, 100)),
        "resultType": "core",          # 'core' is what includes abstractText
        "sort": "P_PDATE_D desc",
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        # One topic failing must not lose the whole digest.
        print(f"WARN query failed ({e}); skipping topic", file=sys.stderr)
        return []

    return data.get("resultList", {}).get("result", [])


def uid(paper: dict) -> str:
    """Stable identity for dedup. DOI when present, else the Europe PMC id."""
    doi = (paper.get("doi") or "").strip().lower()
    return f"doi:{doi}" if doi else f"epmc:{paper.get('id', '')}"


def format_paper(p: dict) -> str:
    title = (p.get("title") or "Untitled").strip().rstrip(".")
    authors = (p.get("authorString") or "Unknown authors").strip()
    if authors.count(",") > 6:                       # long author lists add no signal
        authors = ", ".join(authors.split(", ")[:6]) + ", et al."

    venue = (p.get("journalTitle") or "").strip()
    is_preprint = (p.get("source") or "") == "PPR"
    if is_preprint and not venue:
        venue = "Preprint"
    date = (p.get("firstPublicationDate") or p.get("pubYear") or "").strip()

    doi = (p.get("doi") or "").strip()
    link = f"https://doi.org/{doi}" if doi else (
        f"https://europepmc.org/article/{p.get('source','MED')}/{p.get('id','')}"
    )

    meta = " - ".join(x for x in (venue, date) if x)

    lines = [f"### {title}", ""]
    if is_preprint:
        lines.append("**PREPRINT - not peer reviewed**")
        lines.append("")
    lines += [f"*{authors}*", "", f"{meta}  \n<{link}>", ""]

    abstract = (p.get("abstractText") or "").strip()
    if abstract:
        # Europe PMC sometimes embeds light HTML in abstracts.
        for tag in ("<p>", "</p>", "<i>", "</i>", "<b>", "</b>", "<sub>", "</sub>",
                    "<sup>", "</sup>", "<h4>", "</h4>"):
            abstract = abstract.replace(tag, " ")
        lines += [" ".join(abstract.split()), ""]
    else:
        lines += ["_No abstract available._", ""]

    return "\n".join(lines)


def unique_path(directory: pathlib.Path, stem: str, ext: str) -> pathlib.Path:
    """Never overwrite. A same-day re-run reports only what the first run had no room
    for, so clobbering the earlier file would destroy papers already marked seen --
    they would never resurface."""
    p = directory / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = directory / f"{stem} ({n}){ext}"
        n += 1
    return p


def build_sources(sources: list[tuple[str, dict]], today: dt.date) -> str:
    """A flat, citable index of exactly what went into this date's digest."""
    lines = [
        f"# Sources - {today.isoformat()}",
        "",
        f"Every paper used in the digest of {today.isoformat()}, with a resolvable link.",
        f"{len(sources)} item(s).",
        "",
    ]
    current = None
    for topic_name, p in sources:
        if topic_name != current:
            lines += ["", f"## {topic_name}", ""]
            current = topic_name

        title = (p.get("title") or "Untitled").strip().rstrip(".")
        doi = (p.get("doi") or "").strip()
        epmc_id = p.get("id", "")
        src = p.get("source", "MED")
        venue = (p.get("journalTitle") or "").strip()
        date = (p.get("firstPublicationDate") or p.get("pubYear") or "").strip()
        kind = "preprint" if src == "PPR" else "peer-reviewed"

        link = f"https://doi.org/{doi}" if doi else \
            f"https://europepmc.org/article/{src}/{epmc_id}"

        bits = [b for b in (venue, date, kind) if b]
        lines.append(f"- [{title}]({link})  \n  {' | '.join(bits)}")
        # Keep the raw identifiers -- these are what you paste into a reference manager.
        if doi:
            lines.append(f"  DOI: `{doi}`")
        if p.get("pmid"):
            lines.append(f"  PMID: `{p['pmid']}`")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing")
    ap.add_argument("--days", type=int, help="override lookback_days")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    cfg = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8"))
    settings = cfg.get("settings", {}) or {}
    days = args.days or int(settings.get("lookback_days", 7))
    cap = int(settings.get("max_per_topic", 8))
    preprints = bool(settings.get("preprints", True))
    since = dt.date.today() - dt.timedelta(days=days)

    seen = load_seen()
    fresh: set[str] = set()

    today = dt.date.today()
    body = [
        f"# Paper digest - {today.isoformat()}",
        "",
        f"Papers first published between **{since.isoformat()}** and "
        f"**{today.isoformat()}**, excluding anything covered in an earlier digest.",
        "",
        "---",
        "",
    ]

    total = 0
    per_topic_counts = []
    sources: list[tuple[str, dict]] = []   # (topic name, paper) for the sources file

    for topic in cfg.get("topics", []):
        if not topic.get("enabled", True):
            continue
        name = topic.get("name", "Unnamed topic")
        results = search(topic["query"], since, cap * 3, preprints)
        time.sleep(0.5)                              # rate-limit courtesy

        new = []
        for p in results:
            u = uid(p)
            if u in seen or u in fresh:
                continue
            fresh.add(u)
            new.append(p)
            if len(new) >= cap:
                break

        per_topic_counts.append((name, len(new)))
        total += len(new)
        sources.extend((name, p) for p in new)

        body += [f"## {name}", ""]
        if not new:
            body += ["_Nothing new this week._", ""]
        else:
            body += [format_paper(p) for p in new]
        body += ["---", ""]

    summary = ", ".join(f"{n}: {c}" for n, c in per_topic_counts)
    body.insert(4, f"**{total} new paper(s)** - {summary}")
    body.insert(5, "")

    text = "\n".join(body)

    if args.dry_run:
        print(text)
        print(f"\n--- DRY RUN: {total} new, seen.json not updated ---", file=sys.stderr)
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    outfile = unique_path(args.out, f"digest-{today.isoformat()}", ".md")
    outfile.write_text(text, encoding="utf-8")

    srcfile = unique_path(args.out, f"sources-{today.isoformat()}", ".md")
    srcfile.write_text(build_sources(sources, today), encoding="utf-8")

    save_seen(seen | fresh)

    # stdout is consumed by run-reporter.ps1.
    print(f"{total}|{outfile}|{srcfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
