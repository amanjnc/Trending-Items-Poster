# Two-post TechCrunch RSS test on GitHub

This is currently a manual test. The automatic schedule has been removed. Each live run attempts at most two fresh, unposted TechCrunch items. Hacker News and Google Trends candidates already stored in history are excluded. `AUTOPOST_ENABLED` has no effect while the schedule is absent.

## Run the test

1. Open your repository's **Actions** tab.
2. Select **Automatic X posting**.
3. Click **Run workflow** and select **main**.
4. Check **Post up to TWO TechCrunch stories** (the live-posting option).
5. Leave **Initialize history** unchecked if your earlier initialization succeeded.
6. Leave reconciliation fields blank.
7. Run the workflow, wait for completion, and inspect your X profile and run report.

The existing `TWEETKIT_COOKIE` repository secret is reused; do not paste it into chat or workflow inputs. No paid X developer API is used. Standard public-repository GitHub runners are generally free; private repositories consume your plan's minutes.

A successful run may post fewer than two items if there are not enough fresh candidates or if the rolling daily limit applies. The code never adds duplicates to fill a quota. Posts remain shortened headlines plus links while content quality is evaluated.

## How history is retained

The `autopost-state` branch stores `state.db`: article metadata, URL IDs, and posting outcomes. Every reservation is saved remotely before contacting X. Failed persistence prevents posting; uncertain results pause future sends. Jobs share a concurrency group and cannot overwrite newer history from a stale runner. The state branch contains no session cookie. Do not delete it or reinitialize history to fix a posting problem.

If this is the first-ever run, initialize with a manual dry run: check **Initialize history** and leave live posting off. No X credentials are needed for the dry run.

## If a post has an uncertain result

Check your X profile. The run report lists uncertain story IDs. Run the workflow manually with live and initialization off. Enter one uncertain story ID and either the confirmed numeric X post ID or check **I checked X and this attempt was not posted**. This reconciles history without sending anything. Confirmed not-posted candidates remain reserved and are not retried automatically.

If X expires the session, replace `TWEETKIT_COOKIE` under **Settings → Secrets and variables → Actions → Secrets**. TweetKit-X requires `auth_token` and `ct0` from your own session (preferably also `twid`). Follow its upstream cookie guide: https://github.com/nsozturk/tweetkit-x#getting-your-session-cookie . Never send cookies in chat, screenshots, source, issues, or logs. The upstream author warns that unofficial web-session automation may lead to account restrictions. This workflow does not bypass rate limits or login challenges.

To prevent additional manual tests, do not launch another run. Cancel an in-progress run through Actions if needed. Scheduling remains off until deliberately restored in a future change.
