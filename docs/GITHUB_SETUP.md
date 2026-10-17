# News queue: GitHub setup

Use the **News queue** workflow. It replaces the old two-post TechCrunch test. **Posting history setup and recovery** is now a no-post recovery tool.

## First: check the sources without posting

1. Open the repository's **Actions** tab and select **News queue**.
2. Click **Run workflow**, selecting **main**.
3. Leave **Publish news** unchecked. Leave **Stop after two posts** checked.
4. Run it and inspect **Collect, rank and publish one at a time**.
5. Download the `queue-report` artifact if you want the full source report and five ranked draft examples.

The preview collects only when the previous collection is at least two hours old, saves the queue, and makes no X requests or likes. Source errors appear individually; a green run does not mean every source worked. Missing, private, renamed, or web-preview-disabled Telegram channels are reported as errors, not silently counted as collected. If a preview immediately follows another collection, it reuses the queue. Failed-source retries happen on the next due collection.

No new Telegram login or API credentials are needed: this version reads public `t.me/s/<channel>` pages. It cannot access private channels or promise complete Telegram history. It reads up to three preview pages/channel per collection; media-only messages are skipped. The configured handles are user-selected sources, not a verification of who operates each channel. `@rtnews telegram` was interpreted as `@rtnews`.

## Then: a two-post live test

1. Run **News queue** again.
2. Check **Publish news**.
3. Keep **Stop after two posts** checked.
4. Wait: the posts will be separated by the adaptive interval, normally 2, 3, or 4 minutes, rather than posted as a batch.

The existing `TWEETKIT_COOKIE` Actions secret is reused. This live test also checks replies to your recent posts and likes retrieved direct replies, including criticism, as requested. It excludes your own comments, never writes text replies, and never intentionally likes the same reply twice. To test posts without likes, set repository variable `AUTO_LIKE_REPLIES=false` before running.

## Enable the ongoing queue

In **Settings → Secrets and variables → Actions → Variables**, create:

- `NEWS_QUEUE_ENABLED` = `true`

No additional secret is required. The old `AUTOPOST_ENABLED` variable is not used by this workflow. The schedule starts jobs every two hours at minute 7 (UTC even-numbered hours). Each job runs for up to 115 minutes, collects when the two-hour collection timer is due, and publishes one queued item at a time. It exits early if the queue empties, the 400-attempt rolling daily cap applies, or a posting result is uncertain.

`AUTO_LIKE_REPLIES` defaults to enabled unless explicitly set to `false`, following your request to like all replies. `TWEETKIT_COOKIE` should include the `twid` user identifier as well as `auth_token` and `ct0`; if the account identity cannot be determined, likes are skipped and `identity_ready` is false in the reply report.

To stop scheduled jobs, set `NEWS_QUEUE_ENABLED=false` or disable **News queue** in Actions. Cancel any running job too. A manually launched live job still posts even with the schedule disabled.

## Sources and editing rules

`sources.json` contains the ten selected Telegram handles and The Verge RSS feed. ClashReport has a priority boost; freshness, available Telegram view counts, coverage by distinct configured sources, and recent source repetition also influence rank. Coverage counts do not prove independent corroboration.

Ads with explicit paid-promotion/referral language are rejected. Subscription lines, standalone source handles, and hashtag footers are removed; inline topic hashtags keep their words without the `#`. Small trailing source handles are stripped while retaining the news text. These are conservative text rules, not a perfect advertisement classifier. Inspect previews to tune source-specific formats.

The Verge contributes article headlines and links. Telegram contributes cleaned text excerpts and a source permalink. Long Telegram text uses complete sentences when they fit, otherwise a word-boundary excerpt. X's 280-character budget is respected conservatively. There is no paid AI service, independent fact checker, translation, image/video upload, or automatic generated reply. Source links remain for attribution; channel footer advertising is removed.

## Adaptive pacing and duplicates

The queue is saved on `autopost-state` with the previous posting ledger, so source changes do not reset history or daily limits. Old Hacker News/TechCrunch candidates are not eligible in the new queue. It checks normalized URLs and exact/near-identical text across sources, including recent old posting history. Changed figures can remain distinct updates. Heuristic matching cannot identify every paraphrase, translation, or repeated event.

The delay is selected from 120, 180, and 240 seconds using queue size and time until the next collection. Heavy queue: 2 minutes; medium queue: 3; sparse queue: 4. The next due time is retained between jobs, and a minimum two-minute guard also applies. A very sparse queue can still run empty before two hours because the maximum requested delay is four minutes. It never repeats items to fill gaps. GitHub scheduling delays, worker handover, source collection, and external requests can create longer gaps; this is not a precision clock.

## Reply likes and limits

While a live worker is active, it checks replies about every 30 minutes, using a bounded scan of recent posts from the last 48 hours. It rotates through up to 20 posts/scan and retrieves up to 100 replies per post, subject to TweetKit/X pagination and visibility. It stores observed direct replies durably and likes pending replies within a short time budget so news posting can continue. Large conversations, hidden/deleted replies, inactive workers, and X restrictions prevent a guarantee that every reply will be seen or liked.

The pinned TweetKit version omits direct-parent IDs from its public flattened replies. A response hook reads only those parent IDs from the same response in memory; it sends no extra credentials anywhere. Replies without confirmed direct parentage are not liked. This adapter is covered by fixtures but still needs live compatibility validation on your account.

An uncertain like is not retried. Other pending/new replies can continue in a later scan. The report lists uncertain reply IDs; check those manually on X. This avoids accidentally repeating uncertain actions. Any X rejection stops further likes for that scan. No rate-limit or login-challenge bypass is included.

## Recover an uncertain post

Never delete or reset `autopost-state`. Check your X profile first. Open **Posting history setup and recovery**, choose **main**, leave initialization unchecked, and enter the uncertain story ID from the report. Supply either the confirmed numeric post ID or check **I checked X and this attempt was not posted**. This saves the outcome without posting. A confirmed-not-posted story stays reserved and is not retried automatically.

If history has never been initialized, use that same recovery workflow with **Initialize history** checked first. Existing users should not reinitialize it.

## Hosting and account limitations

No computer or server must remain on. Standard public-repository GitHub runners are generally free; private repositories can consume roughly 40,000 runner minutes/month if workers stay busy nearly all day, so check your plan before enabling. GitHub can delay scheduled jobs or disable inactive public-repository schedules.

TweetKit-X uses your unofficial X web session. Its author warns this can lead to account restrictions. X can expire the cookie or reject runner IPs. Replace the secret securely if necessary; never send cookies in chat, screenshots, source, reports, or logs. The state branch stores public story/comment metadata, not credentials. Do not run a second independent publisher on the same X account with separate history.
