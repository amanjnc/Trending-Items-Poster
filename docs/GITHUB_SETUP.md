# Run entirely on GitHub Actions

No always-on computer, VPS, or paid X API is required. GitHub starts a temporary runner for each scheduled batch and shuts it down afterward. You can manage GitHub from a phone browser. Initial X session-cookie extraction still requires a browser that can inspect its cookies; many mobile browsers cannot do this.

## What is included

- TweetKit-X, pinned to an inspected upstream commit, for text posts with source links.
- Automatic collection from Hacker News and Google Trends.
- A batch of up to five posts every 15 minutes, capped at 400 attempts in any rolling 24 hours.
- URL duplicate protection and persistent history on the `autopost-state` branch.
- Dry runs by default; scheduled live posting requires an explicit repository variable.
- Automatic pause after any uncertain posting result. No blind retries or rate-limit bypasses.

400 posts/day is a target, not a promised minimum. It requires at least 400 eligible distinct stories, enough successful scheduled runs, and an X session that accepts the writes. This small initial collector may not provide that many stories. GitHub schedules may be delayed, dropped, or disabled after prolonged public-repository inactivity. Posts arrive in batches, not uniformly throughout the day.

## 1. Confirm that the code is on GitHub

Open your `amanjnc/Trending-Items-Poster` repository. Its default branch should contain `app.py`, `automation.py`, `actions_runner.py`, `requirements.txt`, and `.github/workflows/autopost.yml`. A cloud environment snapshot alone does not upload these files to GitHub. The workflow only schedules when it is on the default branch.

## 2. Check GitHub costs and permissions

Standard GitHub-hosted Linux runners for public repositories are generally free. Private repositories use your plan's included minutes and can incur charges after those are exhausted. Check **Settings → Billing and licensing** (the location/name may vary by account) before enabling a frequent schedule. This workflow runs up to 96 jobs/day; each job's runtime is billed under GitHub's current rules. Paid larger runners are not used.

Do not make a private repository public without checking everything it contains. The separate history branch stores article metadata and posting status, but never the cookie. It is public when the repository is public.

In **Settings → Actions → General**, ensure Actions are allowed. This workflow requests `contents: write` for its history branch. Organization policies and protected-branch rules may still restrict this. It needs to create/update `autopost-state`; it does not change application source while running.

## 3. Initialize history with a no-post test

1. Open the repository's **Actions** tab.
2. Select **Automatic X posting**.
3. Click **Run workflow** and choose the default branch.
4. Check **Initialize history**.
5. Leave **Send real X posts** unchecked and leave reconciliation fields blank.
6. Run it and wait for the green check.
7. Open the run, then **Collect and publish batch**, to inspect collection results and the preview.

This creates the history branch and verifies it can be saved/restored. No X session is needed for this test. If all news sources fail, the run is marked failed. Do not delete `autopost-state` to troubleshoot a posting error; deleting it removes duplicate protection. Future runs must leave initialization unchecked.

## 4. Add your X session securely

TweetKit-X needs the cookie header from your own logged-in X browser, containing `auth_token` and `ct0` (and preferably `twid`). Follow the upstream documentation at https://github.com/nsozturk/tweetkit-x#getting-your-session-cookie . Never send the cookie in a chat, screenshot, issue, commit, or workflow input. Avoid installing an extension solely to extract cookies unless you trust and understand its access.

In your GitHub repository:

1. Open **Settings → Secrets and variables → Actions**.
2. Under **Secrets**, click **New repository secret**.
3. Name it exactly `TWEETKIT_COOKIE`.
4. Paste the cookie header into the secret value and save.

Only the posting step receives the secret. It is not written to a file or included in reports/history. Anyone who can change trusted workflow code could exfiltrate repository secrets; restrict write access to your repository.

TweetKit-X automates X's unofficial web endpoints. Its author warns this conflicts with X's terms and can cause account restrictions. X may reject GitHub runner IPs, require a login challenge, or expire the session. The workflow pauses on such errors rather than trying to defeat them. This setup is not guaranteed to remain unattended indefinitely.

## 5. Test a live batch, then enable scheduling

1. Run **Automatic X posting** again.
2. Leave **Initialize history** unchecked.
3. Check **Send real X posts**. This will attempt up to five public posts.
4. Check the run report and your X profile.
5. When that succeeds, go to **Settings → Secrets and variables → Actions → Variables**.
6. Create a repository variable named `AUTOPOST_ENABLED` with value `true`.

Scheduled runs now collect and post automatically, without per-post approval. No API key or developer app is used. Content is a shortened source headline plus link, not independently fact-checked reporting. Do not enable this if you require every story's factual claims to be independently verified.

To stop future scheduled posts, set `AUTOPOST_ENABLED` to `false` or disable the workflow under **Actions → Automatic X posting → … → Disable workflow**. Cancel any running job as well; changing a variable does not stop a job already running. A manual live run still posts even when scheduled automation is disabled.

## If posting pauses

The run's `posting-report` artifact and logs list any `uncertain_story_ids`. Check your X profile before taking action. The reservation means X might have accepted a post even if the job did not receive confirmation.

Run the workflow manually with live and initialization off. Fill in the uncertain story ID, then either the confirmed numeric X post ID or check **I checked X and this attempt was not posted**. This reconciles history without sending anything. A confirmed not-posted story is still not retried automatically. Resolve each uncertain ID before scheduling can resume. Replace the cookie secret if the session expired; changing the secret alone does not clear an uncertain attempt.

If a job is killed before writing its report, inspect the latest `autopost-state/state.db` with SQLite, or ask for help recovering the uncertain IDs. Do not paste the cookie into logs while troubleshooting.
