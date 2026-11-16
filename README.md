# Trending Items Poster

A selective, fast X news account for **politics and geopolitics**, plus huge or political tech/business/crypto stories. It runs entirely on GitHub Actions; no server is needed.

**[Setup guide](docs/GITHUB_SETUP.md)**: add `GEMINI_API_KEY`, preview, then set `NEWS_QUEUE_ENABLED=true`.

## What it does every 10 minutes

```
collect  →  cluster into events  →  score  →  AI edit top events  →  publish ≤1 if it clears the bar
```

- **Collect** (`newsroom.py`): Telegram channels, mainstream RSS feeds (Google News sections, BBC, Al Jazeera, Guardian, NYT, Politico, The Hill, NPR, Fox, CNBC, TechCrunch, The Verge) and Google Trends. Uses a rolling 6-hour intake window. Configured in `sources.json`.
- **Cluster** (`events.py`): merges reports of the same story from different outlets, and measures how many outlets carry it and how many joined in the last hour.
- **Score** (`scoring.py`): heat + interest + talkability + freshness + media − fatigue. Every part is saved in the report, so you can see why something was or wasn't chosen.
- **AI edit** (`editor.py`): Gemini (free tier) with optional Groq fallback decides whether a story is worth posting and writes a hook-first post from the source reports only. A validator rejects links, numbers that aren't in the sources and invented quotes. Without a key, it falls back to rule-based wording.
- **Publish** (`prepared.py`): default limit of 30 posts a day, a 40-minute gap, and a quality bar that rises as the day fills up and during US night hours. Breaking stories take a fast lane. Posts carry no links (an optional source-link reply can be enabled with `SOURCE_REPLY=true`). A material update to an already-posted story becomes a quote of the original.
- **Learn** (`engagement.py`, hourly): likes direct replies, reads the bot's own post metrics, and nudges topic weights toward what gets engagement.

Posting safety is unchanged. The reservation is saved before X is contacted, uncertain sends are never retried, and cooldowns and circuit breakers stay in place. Pending items expire within 24 hours.

## Local development

Python 3.12+:

```sh
python -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m unittest discover -s tests -v
TRENDING_DB=.local/dev.db .local/venv/bin/python queue_worker.py --mode auto   # preview, no posting
```

`TRENDING_DB` selects the SQLite state. GitHub keeps state on the `autopost-state` branch. Never reset history to fix an error.

## Honest limits

This is a ranking-and-editing system, not a fact checker or a virality guarantee. Clustering is rule-based and can occasionally miss paraphrases or merge related stories. TweetKit-X uses an unofficial X web session, and X can restrict automation.
