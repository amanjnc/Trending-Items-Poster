# Trending Items Poster

A prepared news queue hosted on GitHub Actions. Collection does the expensive work; publishing takes one already-prepared item, sends it, saves its result, and exits. **No sleeping runner or always-on PC/server is required.**

[Step-by-step setup and scheduler details](docs/GITHUB_SETUP.md).

## Separate jobs

- **Collect and prepare news**: every two hours, fetch configured Telegram/The Verge sources, remove promotions, deduplicate, resolve account identities, edit posts, assign expiry and rank, and save the ordered queue. Scheduled collection also publishes one item.
- **Publish next queued post**: five-minute slots between collection runs. Only load state, select the next nonexpired prepared row, publish once, record outcome, and exit. No collection, profile lookup, rewriting, re-ranking, tests, reply checking, or waiting.
- **Check replies and like**: separate hourly jobs, preserving the user's preference to like retrieved direct replies from other users. Disable with `AUTO_LIKE_REPLIES=false`.
- **Code checks**: tests run when code changes, rather than on every posting job.

`NEWS_QUEUE_ENABLED=true` enables the built-in schedules. Publishing uses a cached installed Python environment. Cold cache misses, GitHub queue delays and X/media requests still affect runtime; no sub-minute or viral-performance guarantee is made.

## Prepared content

For one external cron job, trigger **Automatic news scheduler** (`auto.yml`) every five minutes. It collects/prepares and publishes one item on the first trigger in each two-hour UTC period, then publishes only one saved item on subsequent triggers. The persisted marker tolerates delayed or duplicate collection triggers. Disable the built-in schedules with `NEWS_QUEUE_ENABLED=false` before enabling external dispatches. Posting guards and history reservations remain active. This changes scheduler configuration, not GitHub runner billing. Reply likes remain a separate workflow.

Each collection admits only items published in the preceding two hours, measured against one fixed collection-start timestamp across every source. Older and future-dated items are rejected. This intake window is separate from the 24-hour maximum lifetime of items already queued; normal collections add fresh items without resetting pending items. An explicit queue reset removes pending content and media references while retaining all publication/reservation history.

Account mentions use curated mappings, source-linked identities, and bounded Wikidata account discovery, followed by cached X profile identity/existence checks. Unknown or ambiguous identities stay as names. See [entity_map.json](entity_map.json) for initial mappings. Speaker replacements require the source to explicitly identify the speaker; a company's current CEO is not automatically assumed to have made an unattributed statement.

Discovery prioritises complete people names after titles, company subjects and full institution names, with up to 32 new candidates per collection. Structured native-language profile names can be verified without requiring an English display name. Known contextual surnames such as Iranian President Pezeshkian are resolved separately from full names. Reports list discovery candidates/attempts and profile-verification outcomes. Short uppercase acronyms match case-sensitively so an account like `WHO` does not replace the pronoun “who.” A preparation-version change makes the automatic scheduler refresh queued drafts on its next tick.

Posts preserve source paragraph breaks with one blank line between paragraphs, including when excerpts must be shortened. No fabricated claims, quotes, statistics, or engagement bait are added. This is rule-based editing, not a paid AI writer, independent fact checker, or prediction of virality.

Among candidates allowed by the source-spacing rule, queue format order is: **text + image**, then **text**, then **text + image + outbound link**, then **text + outbound link**. Freshness and source priority order candidates within each format. No source may repeat within two queue positions; this hard rule takes priority over format preference. Available Telegram photos (including `telesco.pe`) and supported publisher images can be used. Source URLs remain in the backend; ordinary posts omit outbound URLs. Essential action/reference links may remain; internal X links do not get the outbound-link penalty. Image upload requires extra network work in the publishing job. Unsupported or unavailable images fall back to text before any public posting attempt.

Rank combines freshness, source priority (ClashReport boosted), available views, coverage, content detail, format preference and a small capped urgency bonus. Queue ordering requires at least two other posts between repeats of a source, including across collection boundaries. It defers candidates when no source fits and applies a further penalty to related speaker/event items. The publisher checks the last two actual published sources too, protecting spacing when items expire or are skipped. Exact and near-identical story text is deduplicated across sources; paraphrases and translations are not perfectly detected.

Telegram videos are detected separately from photos; a poster/thumbnail is never substituted for the video. Downloadable MP4 videos up to 20 MB can be uploaded using the pinned TweetKit media uploader. Videos share the attached-media priority of images. The manual **Publish a selected story** workflow refreshes one configured Telegram message, verifies mentions, reports its attachment type and optionally sends exactly that item. It stops on unresolved identity checks or unavailable media instead of choosing another item or falling back to text. An already posted/reserved story cannot be resent through this workflow.

All pending items have a hard maximum age of 24 hours from the source publication time, including research and guides. Short-lived alerts expire around six hours and market/time-sensitive items around twelve. Explicit supported deadlines can shorten this. Expired pending queue rows, editorial bodies and media references are removed at collection or publishing time; only small URL/deduplication history remains. The publisher enforces the age limit even for legacy drafts with longer expiry dates. Urgency never overrides the expiry gate and is only a minor ranking bonus.

## Local development

Python 3.12+:

```sh
python -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m unittest discover -s tests -v
```

`newsroom.py` collects/cleans sources; `postcraft.py` resolves identities and prepares/ranks posts; `prepared.py` is the small publisher; `queue_worker.py --mode collect|publish|replies` selects a finite task. `sources.json` holds the source configuration. `TRENDING_DB` selects SQLite state.

GitHub keeps state on `autopost-state`, including duplicate history, prepared posts, identity cache and reply actions. Posting reservations are persisted **before** contacting X. Uncertain outcomes are not retried automatically. Never reset history to fix an error. The existing no-post history recovery workflow remains available.

After confirmed successful publication, the prepared queue row, source media/attachment references and duplicate editorial body are deleted. Downloaded media uses temporary files removed when the task exits; media binaries are never saved into the state branch. Story URLs, text and publication IDs remain for duplicate detection and reply handling; old story text is shortened after seven days during cleanup. Prior Git snapshots can retain historical metadata. Failed/uncertain publication payloads are kept for reconciliation.

Tests use fixtures, simulated clocks and real local Git remotes; no tests publish/like real content. Live profile discovery, image uploads and GitHub job timings require account-connected validation. The optional legacy dashboard/collectors remain available locally; scheduled publishing uses only the prepared pipeline.
