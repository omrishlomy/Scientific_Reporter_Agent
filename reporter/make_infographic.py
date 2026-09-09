"""
Renders a per-topic JSON block (appended to brief-DATE.md by run-reporter.ps1, but
extracted from the RAW DIGEST abstracts, not the brief's own critical prose -- see
infographic-prompt.md) into a single infographic PNG: one card per topic, each listing
what the week's papers actually found in plain language. Meant to be readable at a
glance on a phone, the way a NotebookLM-style study guide is, not as a critique.

    python make_infographic.py --brief "G:/.../brief-2026-09-03.md" --out "G:/.../infographic-2026-09-03.png"

Exits 0 with no file written if the brief has no json block or it fails to parse --
a broken infographic step must never take down the rest of the pipeline.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# Muted, print-safe palette -- distinct hues, similar saturation/lightness so no single
# card visually shouts over the others.
PALETTE = [
    "#3B6EA5",  # blue
    "#5E8C61",  # green
    "#A0522D",  # rust
    "#7A5AA3",  # purple
    "#B8860B",  # amber
    "#4A7A7A",  # teal
]
INK = "#1E2430"
SUBINK = "#5A6472"
CARD_BG = "#F7F8FA"
CARD_BORDER = "#DADEE3"


def normalize_topic(t: dict) -> dict:
    """Defense against schema drift: the extraction model is instructed to use exact
    field names, but if it ever wanders, map what we can rather than silently
    rendering a blank card. Also enforces length caps regardless of prompt
    compliance, since a schema-compliant-but-verbose response can still blow the
    layout up (a "finding" that's actually a full paragraph, etc)."""
    name = t.get("name") or t.get("topic") or "Untitled"
    count = t.get("count", 0)

    gist = t.get("gist") or t.get("headline") or t.get("throughline") or t.get("summary") or ""
    gist = cap_words(gist, 22)

    raw_papers = t.get("papers")
    if not isinstance(raw_papers, list):
        # Oldest schema variant used flat "highlights" strings; turn each into a
        # title-less paper entry rather than dropping the content.
        raw_papers = [{"finding": h} for h in (t.get("highlights") or []) if h]

    papers = []
    for p in raw_papers[:5]:
        if isinstance(p, dict):
            title = p.get("title") or p.get("authors") or ""
            finding = p.get("finding") or p.get("description") or ""
        elif isinstance(p, str):
            title, finding = "", p
        else:
            continue
        finding = finding.strip()
        if not finding:
            continue
        papers.append({"title": cap_words(title, 10), "finding": cap_words(finding, 26)})

    return {"name": name, "count": count, "gist": gist, "papers": papers}


def cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(".,;:") + "..."


def extract_json(brief_text: str) -> dict | None:
    m = re.search(r"```json\s*(\{.*?\})\s*```", brief_text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"WARN could not parse infographic json: {e}", file=sys.stderr)
        return None


def wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text, width=width))


def place_text(ax, fig, renderer, x: float, y: float, text: str, pad: float = 0.25, **kw) -> float:
    """Draw a text object anchored at its top-left and return the y just below it,
    measured from the ACTUAL rendered bounding box (converted into this axes' data
    coordinates) rather than an estimated line count. Font metrics do not scale
    linearly with the string length or wrap width we picked, so guessing the height
    reliably undershoots and text stacks on top of itself."""
    artist = ax.text(x, y, text, va="top", ha="left", **kw)
    bbox = artist.get_window_extent(renderer=renderer)
    (_, y0), (_, y1) = ax.transData.inverted().transform([
        (bbox.x0, bbox.y0), (bbox.x1, bbox.y1)
    ])
    height = abs(y1 - y0)
    return y - height - pad


