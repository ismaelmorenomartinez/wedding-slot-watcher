# Kalundborg wedding-slot watcher

Watches Kalundborg Kommune's wedding (**vielse**) booking page and **notifies you
the moment a new appointment slot appears** — so you can grab one before it's gone.

It runs entirely on **GitHub Actions** (free, no server of your own). By default it
notifies you by **opening a GitHub issue**, which GitHub then emails to you (and
pushes to the GitHub mobile app) — **no accounts, apps, or secrets to set up**.
Prefer an instant native phone push? [Add an ntfy topic](#optional-instant-phone-push-via-ntfy) too.

## How it works

1. A scheduled Action runs `watcher.py` every 30 minutes.
2. The script fetches the booking page(s), extracts the set of available slots,
   and compares it against the last run (stored in `state.json`, which the Action
   commits back to the repo).
3. When a **new** slot appears, it opens a GitHub issue titled e.g.
   *"Kalundborg: 2 new wedding slot(s)!"* and @mentions you. GitHub emails you and
   pushes to the GitHub mobile app.

The first run for each URL just records a baseline — it won't spam you with every
currently-open slot.

## Setup — the zero-config default

Nothing to configure. The watcher uses the Action's built-in token to open the
issue, and @mentions the repo owner so you get the email even with default watch
settings.

To make sure the alerts reach you:
- Confirm your GitHub **email notifications** are on:
  [Settings → Notifications](https://github.com/settings/notifications) → *Email*.
- (Optional) Install the **GitHub mobile app** and enable push for a buzz on your
  phone.

### Try it now
Go to **Actions → Watch Kalundborg wedding slots → Run workflow** and run it once.
On the first run it records a baseline (no alert). It then runs automatically every
30 minutes.

## Optional: instant phone push via ntfy

Want a native push instead of / in addition to the email? Add [ntfy](https://ntfy.sh)
(free, no account):

1. Install **ntfy** ([App Store](https://apps.apple.com/app/ntfy/id1625396347) ·
   [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Subscribe to a hard-to-guess topic name, e.g. `kalundborg-wedding-a7f3k9`.
3. Add it as a repo secret: **Settings → Secrets and variables → Actions → New
   repository secret**, name `NTFY_TOPIC`, value = your topic.
4. Test it: **Actions → Run workflow → tick `test_notification`**. Your phone should
   buzz within seconds.

When `NTFY_TOPIC` is set, alerts go to **both** ntfy and a GitHub issue.

## Tuning what it watches

The defaults point at Kalundborg's public wedding pages and use a general
date/time pattern. Once you open the real booking widget and can see how slots are
rendered, you can point the watcher precisely — no code change needed. Set these as
**repo variables** (Settings → Secrets and variables → Actions → **Variables**):

| Variable     | Purpose                                                        | Example |
|--------------|----------------------------------------------------------------|---------|
| `WATCH_URLS` | Comma-separated page(s) to check. Use the exact slot-picker URL if the times live on a separate self-service page. | `https://www.kalundborg.dk/.../vielse-i-kalundborg-kommune` |
| `SLOT_REGEX` | Custom regex for what a slot looks like, if the default over/under-matches. | `\d{2}\.\d{2}\.\d{4}\s+kl\.\s*\d{2}:\d{2}` |

> **Note:** if Kalundborg's slot picker is a JavaScript widget that loads times via
> a background API, point `WATCH_URLS` at that API endpoint (open the booking page,
> check your browser's Network tab) for the most reliable results. Server-side
> fetching can't run the page's JavaScript.

## Running locally

```bash
pip install -r requirements.txt
python watcher.py --selftest              # offline unit check
NTFY_TOPIC=your-topic python watcher.py   # one real check
TEST_NOTIFICATION=1 NTFY_TOPIC=your-topic python watcher.py  # test push
```

## Configuration reference

All settings are environment variables:

| Env | Default | Meaning |
|-----|---------|---------|
| `GITHUB_TOKEN` | *(auto in Actions)* | Token used to open the alert issue |
| `GITHUB_REPOSITORY` | *(auto in Actions)* | `owner/repo` the issue is opened in |
| `NOTIFY_MENTION` | repo owner | GitHub username to @mention in the issue |
| `NTFY_TOPIC` | *(unset → GitHub issue only)* | ntfy topic for an extra phone push |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server (change for self-hosted) |
| `WATCH_URLS` | Kalundborg vielse page | Comma-separated URLs to poll |
| `SLOT_REGEX` | built-in date/time heuristic | Regex identifying a slot |
| `STATE_FILE` | `state.json` | Where last-seen slots are stored |
| `NOTIFY_ON_FIRST_RUN` | `false` | Alert on the very first run too |
| `TEST_NOTIFICATION` | `false` | Send one test push and exit |

## License

MIT
