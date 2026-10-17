# Trending Items Poster

A persistent news queue running on GitHub Actions, using public Telegram channels and The Verge RSS. Collect about every two hours, clean and rank the stories, and publish one at a time with adaptive 2–4 minute spacing. ClashReport receives a ranking boost.

**Start with the [GitHub setup guide](docs/GITHUB_SETUP.md)** and the **News queue** workflow's no-post preview. Scheduling is disabled until `NEWS_QUEUE_ENABLED=true` is configured. The existing X session secret is reused; no paid X developer API or Telegram account is required.

Sources are configured in [sources.json](sources.json). They include bricsnews, hitech, hiaimediaen, Cointelegraph001, cointelegraph, nytimes, WatcherGuru, rtnews, ClashReport, DDGeopolitics, and The Verge. Only accessible public Telegram web previews are collected. Individual failures and rejection/duplicate counts appear in the run report.

## Processing

- Remove explicit promotions, subscription footers, source handles, and hashtag formatting while preserving meaningful news text.
- Filter stale/future/empty candidates and media-only Telegram messages.
- Rank by freshness, configured priority, available views, coverage count, and source diversity.
- Deduplicate URLs and exact/near-identical text, keeping recent posting history across runs.
- Save every posting reservation before contacting X; pause on ambiguous post results.
- Enforce a rolling 400-attempt daily cap. Sparse queues can run empty; no duplicates are invented to hit a quota.

There is no independent fact verification, paid AI rewriting, translation, or image/video posting. Text matching and ad filters are heuristics. The setup guide describes limits, recovery, free-runner conditions, and live validation requirements.

## Replies

Live workers check recent posts periodically and like retrieved direct replies from other users, as requested, regardless of sentiment. Observed replies are queued durably, and likes are deduplicated. No text replies are sent. Set `AUTO_LIKE_REPLIES=false` to disable likes. Hidden replies, large backlogs, missing account identity, and X limitations can prevent complete coverage.

## Local development

Requires Python 3.12+. Dependencies are pinned, including TweetKit-X at an inspected commit.

```sh
python -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m unittest discover -s tests -v
```

`newsroom.py` implements sources, cleanup, deduplication, ranking and queue rules; `queue_worker.py` implements the bounded worker; `engagement.py` implements reply review/likes. Tests use fixtures, simulated clocks and real local Git remotes; they never post publicly. Live provider access and actual reply matching must also be verified on GitHub.

GitHub retains SQLite state on `autopost-state`. Do not delete it to fix failures. `TRENDING_DB` controls the database path. The optional local review dashboard remains in `app.py`; it binds to loopback and must not be exposed publicly. Older collectors remain available for development, but the **News queue** workflow uses only `sources.json`. The old posting workflow is now a no-post history setup/recovery tool.
