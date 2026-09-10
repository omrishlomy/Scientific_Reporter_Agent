# Scientific Reporter Agent

Cloud-hosted version of the weekly paper digest -- runs on GitHub Actions instead of a
local PC, so it can't miss a Monday because a machine was off. Synthesis runs on
`openai/gpt-oss-120b`, a large open-weight model hosted free by Groq, rather than a
personal Claude login (Actions runners need to authenticate as themselves, not as you)
and rather than self-hosted CPU inference (measured infeasible -- see below).

Repo: https://github.com/omrishlomy/Scientific_Reporter_Agent

## Why Groq, not a self-hosted model on the runner itself

An earlier version of this ran a self-hosted 7B model (llama.cpp) directly on the
Actions runner. It was actually measured, not assumed, to be a dead end:

- **2.2-2.5 tokens/sec** on the runner's 2 CPU cores -- a single synthesis call took
  **41 minutes**.
- **~8.3GB RAM** even after quantizing the KV cache, against the runner's ~7GB budget.
- The extraction call's JSON **never finished** within a sane token budget -- it got
  cut off mid-topic.
- A bigger (~30GB) model would need *more* compute per token, making CPU-only
  inference slower still, not faster -- more RAM alone doesn't fix a compute-bound
  bottleneck; only more/faster compute (a GPU) does, and free GPU hosting for a
  recurring scheduled job doesn't really exist.

Groq runs the model on its own fast hardware instead: same idea (a large, genuinely
open-weight model, not Claude), but the speed and memory problems both disappear
because the runner just makes a network call. `openai/gpt-oss-120b` is bigger than the
original ~30GB target, has a 131K context window, and Groq's free tier supports it with
**strict JSON-schema-constrained decoding** for the infographic-extraction step -- the
model's output is now structurally *guaranteed* to match the expected field names,
which real Claude and Qwen runs both drifted from under prompt-only instructions.
`reporter/local_llm.py`'s docstring has the full story if this comes up again.

## Why this is its own repo, separate from the research repo

Two reasons, both load-bearing:

1. **Cost.** The interactive Telegram bot (below) polls every 5 minutes --
   ~8,640 runs/month. GitHub's free Actions minutes (2,000/month) only cover that
   volume on a **public** repo (unlimited free minutes there); on a private repo it
   would blow the free tier and start actually billing. This repo is public and
   contains no research data, so that's a non-issue.
2. **Privacy.** The download-filer agent (a separate, PC-only tool, not part of this
   repo) logs real filenames from Downloads -- those must never be anywhere public.
   Keeping this repo scoped to just the reporter avoids that risk entirely by
   construction, not by discipline.

Nothing sensitive lives here: topic keywords, paper DOIs already reported (`seen.json`),
and the pipeline code itself.

## Two workflows

- **`reporter.yml`** -- Mondays 08:00 Israel time. Fetches new papers (Europe PMC),
  writes a synthesis brief and infographic via Groq, sends both to Telegram, commits
  the updated `seen.json` dedup list back to the repo. Real expected runtime: well
  under a minute of actual work (a few Groq API calls plus rendering an image) --
  timeout is set to 15 minutes purely for headroom.
- **`telegram-bot.yml`** -- every 5 minutes. Checks for `/addtopic`, `/removetopic`,
  `/topics` commands and commits changes to `topics.yaml`. Deliberately simple/rule-based,
  not LLM-backed -- see the comment at the top of `reporter/bot_poll.py` for why.

## Setup status

- [x] Repo created (public, github.com/omrishlomy) and pushed.
- [ ] **Get a free Groq API key** at https://console.groq.com/keys (an account Claude
  cannot create on your behalf).
- [ ] **Add three repo secrets** -- repo -> Settings -> Secrets and variables -> Actions
  -> New repository secret. This step needs you: entering tokens/credentials isn't
  something Claude does on your behalf, even into a form field.
  - `GROQ_API_KEY` -- from the step above
  - `TELEGRAM_BOT_TOKEN` -- the same token from `@BotFather` used for the local PC setup
  - `TELEGRAM_CHAT_ID` -- your chat id (the one `setup-telegram.ps1` auto-detected on
    the PC; check `telegram-config.json` there if you need to look it up again)
- [ ] Confirm Actions is enabled on the repo (Actions tab -- first-time repos sometimes
  need this confirmed explicitly).
- [ ] Trigger the reporter once manually to confirm it works end to end: Actions tab ->
  "Weekly paper digest" -> Run workflow. Should finish in well under a minute of actual
  compute (plus a few seconds of GitHub's own job startup overhead) -- if it's still
  running after several minutes, something is actually wrong (check the job log for a
  Groq API error, most likely a missing/invalid `GROQ_API_KEY`).

Once confirmed working, you can retire the Windows Scheduled Task
(`ClaudeAgent-Reporter`) on the PC -- this repo replaces it. The download-filer agent
is unaffected and keeps running locally exactly as before; it was never part of this
migration.

## Known constraints (read before assuming something's broken)

- **Groq free tier has rate limits.** Fine at this volume (a handful of calls, once a
  week, plus occasional topic-management commands) -- see
  https://console.groq.com/docs/rate-limits if this ever needs to scale up.
- **Topic commands land within ~5 minutes, not instantly** -- a consequence of Actions
  runners being ephemeral rather than a persistent bot process.
- **Quality vs. Claude:** not independently re-measured against gpt-oss-120b yet (the
  quality comparison on record was against the retired local Qwen2.5-7B run, which was
  worse). Worth a real side-by-side after a few real Monday runs land.
