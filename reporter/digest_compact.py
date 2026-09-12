"""
Turns fetch_papers' digest markdown into compact input for the LLM calls.

The full digest (every abstract verbatim) is still what gets sent to the user for
NotebookLM -- it's the right artifact for a deep read. It is the wrong input for Groq's
free tier: a normal week is ~13K tokens against an 8K tokens-per-minute cap, so a
request built from it is rejected outright, and the brief and infographic silently
never appeared. The model doesn't need every word of every abstract to write a brief or
a one-line finding; it needs titles and the opening of each abstract.
"""
from __future__ import annotations

import re

PAPERS_PER_TOPIC = 5


def parse_digest(text: str) -> list[dict]:
    """Returns [{name, count, papers: [{title, preprint, meta, abstract}]}], skipping
    topics with no new papers. Mirrors fetch_papers.format_paper's layout:
        ### title / [**PREPRINT...**] / *authors* / "venue - date  <link>" / abstract"""
    topics = []
    for section in re.split(r"^## ", text, flags=re.M)[1:]:
        name, _, rest = section.partition("\n")
        papers = []
        for block in re.split(r"^### ", rest, flags=re.M)[1:]:
            title, _, body = block.partition("\n")
            paras = [p.strip() for p in re.split(r"\n\s*\n", body)
                     if p.strip() and p.strip() != "---"]
            preprint = any(p.startswith("**PREPRINT") for p in paras)
            meta = next((p.split("\n")[0].strip() for p in paras if "<http" in p), "")
            abstract = paras[-1] if paras else ""
            if abstract.startswith("_No abstract") or "<http" in abstract or abstract.startswith("*"):
                abstract = ""
            papers.append({"title": title.strip(), "preprint": preprint,
                           "meta": meta, "abstract": abstract})
        if papers:
            topics.append({"name": name.strip(), "count": len(papers), "papers": papers})
    return topics


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # End on a sentence boundary when there is one reasonably close, else a word.
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > limit * 0.6 else cut.rsplit(" ", 1)[0]) + " [...]"


def compact(topics: list[dict], abstract_chars: int, per_topic: int = PAPERS_PER_TOPIC) -> str:
    lines = [f"(Abstracts are shortened to their first ~{abstract_chars} characters, and at "
             f"most {per_topic} papers per topic are shown, to fit the model's rate limit.)", ""]
    for t in topics:
        lines.append(f"## {t['name']} -- {t['count']} new paper(s)")
        for p in t["papers"][:per_topic]:
            tag = " [PREPRINT - not peer reviewed]" if p["preprint"] else ""
            meta = f" ({p['meta']})" if p["meta"] else ""
            lines.append(f"- {p['title']}{tag}{meta}")
            if p["abstract"]:
                lines.append(f"  {_clip(p['abstract'], abstract_chars)}")
        hidden = t["count"] - min(t["count"], per_topic)
        if hidden:
            lines.append(f"  (+{hidden} more in this topic not shown)")
        lines.append("")
    return "\n".join(lines)
