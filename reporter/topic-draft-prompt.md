A user of a weekly research-paper digest bot just described a topic they want to
follow, in plain language (on stdin). Turn it into a Europe PMC search query.

Output ONLY JSON matching the required schema -- no markdown fences, no commentary.

Europe PMC query syntax:
- `TITLE:"phrase"` / `ABSTRACT:"phrase"` -- quotes force an exact phrase
- `AND` `OR` `NOT` `( )` for grouping -- ALWAYS parenthesize an OR group before
  combining it with AND; an unparenthesized bare term next to AND/OR can float free
  and match unrelated fields (this has actually happened: an unparenthesized
  "closed-loop" once matched battery-recycling papers).
- `*` wildcard, e.g. `anaesthe*` matches anaesthesia/anaesthetic
- Prefer `TITLE:` over `ABSTRACT:` for precision; use `ABSTRACT:` only for a concept
  unlikely to be spelled out in a title.

Rules:
- "name": a short, human-readable label for the topic, under 6 words, in the casing a
  person would naturally write it (e.g. "Vagus nerve stimulation", not "VAGUS_NERVE").
- "query": a working Europe PMC query capturing the user's description -- specific
  enough to avoid noise, broad enough to catch real variation in how papers phrase the
  same idea (synonyms, abbreviations). Every OR group must be parenthesized per the
  rule above.
