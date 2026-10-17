# Prepared queue: GitHub setup

No always-on computer or server is needed. Your existing `TWEETKIT_COOKIE` repository secret and `autopost-state` history are reused. Do not reinitialize or delete history.

## 1. Preview collection and prepared posts

1. Open **Actions → Collect and prepare news → Run workflow**.
2. Choose **main** and leave **Also publish ONE prepared item** unchecked.
3. Run it and open the preparation report/artifact.

The report shows source results, prepared-item count, confirmed handles, and top/bottom five with rank reasons. Preview can read X profiles using your existing session to confirm mentions, but sends no posts or likes. A green collection does not prove every source succeeded: review each source's status.

## 2. Test one fast publication

1. Open **Actions → Publish next queued post → Run workflow**.
2. Choose **main**.
3. Check **Send ONE already-prepared post**.
4. Run it and inspect your X profile and job duration.

This job does not collect, rewrite, rerank, look up profiles, scan replies, run tests, or wait for the next slot. It checks eligibility/expiry and posting limits, optionally fetches/uploads one source image, reserves the attempt durably, posts once, records the result, then exits. A second manual run within two minutes exits without sleeping. Empty/expired queues exit without posting.

## 3. Enable ongoing operation

Under **Settings → Secrets and variables → Actions → Variables**, set:

- `NEWS_QUEUE_ENABLED` = `true`
- Optional: `AUTO_LIKE_REPLIES` = `false` to stop automatic reply likes. Otherwise your requested all-replies preference remains enabled for retrieved direct replies.

The schedule is partitioned into **12 collection-and-publication jobs + 276 publication-only jobs/day**. Collection runs at minute 7 of even UTC hours. Publishing runs in the remaining five-minute slots; no duplicate publication slot is scheduled at the collection time. Reply checks are separate hourly jobs at minute 24. The global history concurrency group serializes jobs; GitHub delays can cause gaps or skipped pending runs. This is not a precision scheduler.

Every publication job sends at most one post. There are no deliberate sleep periods. The old two-post/sleeping-worker mode is removed from active workflows.

Disable scheduled operation with `NEWS_QUEUE_ENABLED=false`; cancel any currently running job too. Manual live dispatch still works with scheduling disabled.

## One external cron job

Use a fine-grained GitHub token restricted to this repository with **Actions: read and write**. Keep it in cron-job.org's Authorization header, never in the URL or this repository. Create one disabled job initially:

- Schedule: `*/5 * * * *`
- Method: `POST`
- URL: `https://api.github.com/repos/amanjnc/Trending-Items-Poster/actions/workflows/auto.yml/dispatches`
- Headers: `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `Content-Type: application/json`, `X-GitHub-Api-Version: 2022-11-28`
- JSON body: `{"ref":"main","inputs":{"live":true}}`

Set the GitHub repository variable `NEWS_QUEUE_ENABLED=false` and let any active old runs finish before enabling this external job. Do not enable a second external collection/publishing job. Disabling this variable also disables the old hourly reply-like schedule; reply checks remain separately available by manual dispatch and can be configured separately later.

The first trigger in each two-hour UTC period collects, cleans, resolves mentions, ranks and publishes one item. Other triggers publish one saved item only, without importing collectors or editors. If a period's first trigger is delayed, the next trigger collects. A successful manual collection also updates the marker. The decision and marker are protected by the shared workflow concurrency group and durable state saves. If every source fails, collection retries after thirty minutes while intermediate ticks can use the existing queue. Failed/uncertain public sends still pause; no automatic repost occurs.

Normal daily work remains 12 collection ticks plus 276 publishing-only ticks. An HTTP 204 response means GitHub accepted dispatch, not that publishing succeeded; inspect Actions and the saved report. Jobs shut down between ticks, and GitHub runner minutes still apply. Token expiration, disabled cron jobs, GitHub delays and outages can interrupt scheduling. This workflow has no built-in cron trigger and is only externally/manually dispatched.

## Account mentions and writing

To test one particular source item, open **Actions → Publish a selected story → Run workflow** on `main`. Supply its exact configured Telegram message URL. Leave publishing off for a preview/media inspection, or check publishing to send that item. The default URL is `https://t.me/ClashReport/101477`, the Carney quote beginning “If they're ready to move, we'll move.” The workflow refreshes this source message and verifies its identity mentions before selecting it. It preserves other queued drafts and never bypasses previous-post reservations. Its report includes actual source attachment kind and download availability. Unresolved identity checks, expired items, or unavailable selected media stop the job without sending another item.

