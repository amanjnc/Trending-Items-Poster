# Trending Items Poster

Collect fresh stories, rank them, remove duplicate URLs, and publish attributed headline-and-link posts to X using TweetKit-X. Includes a local review dashboard and a GitHub-only automation workflow.

**No computer or server needs to stay running:** GitHub Actions can run the scheduled publisher. It uses an X login-session cookie rather than a paid developer API. It is not activated until configured.

**Start here: [Step-by-step GitHub setup](docs/GITHUB_SETUP.md).**

## GitHub automation

The `Automatic X posting` workflow is currently a **manual, TechCrunch RSS-only test**: it attempts up to two posts per live run. Scheduling has been removed, so `AUTOPOST_ENABLED` does not currently start any jobs. Existing Hacker News and Google Trends stories are excluded from selection. The rolling 400-attempt cap and durable duplicate protection remain in place.

History has already been initialized if your earlier test passed. Run the workflow on `main`, check the live-posting option, leave initialization unchecked, and leave reconciliation fields blank. A green run can still mean fewer than two posts when there are insufficient fresh, unposted candidates or a limit prevents sending. The run report records the outcome.
Runs retain URL deduplication, posting attempts, and the daily limit across fresh GitHub runners. A reservation is saved remotely before each post. A failed history save prevents posting. Timeouts and uncertain outcomes pause publishing until reconciled. Source checkout files and existing local review decisions are not overwritten. The runner uses a separate database.

400/day is a target, not a minimum guarantee. Source shortages, login challenges, rate limits, and delayed GitHub schedules reduce volume. Standard public-repository runners are generally free; private-repository minutes depend on your plan. TweetKit's author warns of account restrictions because this uses unofficial X web endpoints. No rate-limit or login-challenge bypass is included.

## Local development

Requires Python 3.12+. Collectors and dashboard use the standard library. Install pinned dependencies for TweetKit-X publishing:

```sh
python -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m unittest discover -s tests -v
python app.py collect --sources hn trends
python app.py serve
```

The local review dashboard binds to loopback port 8000. It has no multi-user authentication and must not be exposed publicly. Approve drafts after checking the original source; `python app.py export` prints approved drafts. The automatic publisher selects eligible new stories without requiring approval and respects dismissed stories. Already published stories remain excluded by the publication ledger regardless of review status.

`TRENDING_DB` sets the SQLite location (default `.local/stories.db`); `TRENDS_GEO` is a two-letter uppercase country code (default US). GDELT is available through `python app.py collect --sources gdelt`, but is not required by the automated workflow because its availability has been unreliable.

Local automation preview:

```sh
python automation.py
```

To run live on an optional always-on host, supply `TWEETKIT_COOKIE` as a real process secret and run `python automation.py --live --daemon`. Its default local interval is 216 seconds, with a rolling cap of 400 attempts. The current GitHub test uses manual two-post batches instead. Never run local and GitHub publishers independently against the same account: they would not share their posting ledgers.

## Data and validation limits

Collection accepts items from the last 48 hours; automation selects only candidates within 24 hours and never future timestamps. Google Trends supplies a trend timestamp, not necessarily the article's own publication time. GDELT's timestamp is its observation time. URL normalization removes common tracking parameters. Different URLs covering the same event are not clustered.

Discovery scores use freshness and available HN engagement, with age adjustment at selection. They are neither fact checks nor guaranteed virality predictions. Posts use truncated source headlines and links, without full article copying or generated commentary. Telegram, Reddit, image/video generation, and cross-source fact verification are not implemented.

The separate `Collect trending stories` workflow is manual-only and exports a disposable JSON report; it does not post. The posting workflow alone owns durable posting state. Do not delete or reset that state to recover a failed run. See the guide for reconciliation and stopping instructions.

Tests verify adapters with fixtures/mocks and history persistence with real local Git remotes. They make no public X posts. Live account authentication and GitHub-hosted scheduled execution must be verified after secure account setup.
