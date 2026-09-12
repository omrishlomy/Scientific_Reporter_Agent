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

## Deployment: Railway (webhook) -- replaces the GitHub Actions polling setup

The bot runs as a small FastAPI service (`server.py`) on Railway. It receives Telegram
**webhooks**, so messages and button presses are handled the instant they're sent, and
runs the weekly digest in-process via APScheduler.

**This is a separate Railway service from any other project** -- its own service, its
own volume, its own variables. Nothing is shared.

### Why this replaced GitHub Actions

Actions fired the poller every **120-280 minutes** despite a `*/5` cron (measured over
21 runs; GitHub throttles scheduled workflows on free public repos). Every topic added
and every "generate report" press waited hours. A persistent service has no such limit.

### Railway setup

1. **New service** in your Railway project -> Deploy from GitHub repo ->
   `omrishlomy/Scientific_Reporter_Agent`. It builds from the `Dockerfile`
   (`railway.json` pins the Dockerfile builder, same convention as your other project).
2. **Add a Volume**, mount path **`/data`**. This is not optional: Railway's container
   filesystem is wiped on every redeploy, and `/data` is where topics and
   already-seen-paper history live. Without it, every deploy resets all chats.
   On first boot the service copies any chats bundled in the repo into the empty volume,
   so the existing setup carries over automatically.
3. **Variables:**

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | same BotFather token as before |
   | `GROQ_API_KEY` | same Groq key |
   | `TELEGRAM_WEBHOOK_SECRET` | any random string you invent (see below) |
   | `PUBLIC_URL` | the service's public URL, e.g. `https://xxx.up.railway.app` |
   | `DATA_DIR` | `/data` (already set in the Dockerfile; override only if you mount elsewhere) |
   | `TIMEZONE` | `Asia/Jerusalem` (default) |
   | `REPORT_CRON_DAY` / `REPORT_CRON_HOUR` | `mon` / `8` (default) |

   `TELEGRAM_WEBHOOK_SECRET` matters: the webhook URL is public, and without a shared
   secret anyone who found it could POST fake updates to add topics or trigger reports.
   Telegram echoes the secret on every call and the service rejects anything else with
   a 403.
4. **Generate a domain** for the service (Railway -> Settings -> Networking), then set
   `PUBLIC_URL` to it and redeploy. On startup the service calls `setWebhook` itself --
   check the deploy logs for `setWebhook -> 200`.
5. Message the bot. The reply should be immediate.

### The GitHub Actions workflows are now disabled on purpose

**Telegram permits either `getUpdates` or a webhook, never both** -- whichever runs
second gets HTTP 409, and a stray `getUpdates` can knock the webhook offline. Both
workflow schedules are therefore commented out, leaving only `workflow_dispatch` as a
manual fallback. Do not re-enable the `telegram-bot.yml` schedule while Railway is
live. The weekly `reporter.yml` schedule is off for a different reason: Railway already
runs the weekly digest, and both running would send every chat two digests.

## Multi-tenant: works in any number of Telegram chats

There's no fixed "chat ID" to configure -- **any chat that messages the bot
auto-registers itself** with its own topics and its own weekly digest. This is why the
old `TELEGRAM_CHAT_ID` secret is gone; it doesn't mean anything once more than one chat
is in play.

- First message from a new chat gets a welcome + is asked what it's interested in.
- Reply in plain English (e.g. "psychedelics and consciousness") and the bot drafts a
  Europe PMC search query for it via Groq and adds it as a topic -- see
  `reporter/topic-draft-prompt.md`. Or use `/addtopic <name> | <exact query>` if you
  want to write the query yourself.
- `/topics` lists that chat's topics, `/removetopic <name>` disables one.
- Each chat's state lives at `reporter/chats/<chat_id>/` (`topics.yaml`, `seen.json`,
  `meta.json`) -- dedup is per-chat on purpose: two chats interested in the same thing
  each still need to see a paper neither has been sent yet.
- **No allowlist.** The bot's username isn't a secret once you've shared it, so anyone
  who has it can register a chat and start using it -- accepted as low-stakes here
  since Groq's free tier and Europe PMC cost nothing per use. Add a check in
  `chats_store.py` if that ever needs to change.

Your existing single-chat setup (topics + already-seen papers) was migrated
automatically into `reporter/chats/5648569295/` -- nothing was lost or needs re-adding.

## Two workflows