`entity_map.json` contains initial identity mappings including Mark Wahlberg, Replit, Anthropic, and Amjad Masad. Collection can also discover explicit named X profile links in supported publisher articles and unambiguous account claims from Wikidata. Automatic discovery is bounded to eight new name candidates per collection and cached. Handles are never guessed by stripping spaces from names.

Before a handle is used, TweetKit's `user_profile` result must exist and match the intended mapped identity's name/username. Positive profile results are cached for 24 hours, mismatches for six hours, temporary lookup failures for fifteen minutes. Legacy failed entries from the incorrect SDK method are retried automatically. The preparation report includes per-handle lookup outcomes, without raw account responses or credentials. Curated mappings take precedence over discovered links; ambiguous or unavailable lookups keep the original name. Contextual short names are supported: “Canada's Carney” and “Carney” in Canadian context map to `MarkJCarney`, subject to the same profile check. A short alias requires context in the same item, not another queued story. Matching a structured record and profile is not a guarantee against every erroneous external identity claim. A paid blue check alone is not treated as proof of identity. Not every person/company can be automatically resolved; review previews and extend curated mappings for recurring missing entities.

For a headline such as “Replit is no longer working with Anthropic, CEO says,” the prepared result can use `@Replit`, `@AnthropicAI`, and `@amasad` **only if those profiles are confirmed and the source explicitly names Amjad Masad as the speaker**. Otherwise it keeps the unresolved name or attribution. It never assigns a quotation based solely on an assumed current job title.

Editing is deterministic and free: concise wording, source-footer removal, relevant mentions, source paragraphs separated by one blank line, and excerpts within X's character budget. Paragraph breaks survive length trimming. It preserves uncertainty such as “reportedly” or “wants to.” It does not invent facts, quotes, impact statistics, hype, or guaranteed virality. No paid language-model service is configured.

## Images and links

The queue uses this strict format order: **text + image > text > text + image + outbound link > text + outbound link**. Freshness, source priority, content detail and diversity order items within each format; they cannot move a lower format above a higher one. Expiry and promotional-content filters apply before ranking.

The collector captures Telegram photo references and supported RSS/publisher image URLs. Telegram's `telesco.pe` and `cdn-telegram.org` image hosts are supported. One image can be uploaded with a post. Binary media is not packed into the Git history database: the publisher fetches the one selected image from a restricted source-host allowlist and uploads it to X. Redirects, format signatures and a 2.5 MB cap are checked. This extra network work can make image posts slower than text-only posts. Missing/unsupported images fall back to text before sending; failures after a public send attempt pause rather than risking a duplicate.

Ordinary prepared posts omit outbound links while retaining source provenance in the backend/report. Essential action/reference links may remain. Links to X itself are not penalized as outbound links. Source material is not independently fact-checked, and images are not generated by the app.

Telegram HTML video markers are distinguished from photos, even if a video also has a poster image. Downloadable MP4 media up to 20 MB is validated by signature and uploaded as video through TweetKit; videos receive the same format priority as photos. Unsupported or unavailable video may fall back to text in normal publishing. The selected-story test requires the attachment and stops if it cannot be uploaded. Media availability, X processing and account limits can prevent a successful video post; the source inspection report does not guarantee X accepts an upload.

After a confirmed successful post, its prepared queue payload, source media references and duplicate editorial body are removed. Temporary image/video downloads are deleted automatically, and media binaries are never put in the persistent database. Keep the small URL/story/publication history for deduplication and reply scans. Cleanup also removes old posted payloads on the next successful post or collection. Uncertain sends retain their payloads until reconciled; they are never automatically retried. Historical Git snapshots still contain prior metadata, so this cleanup does not rewrite repository history.

