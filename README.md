# buh-bot

A Discord message at about **9 AM and 9 PM Central** with what happened on the YouTube channels since the last one:

- **Needs you**: a channel that's paused, a queue about to run dry (under 2 days of ready videos), a failed upload run, or an upload workflow that stopped being started
- per channel: new uploads (with views), the biggest mover, subscriber and view counts with the change since last time, how many ready videos are left (in days), and whether anything is still being made (commits and merged PRs)

Channels: HelloBuh (`hello-buh`), BuhExplains (`buh-explains`), Buhtown (`blocky-stories`), Six Degrees (`sixdegrees`). Add or change them in `CHANNELS` at the top of `digest.py`.

**It uses no Claude.** It reads only the public repos and the YouTube Data API and runs on GitHub Actions, so it keeps working when the usage limit is hit. That's when the runway line matters most: uploads continue from the queue on their own, and the digest says how many days are left before a channel goes quiet.

## Setup (about 15 minutes)

### 1. Discord: a bot that DMs you
1. https://discord.com/developers/applications → **New Application** (e.g. "buh-bot").
2. **Bot** → **Reset Token** → copy it. This is `DISCORD_BOT_TOKEN`.
3. **Installation** → enable **Guild Install** with scope `bot`, open the install link, and add it to any server you're in (a private one of your own is fine). A bot can only DM people it shares a server with.
4. Discord → Settings → Advanced → **Developer Mode** on. Right-click your name → **Copy User ID**. This is `DISCORD_USER_ID`.

*Simpler alternative:* skip the bot. In a private server: channel settings → Integrations → **Webhooks** → New Webhook → Copy URL, and save it as `DISCORD_WEBHOOK_URL`. It posts in that channel instead of a DM.

### 2. YouTube API key (optional, for views and subscribers)
Google Cloud Console (the same project as the upload credentials is fine) → APIs & Services → **Credentials** → Create credentials → **API key**. Restrict it to *YouTube Data API v3*. This is `YT_API_KEY`. A digest uses about 10 of the 10,000 daily quota units.

### 3. Secrets
In this repo: Settings → Secrets and variables → Actions → **New repository secret** for each of `DISCORD_BOT_TOKEN`, `DISCORD_USER_ID` (or `DISCORD_WEBHOOK_URL`) and `YT_API_KEY`.

Test it: Actions → **digest** → Run workflow (tick *force*). You should get the DM within a minute.

### 4. The clock: cron-job.org
These are the same steps as `blocky-stories/docs/external-cron.md`, with two jobs:

1. A fine-grained token: Repository access **Only `evanlin23/buh-bot`**, Permissions → **Actions: Read and write**, nothing else.
2. On cron-job.org, create two jobs, each set like this:
   - URL `https://api.github.com/repos/evanlin23/buh-bot/actions/workflows/digest.yml/dispatches`
   - Method **POST**, body `{"ref":"main"}`
   - Headers `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28`
   - Time zone **America/Chicago**, which handles daylight saving time
   - Schedules **9:00** (first job) and **21:00** (second job). Make the second one with **Clone** so you don't paste the token twice.

A successful call returns HTTP 204.

## Running it locally
```bash
GITHUB_TOKEN=$(gh auth token) YT_API_KEY=... python digest.py --dry-run
```
`--dry-run` prints the digest and doesn't send or save anything. `--force` sends even if this morning's or evening's digest already went out.

## How "ready" is counted
Each count mirrors that repo's upload script:
- **HelloBuh**: Shorts with status `ready` on the `videos` branch that aren't uploaded or posted by hand. 3 a day.
- **BuhExplains**: `buhexplains/queue.txt` entries that aren't uploaded and have an episode package. 2 a day.
- **Buhtown** (approximate): stories with a passing final `qa/<story>.json` that aren't uploaded or held. The real check also confirms the film artifact is current, so the true number can be a little lower. 2 a day.
- **Six Degrees**: folders left in `queue/` on the `queue` branch. 2 a day.

`state.json` is what the last digest saw: which videos it reported, ready lists, and view and subscriber counts. It powers the "new" and "+N" numbers, and the workflow commits it back after each run.