- **`reporter.yml`** -- Mondays 08:00 Israel time. Loops over every registered chat:
  fetches that chat's new papers (Europe PMC), writes a synthesis brief and infographic
  via Groq, sends both to that chat's Telegram, commits the updated `seen.json` files
  back to the repo. Real expected runtime: well under a minute of actual work per chat
  -- timeout is set to 15 minutes purely for headroom.
- **`telegram-bot.yml`** -- every 5 minutes. Registers new chats, handles
  `/addtopic`, `/removetopic`, `/topics`, and plain-English topic descriptions (see
  above), commits any changes under `reporter/chats/`.

## Setup status

- [x] Repo created (public, github.com/omrishlomy) and pushed.
- [x] Groq API key and Telegram bot token added as repo secrets.
- [ ] Confirm Actions is enabled on the repo (Actions tab -- first-time repos sometimes
  need this confirmed explicitly).
- [ ] Trigger the reporter once manually to confirm it works end to end: Actions tab ->
  "Weekly paper digest" -> Run workflow. Should finish in well under a minute of actual
  compute per chat (plus a few seconds of GitHub's own job startup overhead) -- if it's
  still running after several minutes, something is actually wrong (check the job log
  for a Groq API error, most likely a missing/invalid `GROQ_API_KEY`).
- [ ] Try adding a second chat: message the bot from another Telegram account (or a
  group it's added to) and confirm it registers and takes a topic.

Once confirmed working, you can retire the Windows Scheduled Task
(`ClaudeAgent-Reporter`) on the PC -- this repo replaces it. The download-filer agent
is unaffected and keeps running locally exactly as before; it was never part of this
migration.

## Known constraints (read before assuming something's broken)

- **Groq free tier has rate limits**, shared across all chats using this bot. Fine at
  small scale -- see https://console.groq.com/docs/rate-limits if this grows a lot.
- **Topic commands can take HOURS to land, not the ~5 minutes the cron implies.**
  The workflow is scheduled `*/5` but GitHub heavily throttles scheduled workflows on
  free/public repos. Measured over 21 real runs, actual gaps were **120-280 minutes**
  (2-4.5 hours), never 5. GitHub documents that "the schedule event can be delayed
  during periods of high load" -- in practice that delay is the norm here, not the
  exception. Lowering the cron interval does not help; the scheduler, not the cron
  expression, is the limit.
  - Need a topic added *now*? Trigger the workflow by hand: Actions tab -> "Telegram
    topic manager" -> Run workflow. That runs within seconds.
  - Want genuinely instant replies? That needs a Telegram *webhook* pointed at a
    persistent HTTPS endpoint, which Actions fundamentally cannot provide (ephemeral
    runners, no inbound URL). A free serverless function (e.g. Cloudflare Workers)
    could host it -- a separate piece of infrastructure, not a tweak to this repo.
- **Telegram only retains pending updates for ~24 hours.** If the bot is broken or
  paused for longer than that, messages sent in the meantime are dropped by Telegram
  and can never be recovered -- you just send a new one.
- **In GROUP chats, the bot cannot see plain text by default.** Telegram bots ship with
  privacy mode ON, which means inside a group a bot only receives messages that are
  commands (`/topics`, `/addtopic ...`) or direct replies to its own messages -- ordinary
  chatter is never delivered to it. Consequences for this bot:
  - In a group, plain-English topic descriptions will be silently ignored; use the
    `/addtopic <name> | <query>` command form instead.
  - To allow plain text in groups, message `@BotFather` -> `/setprivacy` -> pick this
    bot -> **Disable**. Note this means the bot then receives *every* message in that
    group, and each non-command message would be treated as a topic description -- so
    leaving privacy ON is usually the better choice for a busy group.
  - One-to-one chats are unaffected: the bot sees everything you send it.
- **Plain-English topic drafting can misfire** -- it's one Groq call with no human
  review before the topic is saved. If a drafted query looks wrong, `/removetopic
  <name>` and try again with different wording, or use `/addtopic <name> | <query>`
  to write the exact query yourself.
- **Quality vs. Claude:** not independently re-measured against gpt-oss-120b yet (the
  quality comparison on record was against the retired local Qwen2.5-7B run, which was
  worse). Worth a real side-by-side after a few real Monday runs land.
- **Concurrent-commit risk:** the weekly reporter and the 5-minute poller both commit
  to `reporter/chats/`. Both now do `git pull --rebase` before pushing, which handles
  the common case, but a very unlucky simultaneous push could still need a manual
  `git pull --rebase` if a workflow run ever fails on a push step.