## Ranking, age and expiration

At collection time all eligible candidates are prepared and ranked once. Freshness, ClashReport priority, available Telegram views, source coverage, content detail, format bonus and source/event diversity determine the saved order. A small urgency bonus is capped at five points and applies only to sufficiently strong candidates. It never overrides hard expiration.

Typical shelf lives:

- Six hours: outages, evacuation/weather/air-raid alerts and live results.
- Twelve hours: market-price updates and explicitly short-lived phrasing.
- Twenty-four hours: ordinary news.
- Forty-eight hours: launches, releases, funding and partnership news.
- Up to five days: selected research, explainers, reviews and guides.

Supported explicit relative/ISO-dated event deadlines shorten those limits. These are heuristic relevance rules, not comprehensive event understanding. No item remains eligible beyond five days. The publisher only performs a cheap expiration/eligibility check; it does not rerank. Very small queues can run empty, and no duplicate filler posts are created.

## Reply likes

**Check replies and like** runs separately once per hour and can be manually dispatched. It processes retrieved direct replies under recent posts, excluding your own comments and avoiding intentionally repeated likes. It does not send text replies. Bounded scans, hidden/deleted replies, missing `twid` identity, X restrictions and large backlogs prevent a guarantee that every comment is seen or liked.

Observed replies and pending likes are retained in history. Uncertain likes are not retried. The pinned TweetKit adapter confirms direct parentage from response metadata; live compatibility still needs testing after library changes. Review-only/manual previews do not like comments.

## Efficiency and runtime measurement

Dependencies are restored as an installed virtual environment keyed by OS, architecture, exact Python version and requirements hash. Installation runs only on cache misses. Unit tests run on code pushes/PRs rather than each scheduled publication. The posting path imports no collector/editorial module.

The report includes `task_seconds` and `state_and_task_seconds`; the latter includes Git state restore. Neither includes GitHub runner allocation, checkout or cache setup. Use the full Actions job duration for actual billing estimates. Text posts are intended to finish quickly with a warm cache, but no sub-minute guarantee is made. Image upload, X's internal session requests, Git state writes, cold caches and service delays are unavoidable external costs.

GitHub rounds each job to whole runner minutes. Standard public-repository runners are generally free; private plans have allowances. At one billed minute per publication-only job and two to three per collection, the posting pipeline is roughly 9,000–9,360 minutes/30 days before extra setup overruns. Hourly reply checks add 720–1,440 minutes if each takes one to two minutes. Code-check jobs add their own usage. Measure real durations rather than treating those assumptions as promises.

## External scheduler later

The finite entry points are ready for an authenticated external scheduler:

- Dispatch `queue.yml` with `live=true` every two hours.
- Dispatch `publish.yml` with `live=true` for intervening posting slots.
- Optionally dispatch `replies.yml` with `live=true` on a separate cadence.

Disable `NEWS_QUEUE_ENABLED` when switching, so GitHub cron and the external scheduler do not both drive the queue. Store the scheduler's GitHub authorization only in its secure secret settings, never chat, repository source or a URL. No computer must remain open. If it triggers GitHub Actions jobs, GitHub runner billing still applies; an external scheduler alone does not remove the one-minute job rounding. No external service is connected yet.

## History recovery and account access

Never delete `autopost-state`. **Posting history setup and recovery** can initialize a new history or reconcile an uncertain story ID with its confirmed X post ID / confirmed-not-posted outcome. Reconciliation sends no post. An uncertain public send pauses further posting until resolved.

TweetKit-X uses your unofficial X web session, not a paid developer API. Its author warns this can cause account restrictions; X can expire the cookie or reject runner IPs. Keep `TWEETKIT_COOKIE` private, never paste it in chat/logs/screenshots, and replace it securely if necessary. State contains public news/comment metadata and prepared text, not credentials. Avoid a second independent publisher using separate history on the same account.
