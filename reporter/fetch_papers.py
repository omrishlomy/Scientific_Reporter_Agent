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
DEFAULT_TOPICS_FILE = HERE / "topics.yaml"
DEFAULT_SEEN_FILE = HERE / "seen.json"
DEFAULT_OUT = pathlib.Path(r"G:\My Drive\Research\Digests")

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Europe PMC asks for identification and is generous in return. Be polite.
UA = "ArziLab-paper-digest/1.0 (mailto:omrishlomy44@gmail.com)"
TIMEOUT = 30


def load_seen(seen_file: pathlib.Path) -> set[str]:
    if seen_file.exists():
        try:
            return set(json.loads(seen_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            # A corrupt seen-list must not stop the digest; worst case we repeat a week.
            print(f"WARN {seen_file} unreadable, starting fresh", file=sys.stderr)
    return set()


def save_seen(seen: set[str], seen_file: pathlib.Path) -> None:
    # Keep it bounded: 5000 ids is years of history at this volume.
    trimmed = sorted(seen)[-5000:]
    seen_file.write_text(json.dumps(trimmed, indent=0), encoding="utf-8")


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


# When a topic has fewer than min_per_topic unseen papers in the normal lookback window,
# the search walks further back in these steps. "Nothing new this week" was a dead end
# for the reader; an earlier paper they haven't been sent is still worth reading, as long
# as the digest is honest that it isn't new.
BACKFILL_WINDOWS_DAYS = (30, 90, 365, 1825)
MAX_PAGES = 5   # per query slice; up to 500 records scanned for unseen papers


def search_page(query: str, since: dt.date, until: dt.date, page_size: int,
                preprints: bool, cursor: str) -> tuple[list[dict], str | None]:
    """One page of a Europe PMC query, newest first, within [since, until]."""
    date_clause = f"(FIRST_PDATE:[{since.isoformat()} TO {until.isoformat()}])"
    full = f"({query.strip()}) AND {date_clause}"
    if not preprints:
        full += ' AND (SRC:"MED")'
    params = {
        "query": full,
        "format": "json",
        "pageSize": str(page_size),
        "resultType": "core",          # 'core' is what includes abstractText
        "sort": "P_PDATE_D desc",
        "cursorMark": cursor,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("resultList", {}).get("result", []), data.get("nextCursorMark")


def collect_unseen(query: str, since: dt.date, until: dt.date, want: int,
                   preprints: bool, taken: set[str]) -> list[dict]:
    """Pages newest-first until `want` papers not in `taken` are found, adding each one
    to `taken`. Paging matters: once seen.json is large, the whole first page can be
    papers already sent, and stopping there wrongly concludes there is nothing to show."""
    found: list[dict] = []
    if want <= 0 or since > until:
        return found
    cursor = "*"
    page_size = min(100, max(25, want * 3))
    for _ in range(MAX_PAGES):
        try:
            results, next_cursor = search_page(query, since, until, page_size, preprints, cursor)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            # One failing query must not lose the whole digest.
            print(f"WARN query failed ({e}); keeping what was found so far", file=sys.stderr)
            break
        for p in results:
            u = uid(p)
            if u in taken:
                continue
            taken.add(u)
            found.append(p)
            if len(found) >= want:
                return found
        if not results or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.3)                              # rate-limit courtesy
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing")
    ap.add_argument("--days", type=int, help="override lookback_days")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--topics-file", type=pathlib.Path, default=DEFAULT_TOPICS_FILE,
                     help="per-chat topics.yaml in multi-tenant mode; defaults to the shared one")
    ap.add_argument("--seen-file", type=pathlib.Path, default=DEFAULT_SEEN_FILE,
                     help="per-chat seen.json in multi-tenant mode; defaults to the shared one")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.topics_file.read_text(encoding="utf-8")) or {}
    settings = cfg.get("settings", {}) or {}
    days = args.days or int(settings.get("lookback_days", 7))
    cap = int(settings.get("max_per_topic", 8))
    min_per_topic = max(1, min(cap, int(settings.get("min_per_topic", 3))))
    preprints = bool(settings.get("preprints", True))

    today = dt.date.today()
    since = today - dt.timedelta(days=days)
    windows = [w for w in BACKFILL_WINDOWS_DAYS if w > days]

    seen = load_seen(args.seen_file)
    taken = set(seen)          # grows as papers are picked, so topics never share one

    body = [
        f"# Paper digest - {today.isoformat()}",
        "",
        f"Papers first published between **{since.isoformat()}** and **{today.isoformat()}** "
        f"that no earlier digest covered. Topics with fewer than {min_per_topic} of those are "
        "topped up with earlier papers you haven't been sent yet, and say so below.",
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

        papers = collect_unseen(topic["query"], since, today, cap, preprints, taken)
        new_count = len(papers)
        time.sleep(0.5)                              # rate-limit courtesy

        # Walk back through non-overlapping older slices, so already-scanned recent
        # results aren't paged through again on every widening step.
        widest = days
        upper = since - dt.timedelta(days=1)
        for w in windows:
            if len(papers) >= min_per_topic:
                break
            lower = today - dt.timedelta(days=w)
            papers += collect_unseen(topic["query"], lower, upper, cap - len(papers),
                                     preprints, taken)
            widest = w
            upper = lower - dt.timedelta(days=1)
            time.sleep(0.5)

        backfilled = len(papers) - new_count
        per_topic_counts.append((name, new_count, backfilled))
        total += len(papers)
        sources.extend((name, p) for p in papers)

        body += [f"## {name}", ""]
        if not papers:
            body += [f"_No papers you haven't already been sent, even searching back {widest} "
                     "days. This topic may be too narrow._", ""]
        else:
            if backfilled:
                lead = (f"No new papers in the last {days} days" if new_count == 0
                        else f"Only {new_count} new paper(s) in the last {days} days")
                body += [f"_{lead} -- also including {backfilled} earlier paper(s) you haven't "
                         f"been sent, from up to {widest} days back._", ""]
            body += [format_paper(p) for p in papers]
        body += ["---", ""]

    fresh = {uid(p) for _, p in sources}
    summary = ", ".join(f"{n}: {c}" + (f" (+{b} earlier)" if b else "")
                        for n, c, b in per_topic_counts)
    body.insert(4, f"**{total} paper(s)** - {summary}")
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

    save_seen(seen | fresh, args.seen_file)

    # stdout is consumed by the caller (run_reporter_cloud.py / run-reporter.ps1).
    print(f"{total}|{outfile}|{srcfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
