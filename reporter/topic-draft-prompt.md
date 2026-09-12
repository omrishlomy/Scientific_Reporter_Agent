A user of a weekly research-paper digest bot sent a message (on stdin). Decide whether
it describes a research topic they want to follow, and if so, turn it into a Europe PMC
search query.

Output ONLY JSON matching the required schema -- no markdown fences, no commentary.

## First: is this actually a topic?

Set `is_topic` to false for anything that is NOT a research subject to follow --
greetings ("hi", "hello", "test"), thanks, questions about the bot, small talk, or
anything too vague to search on ("papers", "science", "interesting stuff"). In that
case set `name` and `query` to empty strings and put a one-sentence explanation in
`reason`.

Only set `is_topic` to true when there is a real, specific subject to search for.
**Never invent a catch-all query to satisfy the format** -- a query like `TITLE:*` or
any query that would match nearly everything is always wrong. If you cannot write a
specific query, that means `is_topic` is false.

## If it is a topic

Europe PMC query syntax:
- `TITLE:"phrase"` / `ABSTRACT:"phrase"` -- quotes force an exact phrase
- `AND` `OR` `NOT` `( )` for grouping -- ALWAYS parenthesize an OR group before
  combining it with AND; an unparenthesized bare term next to AND/OR can float free
  and match unrelated fields (this has actually happened: an unparenthesized
  "closed-loop" once matched battery-recycling papers).
- `*` wildcard on a word stem, e.g. `anaesthe*` matches anaesthesia/anaesthetic.
  A bare `*` on its own is never acceptable.
- Prefer `TITLE:` over `ABSTRACT:` for precision; use `ABSTRACT:` only for a concept
  unlikely to be spelled out in a title.

Rules:
- `name`: a short, human-readable label, under 6 words, in the casing a person would
  naturally write (e.g. "Vagus nerve stimulation", not "VAGUS_NERVE").
- `query`: a working Europe PMC query capturing the user's description -- specific
  enough to avoid noise, broad enough to catch real variation in how papers phrase the
  same idea (synonyms, abbreviations). Every OR group must be parenthesized.
- `reason`: empty string when `is_topic` is true.
