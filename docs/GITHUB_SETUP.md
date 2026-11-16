# GitHub setup

No always-on computer or server is needed. Your existing `TWEETKIT_COOKIE` secret and the `autopost-state` history branch are reused. Do not reinitialize or delete history.

## 1. Add the AI writer key (free)

1. Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key (free tier).
2. In the repository open **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `GEMINI_API_KEY`, value: the key.
4. Optional backup provider: create a free key at [console.groq.com](https://console.groq.com/keys) and add it as `GROQ_API_KEY`. It is only used when Gemini fails or returns an unusable draft.

Without any key the bot still runs, but writes posts with the old rule-based wording (source text trimmed), which reads like reposted headlines.

Gemini's free tier may use prompts to improve Google's products. The bot only sends public news text, never credentials.

## 2. Preview

1. Open **Actions → Automatic news scheduler → Run workflow** on `main`.
2. Leave **Publish at most ONE item** unchecked and run it.
3. Open the job summary. It shows:
   - **Top of the queue**: rank, category, how many outlets carry the story, the AI's newsworthiness/talkability ratings and the draft.
   - **AI editor**: new calls, cached verdicts, declined stories and errors.
   - The would-be post (`preview`), or `preview (below quality bar …)` when nothing is good enough yet.

The full report (`posting-report.json` artifact) also contains per-source results, score parts and why the AI declined stories.

## 3. Turn it on

Under **Settings → Secrets and variables → Actions → Variables** set `NEWS_QUEUE_ENABLED` = `true`.

Optional tuning variables (defaults in brackets):

| Variable | Meaning |
|---|---|
| `DAILY_LIMIT` (20) | Maximum posts per rolling 24 hours. |
| `MIN_GAP_MINUTES` (40) | Normal minimum time between posts. Breaking news uses an 8-minute gap. |
| `MIN_SCORE` (60) | Quality bar. It rises by 2 for every post beyond 8 in 24h. |
| `QUIET_HOURS_UTC` (`5-10`) | Hours (UTC) when the bar is 15 points higher (US night). Empty to disable. |
| `SOURCE_REPLY` (true) | Reply to each post with the source link (and extra context). |
| `GEMINI_MODEL` (`gemini-flash-latest`) | Gemini model name. |
| `AUTO_LIKE_REPLIES` | `false` stops automatic likes on replies. |

## How it works

**Automatic news scheduler** (`auto.yml`) runs every 10 minutes. Each tick:

1. **Collects** the Telegram channels and RSS feeds in `sources.json` (Google News top/world/US/business/tech, BBC, Al Jazeera, Guardian, NYT, Politico, The Hill, NPR, Fox News, CNBC, TechCrunch, The Verge) plus Google Trends searches. A rolling 6-hour window means a delayed or missed run doesn't lose stories.
2. **Clusters** reports of the same story from different outlets into one *event*.
3. **Scores** each event:
   - heat: how many outlets carry it, how many joined in the last hour, Telegram views, Google News related coverage, Google Trends matches
   - interest: politics/geopolitics first. Tech/business/crypto only count when huge or with a political angle (bans, sanctions, tariffs, export controls, lawsuits…). Household names add interest.
   - talkability: conflict, firsts and records, firings and resignations, arrests, numbers and quotes. Opinion, podcasts and explainers are penalised.
   - freshness: two-hour half-life.
   - fatigue: a penalty for repeating the same people/places within a few hours.
4. **AI edits** the top events (max 4 new calls per tick, 250/day, verdicts cached). The model only sees the source reports. It declines minor or promotional stories and writes a hook-first post. A validator rejects drafts with links, numbers not in the sources or invented quotes. The model gets one corrective retry, then Groq is tried, then the rule-based draft is used.
5. **Publishes** at most one post, only if it clears the quality bar and the posting gap has passed. A big, fast-spreading political story (or a huge crossover story) takes the fast lane. The source link goes into a self-reply, because links in the main post reduce reach.

An event that was already posted is not posted again from another outlet. A genuinely new development (new numbers, and at least 2 outlets in the last hour, 45+ minutes later) can be posted as a **quote of the original post**, but only when the AI confirms it is new.

**Check replies and like** (`replies.yml`, hourly) still likes direct replies from other users. It now also reads likes, reposts and replies of recent posts and gently adjusts category weights (bounded 0.7–1.4) toward what your audience engages with. The job summary lists the best recent posts.

## One external cron job (optional)

GitHub's own cron can be delayed. For steadier timing, use cron-job.org with a fine-grained GitHub token restricted to this repository (**Actions: read and write**), kept only in cron-job.org's Authorization header:

- Schedule: `*/10 * * * *`
- Method: `POST`
- URL: `https://api.github.com/repos/amanjnc/Trending-Items-Poster/actions/workflows/auto.yml/dispatches`
- Headers: `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `Content-Type: application/json`, `X-GitHub-Api-Version: 2022-11-28`
- JSON body: `{"ref":"main","inputs":{"live":true}}`

Set `NEWS_QUEUE_ENABLED=false` when using an external trigger, so both don't drive the queue. Duplicate triggers within 8 minutes only try to publish from the saved queue.

## Safety (unchanged)

- A posting reservation is saved to `autopost-state` **before** contacting X. Uncertain outcomes are never retried automatically. A stale uncertain attempt (2+ minutes) is quarantined so other stories continue.
- Media uploads finish before tweet creation. A failed upload skips that story without pausing the queue.
- Rate limits and account-access rejections trigger a 30-minute cooldown. Three consecutive failures within an hour open a circuit breaker.
- Collector or editor failures are isolated, and the previously saved queue can still be used.
- Every pending item expires at most 24 hours after the source published it (alerts after ~6h, market items after ~12h).
- **Posting history setup and recovery** (`autopost.yml`) can initialize history or reconcile an uncertain story.

## Account mentions

`entity_map.json` plus bounded Wikidata discovery map names to X handles. Each handle is verified against the live X profile and cached. In AI-written posts, at most two accounts are tagged, each once, and never as the first word (X treats a post starting with @ like a reply). Short aliases need context in the same story.

## Selected story

**Publish a selected story** (`selected.yml`) refreshes one configured Telegram message and optionally sends exactly that item. It bypasses the quality bar but never the safety gates, and it uses rule-based wording.

## Costs and limits

- 144 scheduler ticks/day. On a private repository each tick costs at least one billed runner minute; public repositories use standard runners for free.
- Gemini's free tier comfortably covers ~100–250 calls/day. If you hit its limits, cached verdicts, the Groq fallback and the rule-based draft keep the bot running.
- TweetKit-X uses your X web session (unofficial). Keep posting volume moderate. X can expire the cookie or restrict automation. Keep `TWEETKIT_COOKIE` private.
- No ranking guarantees virality. The bot maximises the odds (speed, selection, hooks, low spam), but reach still depends on X's distribution and your account's history.
