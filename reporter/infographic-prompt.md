Below (on stdin) is this week's raw literature harvest -- full abstracts, grouped by
topic. This is the source material itself, not anyone's commentary on it.

Your job: turn each topic's papers into a compact, plain-language visual summary of
WHAT THE PAPERS ACTUALLY FOUND -- the way a well-made study guide or briefing doc
would, not the way a critical peer reviewer would. A reader with no background in the
topic should be able to look at this and understand what happened this week, without
needing outside context. Save judgment (small sample, preprint caveats, etc.) for the
full written brief elsewhere -- this output's only job is comprehension, not critique.

Output ONLY JSON. No markdown fences, no preamble, no commentary, no trailing text --
your entire response must be a single valid JSON object, parsed programmatically by a
script that reads these EXACT field names and no others. Do not rename, nest, add, or
omit any field. Do not invent your own richer schema.

Required shape, with a worked example so the field names are unambiguous:

{
  "topics": [
    {
      "name": "Neurofeedback",
      "count": 8,
      "gist": "New engineering (7T fMRI, fNIRS decoders) lands alongside two reviews of clinical results",
      "papers": [
        {
          "title": "7T real-time fMRI neurofeedback",
          "finding": "Isolated a brain reward-circuit signal from surrounding fluid and tissue with high precision"
        },
        {
          "title": "Cochrane review of BCI stroke rehab",
          "finding": "BCI training performed the same as a fake (sham) version in most trials"
        }
      ]
    }
  ]
}

Rules:
- One object per topic that had new papers this week. Use the topic names as they
  appear in the input's own section headings.
- "count" is the number of new papers under that topic (count them from the input).
- "gist": one sentence, under 18 words, plain language, orienting the reader to what
  this topic's papers are about this week as a set -- not a critique, just a compass.
- "papers": one entry per paper in that topic, up to 5 (if there are more than 5, pick
  the 5 most substantive -- skip pure reviews-of-reviews or purely administrative
  entries in favor of primary findings).
  - "title": under 8 words, plain language, NOT the paper's actual academic title --
    describe what it's about (e.g. "7T real-time fMRI neurofeedback", not
    "Feasibility of high-field real-time functional MRI...").
  - "finding": one sentence, under 20 words, plain language, stating what the paper
    actually did or found -- translate jargon into terms a smart non-specialist would
    understand. This is a summary of the finding, not an opinion about its quality.
- No jargon left unexplained: if the abstract's key term is technical (e.g. "sham-BCI",
  "DMN connectivity"), either explain it inline in a few words or replace it with a
  plain-language equivalent.
- If a topic had zero new papers, omit it entirely -- do not include an empty entry.
