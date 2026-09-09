# Paper digest bot

Cloud-hosted version of the weekly paper digest -- runs on GitHub Actions instead of a
local PC, so it can't miss a Monday because a machine was off. Synthesis runs on a
self-hosted open-weights model (Qwen2.5-7B-Instruct, quantized) rather than a personal
Claude login, since Actions runners need to authenticate as themselves, not as you.

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
  writes a synthesis brief and infographic via the local model, sends both to Telegram,
  commits the updated `seen.json` dedup list back to the repo.
- **`telegram-bot.yml`** -- every 5 minutes. Checks for `/addtopic`, `/removetopic`,
  `/topics` commands and commits changes to `topics.yaml`. Deliberately simple/rule-based,
  not LLM-backed -- see the comment at the top of `reporter/bot_poll.py` for why.

## One-time setup (you do this part -- account/repo creation isn't something Claude does on your behalf)

1. **Create a new GitHub repo**, public, e.g. named `paper-digest-bot`. (github.com ->
   New repository -> do not initialize with a README, since this folder already has one.)
2. From this folder:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/paper-digest-bot.git
   git push -u origin main
   ```
3. **Add two repo secrets** (repo -> Settings -> Secrets and variables -> Actions ->
   New repository secret):
   - `TELEGRAM_BOT_TOKEN` -- the same token from `@BotFather` used for the local setup
   - `TELEGRAM_CHAT_ID` -- your chat id (same one `setup-telegram.ps1` auto-detected;
     see `telegram-config.json` on the PC if you need to look it up again)
4. Enable Actions on the repo if prompted (first-time repos sometimes need this
   confirmed explicitly under the Actions tab).
5. Trigger the reporter once manually to confirm it works end to end: Actions tab ->
   "Weekly paper digest" -> Run workflow. Expect it to take a while (see below) --
   CPU-only inference on a free runner is not fast.

Once confirmed working, you can retire the Windows Scheduled Task
(`ClaudeAgent-Reporter`) on the PC -- this repo replaces it. The download-filer agent
is unaffected and keeps running locally exactly as before; it was never part of this
migration.

## Known constraints (read before assuming something's broken)

- **Slow.** CPU-only inference on a free runner's 2 cores, at the context sizes a
  weekly digest needs, takes real time -- see the timing measured during setup in the
  handoff notes for actual numbers. This is expected, not a hang; the workflow's
  90-minute timeout gives it room.
- **Lower synthesis quality than Claude.** This was an explicit, informed tradeoff --
  see the project's memory notes for the fuller reasoning if you revisit this later.
- **Topic commands land within ~5 minutes, not instantly** -- a consequence of Actions
  runners being ephemeral rather than a persistent bot process.
