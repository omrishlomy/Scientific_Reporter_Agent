"""
Builds the comprehensive "research report for NotebookLM" document.

The paper digest (titles, abstracts, links) is a good reading list but too thin a source
for NotebookLM's Audio Overview: the hosts can only talk about what's in the source, and
an abstract is ~250 words. This document gives them real material per paper -- the
written overview, per-topic background and how the papers connect, a plain-language
explainer for each paper, the full abstract, the paper's own full text when it is open
access (Introduction / Results / Discussion / Conclusions, methods shortened), and a
glossary of the technical terms.

Full text comes from Europe PMC's fullTextXML, which only exists for open-access papers
with a PMC id; everything else gets the abstract and the explainer, and says so.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UA = "ArziLab-paper-digest/1.0 (mailto:omrishlomy44@gmail.com)"

MAX_WORDS_PER_PAPER = 7000
METHODS_WORDS = 350

# Front/back matter that adds length without adding anything worth hearing.
_SKIP = re.compile(r"reference|acknowledg|fund|author|contribution|conflict|competing|"
                   r"declaration|ethic|consent|data avail|availability|associated data|"
                   r"abbreviation|supplement|appendix|financial|disclosure|review board|"
                   r"orcid|footnote|publisher", re.I)
_METHODS = re.compile(r"method|material|procedure|participant|statistic|protocol|design", re.I)
# Inline citations and non-prose elements, removed before extracting text.
_DROP_TAGS = {"xref", "table-wrap", "fig", "disp-formula",
              "supplementary-material", "table", "graphic", "media"}
# Replaced by their plain text instead of dropped. Inline formulas are short and carry
# meaning: papers mark up the "p" in "p < 0.001" as MathML, and dropping the element
# turned every reported statistic into "F = 1690.30, < 0.001".
_FLATTEN_TAGS = {"inline-formula"}


def _strip(elem: ET.Element) -> None:
    """Removes _DROP_TAGS and flattens _FLATTEN_TAGS in place, keeping the text that
    followed each one."""
    for child in list(elem):
        if child.tag in _FLATTEN_TAGS:
            inner = "".join(child.itertext())
        else:
            _strip(child)
            if child.tag not in _DROP_TAGS:
                continue
            inner = ""
        joined = inner + (child.tail or "")
        idx = list(elem).index(child)
        if idx > 0:
            prev = elem[idx - 1]
            prev.tail = (prev.tail or "") + joined
        else:
            elem.text = (elem.text or "") + joined
        elem.remove(child)


def _clean(text: str) -> str:
    text = " ".join(text.split())
    # Citation brackets left empty once their xref numbers are gone: "[ , ]", "( ; )".
    text = re.sub(r"\[\s*[,;–\-\s]*\]|\(\s*[,;–\-\s]*\)", "", text)
    return re.sub(r"\s+([,.;:])", r"\1", text).strip()


def _words(text: str) -> int:
    return len(text.split())


def fetch_full_text(paper: dict) -> list[tuple[str, str]]:
    """[(section title, text)] for an open-access paper with a PMC id, else []."""
    pmcid = paper.get("pmcid")
    if not (pmcid and paper.get("open_access")):
        return []
    try:
        req = urllib.request.Request(f"{API}/{pmcid}/fullTextXML", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as resp:
            root = ET.fromstring(resp.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        print(f"WARN full text unavailable for {pmcid}: {e}", file=sys.stderr)
        return []
    finally:
        time.sleep(0.3)   # Europe PMC courtesy

    body = root.find(".//body")
    if body is None:
        return []
    _strip(body)

    sections: list[tuple[str, str]] = []
    budget = MAX_WORDS_PER_PAPER
    blocks = body.findall("sec") or [body]
    for sec in blocks:
        title_el = sec.find("title")
        title = _clean("".join(title_el.itertext())) if title_el is not None else ""
        if title and _SKIP.search(title):
            continue
        paras = [_clean("".join(p.itertext())) for p in sec.iter("p")]
        paras = [p for p in paras if _words(p) >= 5]
        if not paras:
            continue
        limit = METHODS_WORDS if (title and _METHODS.search(title)) else budget
        kept, used = [], 0
        for p in paras:
            if used + _words(p) > min(limit, budget):
                break
            kept.append(p)
            used += _words(p)
        if kept:
            shortened = len(kept) < len(paras)
            text = "\n\n".join(kept) + ("\n\n[Section shortened.]" if shortened else "")
            sections.append((title or "Main text", text))
            budget -= used
        if budget <= 0:
            break
    return sections


def _plain(value: str) -> str:
    return (value or "").strip()


def build_document(date: str, label: str, topics: list[dict], brief: str,
                   notes: dict[str, dict], full_texts: dict[str, list[tuple[str, str]]],
                   lookback_days: int) -> str:
    """topics: papers.json topics. notes: topic name -> topic-notes JSON (may be missing).
    full_texts: paper link -> sections."""
    total = sum(len(t["papers"]) for t in topics)
    earlier = sum(1 for t in topics for p in t["papers"] if p.get("backfilled"))
    with_ft = sum(1 for t in topics for p in t["papers"] if full_texts.get(p["link"]))

    out = [
        f"# Research report: {label}",
        "",
        f"Date: {date}",
        "",
        f"This report covers {total} research paper(s) across {len(topics)} topic(s): "
        + ", ".join(t["name"] for t in topics) + ". "
        + (f"{total - earlier} were published in the last {lookback_days} days; the other "
           f"{earlier} are earlier papers included because those topics had few new ones. "
           if earlier else f"All were published in the last {lookback_days} days. ")
        + f"Full text was available for {with_ft} of them; the rest are covered from their "
          "abstracts.",
        "",
        "It is organised as an overview, then one section per topic: background, how the "
        "papers connect, each paper explained in plain language with its abstract and (when "
        "open access) its full text, and a glossary of key terms.",
        "",
    ]
    if brief.strip():
        out += ["## Overview", "", brief.strip(), ""]

    for ti, t in enumerate(topics, 1):
        n = notes.get(t["name"]) or {}
        out += [f"## Topic {ti}: {t['name']}", ""]
        if t.get("note"):
            out += [f"Note: {t['note']}", ""]
        if _plain(n.get("background")):
            out += ["### Background", "", _plain(n["background"]), ""]
        if _plain(n.get("connections")):
            out += ["### How these papers connect", "", _plain(n["connections"]), ""]

        explainers = {e.get("index"): e for e in (n.get("papers") or []) if isinstance(e, dict)}
        for pi, p in enumerate(t["papers"], 1):
            out += [f"### Paper {ti}.{pi}: {p['title']}", ""]
            facts = []
            if p.get("authors"):
                facts.append(f"Authors: {p['authors']}")
            venue = " - ".join(x for x in (p.get("journal"), p.get("date")) if x)
            if venue:
                facts.append(f"Published: {venue}")
            facts.append("Status: preprint, not yet peer reviewed" if p.get("preprint")
                         else "Status: peer-reviewed publication")
            if p.get("backfilled"):
                facts.append("Timing: an earlier paper, not new this week")
            facts.append(f"Link: {p['link']}")
            out += [f"- {f}" for f in facts] + [""]

            e = explainers.get(pi) or {}
            parts = [("Research question", e.get("question")), ("Approach", e.get("approach")),
                     ("Key results", e.get("key_results")), ("Why it matters", e.get("why_it_matters")),
                     ("Limitations", e.get("limitations"))]
            parts = [(k, _plain(v)) for k, v in parts if _plain(v)]
            if parts:
                out += ["#### In plain language", ""]
                out += [f"**{k}.** {v}" for k, v in parts] + [""]

            out += ["#### Abstract", "", p.get("abstract") or "No abstract available.", ""]

            sections = full_texts.get(p["link"]) or []
            if sections:
                out += ["#### Full text (open access)", ""]
                for title, text in sections:
                    out += [f"##### {title}", "", text, ""]
            else:
                out += ["_Full text is not openly available for this paper, so it is covered "
                        "from its abstract._", ""]

        glossary = [g for g in (n.get("glossary") or []) if isinstance(g, dict)
                    and _plain(g.get("term")) and _plain(g.get("definition"))]
        if glossary:
            out += [f"### Key terms: {t['name']}", ""]
            out += [f"- **{_plain(g['term'])}**: {_plain(g['definition'])}" for g in glossary] + [""]

    out += ["## Sources", ""]
    for t in topics:
        for p in t["papers"]:
            out.append(f"- {p['title']} ({p.get('journal') or 'preprint'}, {p.get('date', '')}): {p['link']}")
    return "\n".join(out) + "\n"


def numbered_topic_input(topic: dict, abstract_chars: int) -> str:
    """Compact, numbered view of one topic for the topic-notes LLM call. Numbered so the
    model's per-paper notes can be matched back by index, not by fuzzy title."""
    lines = [f"## {topic['name']} -- {len(topic['papers'])} paper(s)"]
    if topic.get("note"):
        lines.append(f"Note: {topic['note']}")
    for i, p in enumerate(topic["papers"], 1):
        tags = []
        if p.get("preprint"):
            tags.append("PREPRINT - not peer reviewed")
        if p.get("backfilled"):
            tags.append("earlier paper, not new this week")
        meta = " - ".join(x for x in (p.get("journal"), p.get("date")) if x)
        lines.append(f"\n[{i}] {p['title']}" + (f" ({meta})" if meta else "")
                     + (f" [{'; '.join(tags)}]" if tags else ""))
        abstract = p.get("abstract") or "(no abstract available)"
        if len(abstract) > abstract_chars:
            cut = abstract[:abstract_chars]
            dot = cut.rfind(". ")
            abstract = (cut[:dot + 1] if dot > abstract_chars * 0.6 else cut.rsplit(" ", 1)[0]) + " [...]"
        lines.append(abstract)
    return "\n".join(lines)