def draw_card(ax, fig, renderer, topic: dict, color: str) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")

    ax.add_patch(FancyBboxPatch(
        (0.15, 0.15), 9.7, 9.7,
        boxstyle="round,pad=0,rounding_size=0.35",
        linewidth=1.2, edgecolor=CARD_BORDER, facecolor=CARD_BG, zorder=0,
    ))
    ax.add_patch(FancyBboxPatch(
        (0.15, 9.15), 9.7, 0.7,
        boxstyle="round,pad=0,rounding_size=0.35",
        linewidth=0, facecolor=color, zorder=1,
    ))
    # square off the bottom corners of the header strip so it reads as one card
    ax.add_patch(plt.Rectangle((0.15, 8.85), 9.7, 0.35, facecolor=color, edgecolor="none", zorder=1))

    name = topic.get("name", "")
    count = topic.get("count", 0)
    ax.text(0.55, 9.5, wrap(name, 28), fontsize=13, fontweight="bold",
             color="white", va="center", ha="left", zorder=2)
    ax.text(9.45, 9.5, str(count), fontsize=16, fontweight="bold",
             color="white", va="center", ha="right", zorder=2)
    ax.text(9.45, 9.15, "new", fontsize=7,
             color="white", va="top", ha="right", alpha=0.85, zorder=2)

    # Sequential layout throughout: each block's height is measured from its actual
    # rendered bbox (via place_text) before the next is placed. A block is skipped
    # rather than force-overlapped if there is no room left; this matters because the
    # extraction model's output length is not perfectly bounded, and a headless weekly
    # run has no one watching to catch a garbled card.
    y = 8.2
    card_bottom = 0.35

    gist = topic.get("gist", "")
    if gist and y > card_bottom:
        y = place_text(ax, fig, renderer, 0.55, y, wrap(gist, 34),
                        fontsize=10, color=SUBINK, style="italic",
                        linespacing=1.3, pad=0.3)
        y -= 0.1
        ax.plot([0.55, 9.45], [y, y], color=CARD_BORDER, linewidth=1, zorder=1)
        y -= 0.35

    # Each paper gets a bold plain-language title, then its finding as a sentence --
    # this is the actual content the reader came for: what did the papers say, not
    # what should you doubt about them.
    papers = topic.get("papers", [])
    shown = 0
    for paper in papers:
        if y < card_bottom + 0.5:
            break
        title = paper.get("title", "")
        finding = paper.get("finding", "")
        if title:
            y = place_text(ax, fig, renderer, 0.55, y, wrap(title, 36),
                            fontsize=10, fontweight="bold", color=color,
                            linespacing=1.25, pad=0.12)
        if finding and y > card_bottom:
            y = place_text(ax, fig, renderer, 0.55, y, wrap(finding, 40),
                            fontsize=9.5, color=INK, linespacing=1.3, pad=0.3)
        shown += 1

    # A card can under-represent a topic two ways: the extraction step already caps
    # at 5 papers, and the card itself may run out of vertical room before that. Either
    # way, silently stopping reads as broken ("says 7, shows 2") rather than trimmed --
    # so always say how many more are in the full digest when the numbers don't match.
    total = topic.get("count", 0) or len(papers)
    remaining = total - shown
    if remaining > 0:
        ax.text(0.55, max(y - 0.1, card_bottom), f"+ {remaining} more in this week's digest",
                 fontsize=8.5, color=SUBINK, style="italic", va="top", ha="left")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--date", default="")
    args = ap.parse_args()

    if not args.brief.exists():
        print(f"WARN brief not found: {args.brief}", file=sys.stderr)
        return 0

    data = extract_json(args.brief.read_text(encoding="utf-8"))
    if not data or not data.get("topics"):
        print("WARN no infographic data in brief; skipping image", file=sys.stderr)
        return 0

    topics = [normalize_topic(t) for t in data["topics"] if isinstance(t, dict)]
    n = len(topics)
    cols = 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols

    fig, axes = plt.subplots(rows, cols, figsize=(5.2 * cols, 5.4 * rows), facecolor="white")
    if rows * cols == 1:
        axes = [axes]
    else:
        axes = axes.flatten()

    # A renderer is needed to measure text bounding boxes for the sequential layout;
    # an initial draw (even on empty axes) is enough for Agg to hand one back.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    for i, topic in enumerate(topics):
        draw_card(axes[i], fig, renderer, topic, PALETTE[i % len(PALETTE)])
    for j in range(n, len(axes)):
        axes[j].axis("off")

    fig.suptitle(f"Weekly paper digest{'  -  ' + args.date if args.date else ''}",
                 fontsize=16, fontweight="bold", color=INK, y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180, facecolor="white")
    print(str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
