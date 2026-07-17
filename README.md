# Copenhagen City Hall wedding-slot watcher

Watches the **Copenhagen City Hall (Rådhuset)** wedding booking calendar and
**notifies you the moment a new appointment slot appears** — so you can grab one
before it's gone.

It runs entirely on **GitHub Actions** (free, no server of your own). By default it
notifies you by **opening a GitHub issue**, which GitHub then emails to you (and
pushes to the GitHub mobile app) — **no accounts, apps, or secrets to set up**.
Prefer an instant native phone push? [Add an ntfy topic](#optional-instant-phone-push-via-ntfy) too.

By default it watches the **"open to all couples"** calendar
(*Vielsestider åbne for alle brudepar*). To watch the Copenhagen-residents calendar
instead, see [Tuning](#tuning-what-it-watches).

## How it works

The booking system (FrontDeskSuite at `reservation.frontdesksuite.com`) is a
multi-step wizard: the time-selection page only works once you've walked through
the earlier steps in the same session — a bare request fails with
`FlowStateIsMissing`. So `watcher.py`:

1. Starts a session and walks the flow (booking home → calendar page →
   `StartReservation`) with a cookie jar, which loads the real availability page.
2. Parses that page's date headers + time links into concrete slots, e.g.
   `Tuesday October 13, 2026 9:20 a.m.`
3. Compares them against the last run (stored in `state.json`, which the Action
   commits back to the repo).
4. When a **new** slot appears, it opens a GitHub issue titled e.g.
   *"Copenhagen City Hall: 2 new wedding slot(s)!"* and @mentions you.

A GitHub Actions job checks **every ~2.5 minutes by default**: each run loops
internally for ~50 minutes and then relaunches itself, so the cadence doesn't
depend on GitHub's best-effort cron (which stays in place only as a backstop to
revive the chain). Tune the interval with the `CHECK_SECONDS` repo variable
(e.g. `60` for one check per minute) — no workflow edit needed. The first run for a URL just records a baseline —
it won't spam you with every currently-open slot.

## Setup — the zero-config default

Nothing to configure. The watcher uses the Action's built-in token to open the
issue, and @mentions the repo owner so you get the email even with default watch
settings.

To make sure the alerts reach you:
- Confirm your GitHub **email notifications** are on:
  [Settings → Notifications](https://github.com/settings/notifications) → *Email*.
- (Optional) Install the **GitHub mobile app** and enable push for a buzz on your
  phone.

### Run / test it
**Actions → Watch wedding slots → Run workflow** runs it once. Tick
**test_notification** to fire a one-off test alert and confirm notifications reach
you. A normal run records the current availability as a baseline (no alert), then
the self-relaunching loop takes over (every `CHECK_SECONDS` seconds, default 150).

## Optional: instant phone push via ntfy

Want a native push instead of / in addition to the email? Add [ntfy](https://ntfy.sh)
(free, no account):

1. Install **ntfy** ([App Store](https://apps.apple.com/app/ntfy/id1625396347) ·
   [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Subscribe to a hard-to-guess topic name, e.g. `cph-wedding-a7f3k9`.
3. Add it as a repo secret: **Settings → Secrets and variables → Actions → New
   repository secret**, name `NTFY_TOPIC`, value = your topic.
4. Test it: **Actions → Run workflow → tick `test_notification`**.

When `NTFY_TOPIC` is set, alerts go to **both** ntfy and a GitHub issue.

## Tuning what it watches

Set these as **repo variables** (Settings → Secrets and variables → Actions →
**Variables**) — no code change needed:

| Variable     | Purpose |
|--------------|---------|
| `WATCH_URLS` | Which booking flow to watch (comma-separated). See the two calendars below. |
| `SLOT_REGEX` | Fallback slot pattern for non-FrontDeskSuite pages (unused for Copenhagen). |
| `CHECK_SECONDS` | Seconds between checks inside the watch loop (default `150`). |

The two Copenhagen City Hall calendars (both are `StartReservation` URLs the watcher
knows how to walk):

- **Open to all couples** (default):
  ```
  https://reservation.frontdesksuite.com/kkvielse/raadhuset/ReserveTime/StartReservation?pageId=c819aa7d-575b-4633-b7c0-a1d425b72390&buttonId=d77d235b-8f65-44e5-bccd-fc93e4edddc8&culture=en&uiCulture=en
  ```
- **Reserved for Copenhagen residents**:
  ```
  https://reservation.frontdesksuite.com/kkvielse/raadhuset/ReserveTime/StartReservation?pageId=3777e58e-1dc4-4ab1-8ee5-1200947805d5&buttonId=140a6acc-0a46-4cfe-805d-de10e077156a&culture=en&uiCulture=en
  ```

> The `pageId`/`buttonId` come from the booking site and may change if the
> municipality rebuilds its booking pages. If alerts stop, re-grab them: open the
> booking site, pick the calendar, and copy the "Vælg dato og tidspunkt" link, or
> run the workflow with the `DEBUG=1` repo variable to dump the page.

## Running locally

```bash
pip install -r requirements.txt
python watcher.py --selftest              # offline unit checks
python watcher.py                         # one real check (prints, no push)
TEST_NOTIFICATION=1 python watcher.py     # test the alert path
DEBUG=1 python watcher.py                 # dump page text/links for debugging
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
| `WATCH_URLS` | all-couples calendar | Comma-separated booking flow URL(s) |
| `SLOT_REGEX` | built-in heuristic | Fallback slot pattern (non-FrontDeskSuite) |
| `STATE_FILE` | `state.json` | Where last-seen slots are stored |
| `NOTIFY_ON_FIRST_RUN` | `false` | Alert on the very first run too |
| `TEST_NOTIFICATION` | `false` | Send one test alert and exit |
| `DEBUG` | `false` | Dump fetched page text/links to the log |

## License

MIT
