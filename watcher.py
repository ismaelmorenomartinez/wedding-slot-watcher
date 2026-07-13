#!/usr/bin/env python3
"""Copenhagen City Hall wedding-slot watcher.

Polls the Copenhagen City Hall (Rådhuset) wedding booking page on the
FrontDeskSuite reservation system, extracts the set of available appointment
slots, and alerts you whenever a slot appears that wasn't there last time. By
default the alert opens a GitHub issue (which GitHub emails to you -- zero
setup); set NTFY_TOPIC to also get an instant phone push via ntfy.sh.

State is kept in a small JSON file (STATE_FILE) so the watcher only alerts on
*newly* available slots rather than re-announcing everything on every run. The
GitHub Actions workflow commits that file back to the repo between runs.

Everything is configured through environment variables so no secrets live in the
code -- see README.md. The default points at the Copenhagen City Hall booking
page; override WATCH_URLS / SLOT_REGEX to watch a different service or tune matching.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import http.cookiejar
import smtplib
import urllib.request
import urllib.error
from urllib.parse import urlsplit, parse_qs
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # keeps the script importable for --selftest without deps
    BeautifulSoup = None


# --- Configuration (all overridable via env) --------------------------------

DEFAULT_URLS = [
    # Copenhagen City Hall (Rådhuset) wedding booking -- "open to all couples"
    # calendar. This is a FrontDeskSuite StartReservation URL: fetch() walks the
    # session flow (base -> calendar -> StartReservation) so the availability
    # page loads instead of erroring with FlowStateIsMissing.
    "https://reservation.frontdesksuite.com/kkvielse/raadhuset/ReserveTime/StartReservation?pageId=c819aa7d-575b-4633-b7c0-a1d425b72390&buttonId=d77d235b-8f65-44e5-bccd-fc93e4edddc8&culture=en&uiCulture=en",
]

# Danish weekday / month tokens help us recognise a rendered slot.
_DK_TOKENS = (
    r"mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag|"
    r"januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december"
)

# Default heuristic: a date (12/03/2026 or 2026-03-12 or "12. marts 2026") optionally
# followed by a time, or a bare "HH:MM" time. Tunable via SLOT_REGEX once you can
# inspect the booking widget's real markup.
DEFAULT_SLOT_REGEX = (
    r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}(?:\s*(?:kl\.?\s*)?\d{1,2}[:.]\d{2})?"
    r"|\d{4}-\d{2}-\d{2}(?:\s*\d{1,2}[:.]\d{2})?"
    r"|\d{1,2}\.\s*(?:" + _DK_TOKENS + r")\s*\d{2,4}"
    r"|kl\.?\s*\d{1,2}[:.]\d{2})"
)

# Phrases that mean "nothing available" -- if the page says this, slots = empty
# even if a stray date (e.g. today's date in a footer) matches the regex.
NO_SLOTS_PHRASES = [
    # Danish
    "ingen ledige tider",
    "ingen ledige tid",
    "ingen tider",
    "der er ingen ledige",
    "fuldt booket",
    "ikke muligt at booke",
    # English (FrontDeskSuite wording)
    "no available times",
    "there are no available",
    "no times available",
    "no available",
    "fully booked",
]

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)


def get_env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [u.strip() for u in raw.split(",") if u.strip()]


# --- Core ------------------------------------------------------------------


def _query_param(url: str, name: str) -> str | None:
    vals = parse_qs(urlsplit(url).query)
    got = vals.get(name) or vals.get(name.lower())
    return got[0] if got else None


def fetch(url: str, timeout: int = 30) -> str:
    """Fetch a page, following redirects and keeping cookies.

    FrontDeskSuite's booking steps need a session: hitting a StartReservation or
    TimeSelection URL cold fails with "FlowStateIsMissing". So when we detect one,
    we first warm the session (base page, then the calendar page) with a shared
    cookie jar, which lets StartReservation redirect into the availability page.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "da,en"}
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    if "/ReserveTime/" in url:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path.split('/ReserveTime/')[0]}/"
        page_id = _query_param(url, "pageId")
        warmups = [base]
        if page_id:
            warmups.append(f"{base}Home/Index?pageId={page_id}&culture=en&uiCulture=en")
        for w in warmups:
            try:
                opener.open(urllib.request.Request(w, headers=headers), timeout=timeout).read()
            except (urllib.error.URLError, TimeoutError):
                pass  # best-effort warm-up; the real fetch below reports failures

    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def visible_text(html: str) -> str:
    """Return the human-visible text of a page, scripts/styles stripped."""
    if BeautifulSoup is None:
        # Very rough fallback if bs4 is unavailable.
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))


# --- FrontDeskSuite availability parsing ------------------------------------
# The Copenhagen City Hall booking page renders availability as English date
# headers ("Tuesday October 13th, 2026") each followed by time links
# ("9:20 a.m."). We pair each time with the date heading that precedes it.

_FD_DATE = (
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2}\S*\s*,?\s*\d{4}"
)
_FD_TIME = r"\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?"
_FD_TOKEN = re.compile(f"(?P<date>{_FD_DATE})|(?P<time>{_FD_TIME})", re.IGNORECASE)


def _norm_date(d: str) -> str:
    # Drop the unicode superscript ordinal ("13th") and collapse whitespace.
    return re.sub(r"\s+", " ", d.encode("ascii", "ignore").decode()).replace(" ,", ",").strip()


def extract_frontdesk_slots(text: str) -> list[str]:
    slots, current = [], None
    for m in _FD_TOKEN.finditer(text):
        if m.group("date"):
            current = _norm_date(m.group("date"))
        elif current:
            t = re.sub(r"\s+", " ", m.group("time")).strip()
            slots.append(f"{current} {t}")
    return sorted(set(slots))


def extract_slots(html: str, slot_regex: str) -> list[str]:
    text = visible_text(html)
    low = text.lower()
    if any(p in low for p in NO_SLOTS_PHRASES):
        return []
    # Prefer the FrontDeskSuite date+time pairing when the page looks like it.
    fd = extract_frontdesk_slots(text)
    if fd:
        return fd
    matches = re.findall(slot_regex, text, flags=re.IGNORECASE)
    # Normalise whitespace and de-duplicate while keeping deterministic order.
    slots = sorted({re.sub(r"\s+", " ", m).strip() for m in matches if m.strip()})
    return slots


def content_hash(html: str) -> str:
    return hashlib.sha256(visible_text(html).encode("utf-8")).hexdigest()[:16]


def push_ntfy(topic: str, server: str, title: str, message: str, click: str | None,
              priority: str = "high", tags: str = "wedding_ring,bell") -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title.encode("utf-8"),
        "Tags": tags,
        "Priority": priority,
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"[notify] pushed to ntfy {url}")
        return True
    except urllib.error.URLError as exc:  # don't let a notify failure crash the run
        print(f"[notify] FAILED to push to {url}: {exc}", file=sys.stderr)
        return False


def open_github_issue(title: str, message: str, click: str | None) -> bool:
    """Open an issue in the current repo -> GitHub emails/pushes you for free.

    Uses the Actions-provided GITHUB_TOKEN + GITHUB_REPOSITORY, so no extra
    setup is needed. Returns False (and stays quiet) when not running in Actions.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return False
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    # @mention someone so GitHub emails/pushes them even if they only "watch"
    # participating activity (the default). Defaults to the repo owner.
    mention = os.environ.get("NOTIFY_MENTION", repo.split("/")[0]).lstrip("@").strip()
    body = message
    if click:
        body += f"\n\n[Open the booking page]({click})"
    if mention:
        body += f"\n\ncc @{mention}"
    body += "\n\n_Filed automatically by the wedding-slot watcher._"
    payload = json.dumps({"title": title, "body": body, "labels": ["new-slot"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wedding-slot-watcher",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"[notify] opened GitHub issue #{data.get('number')}: {data.get('html_url')}")
        return True
    except urllib.error.URLError as exc:
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            detail = f" ({exc.code}: {exc.read().decode('utf-8', 'replace')[:200]})"
        print(f"[notify] FAILED to open GitHub issue{detail}: {exc}", file=sys.stderr)
        return False


def _gh_env() -> tuple[str, str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    return token, repo, api


def _gh_post(url: str, payload: dict) -> dict:
    token, _, _ = _gh_env()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wedding-slot-watcher",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def github_status_ping(meta: dict, title: str, message: str) -> bool:
    """Post a heartbeat as a comment on a single rolling status issue.

    Keeps the "still watching, nothing new" pings out of the main issue list --
    real new-slot alerts still open their own issues via open_github_issue().
    """
    token, repo, api = _gh_env()
    if not token or not repo:
        return False
    mention = os.environ.get("NOTIFY_MENTION", repo.split("/")[0]).lstrip("@").strip()
    body = message + (f"\n\ncc @{mention}" if mention else "")
    num = meta.get("status_issue")
    if num:
        try:
            _gh_post(f"{api}/repos/{repo}/issues/{num}/comments", {"body": body})
            print(f"[heartbeat] commented on status issue #{num}")
            return True
        except urllib.error.URLError as exc:
            print(f"[heartbeat] status issue #{num} unavailable ({exc}); opening a new one", file=sys.stderr)
            meta.pop("status_issue", None)
    try:
        data = _gh_post(
            f"{api}/repos/{repo}/issues",
            {"title": title, "body": body, "labels": ["watcher-status"]},
        )
        meta["status_issue"] = data.get("number")
        print(f"[heartbeat] opened status issue #{data.get('number')}")
        return True
    except urllib.error.URLError as exc:
        print(f"[heartbeat] FAILED to open status issue: {exc}", file=sys.stderr)
        return False


def send_heartbeat(topic: str, server: str, meta: dict, summary: str, click: str | None) -> None:
    """Reassure you the watcher is alive when there is nothing new to report."""
    title = "🟢 Wedding watcher: still watching, no new slots"
    msg = f"No new Copenhagen City Hall wedding slots since the last alert. Currently {summary}."
    sent = False
    if topic:
        sent = push_ntfy(topic, server, title, msg, click,
                         priority="low", tags="hourglass_flowing_sand") or sent
    sent = github_status_ping(meta, title, msg) or sent
    sent = send_email_smtp(title, msg) or sent
    if not sent:
        print(f"[heartbeat] (no channel) {title}: {msg}")


def send_email_smtp(subject: str, body: str) -> bool:
    """Email an alert directly via SMTP (e.g. Gmail). No-op unless configured.

    Reads SMTP_USER / SMTP_PASS (a Gmail App Password) and optionally SMTP_HOST,
    SMTP_PORT, EMAIL_FROM, NOTIFY_EMAIL (recipient; defaults to SMTP_USER).
    """
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not user or not password:
        return False
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    to_addr = os.environ.get("NOTIFY_EMAIL", "").strip() or user
    msg = EmailMessage()
    msg["From"] = os.environ.get("EMAIL_FROM", "").strip() or user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"[notify] emailed {to_addr}")
        return True
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[notify] FAILED to email {to_addr}: {exc}", file=sys.stderr)
        return False


def notify(topic: str, title: str, message: str, click: str | None, server: str) -> None:
    """Send an alert through whichever channel is configured.

    Priority: ntfy (if NTFY_TOPIC set) -> GitHub issue (if running in Actions)
    -> print (local dry-run). Both channels fire if both are available.
    """
    sent = False
    if topic:
        sent = push_ntfy(topic, server, title, message, click) or sent
    sent = open_github_issue(title, message, click) or sent
    email_body = message + (f"\n\nOpen the booking page: {click}" if click else "")
    sent = send_email_smtp(title, email_body) or sent
    if not sent:
        print("[notify] no channel configured -- would have sent:")
        print(f"         {title}: {message}")


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def run() -> int:
    urls = get_env_list("WATCH_URLS", DEFAULT_URLS)
    slot_regex = os.environ.get("SLOT_REGEX", DEFAULT_SLOT_REGEX)
    state_file = os.environ.get("STATE_FILE", "state.json")
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    ntfy_server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    notify_first_run = os.environ.get("NOTIFY_ON_FIRST_RUN", "").lower() in ("1", "true", "yes")
    heartbeat_hours = float(os.environ.get("HEARTBEAT_HOURS", "3") or 0)

    # Manual smoke test: fire one push so you can confirm your phone is set up.
    if os.environ.get("TEST_NOTIFICATION", "").lower() in ("1", "true", "yes"):
        notify(
            topic=ntfy_topic,
            title="Copenhagen wedding watcher: test 🔔",
            message="If you can read this on your phone, notifications work.",
            click=urls[0] if urls else None,
            server=ntfy_server,
        )
        return 0

    state = load_state(state_file)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    any_new = False

    for url in urls:
        prev = state.get(url, {})
        try:
            html = fetch(url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"[check] {url} -> fetch error: {exc}", file=sys.stderr)
            state.setdefault(url, {})["last_error"] = str(exc)
            state[url]["last_checked"] = now
            continue

        vtext = visible_text(html)
        matched_phrase = next((p for p in NO_SLOTS_PHRASES if p in vtext.lower()), None)
        slots = extract_slots(html, slot_regex)
        chash = content_hash(html)
        prev_slots = set(prev.get("slots", []))
        first_run = "slots" not in prev

        new_slots = sorted(set(slots) - prev_slots)
        changed = chash != prev.get("hash")

        print(
            f"[check] {url}\n"
            f"        slots={len(slots)} new={len(new_slots)} "
            f"changed={changed} first_run={first_run} "
            f"text_len={len(vtext)} no_slots_phrase={matched_phrase!r}"
        )
        debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
        if debug:  # set DEBUG=1 to dump page text/links for troubleshooting
            print("[debug] first 2000 chars of visible text:")
            print(vtext[:2000])
            print(f"[debug] first 40 regex matches: {re.findall(slot_regex, vtext, re.IGNORECASE)[:40]}")
            if BeautifulSoup is not None:
                soup = BeautifulSoup(html, "html.parser")
                for f in soup.find_all("form"):
                    names = [i.get("name") or i.get("id") for i in f.find_all(("input", "select", "button"))]
                    print(f"[debug] FORM action={f.get('action')!r} method={f.get('method')!r} fields={names}")
                for a in soup.find_all("a", href=True)[:40]:
                    txt = re.sub(r"\s+", " ", a.get_text(" ")).strip()
                    print(f"[debug] LINK {a['href']!r} -> {txt[:60]!r}")
                for b in soup.find_all("button")[:20]:
                    btext = re.sub(r"\s+", " ", b.get_text(" ")).strip()[:60]
                    print(f"[debug] BUTTON {b.get('name')!r}/{b.get('value')!r} -> {btext!r}")

        state[url] = {
            "slots": slots,
            "hash": chash,
            "last_checked": now,
            "last_new": new_slots or prev.get("last_new", []),
        }

        should_notify = bool(new_slots) and (not first_run or notify_first_run)
        if should_notify:
            any_new = True
            preview = "\n".join(f"• {s}" for s in new_slots[:15])
            if len(new_slots) > 15:
                preview += f"\n… and {len(new_slots) - 15} more"
            notify(
                topic=ntfy_topic,
                title=f"Copenhagen City Hall: {len(new_slots)} new wedding slot(s)!",
                message=f"{preview}\n\nOpen the booking page to grab one.",
                click=url,
                server=ntfy_server,
            )
        elif first_run:
            print(f"        baseline recorded ({len(slots)} slots) -- no alert on first run")

    # Heartbeat: if nothing new for a while, send a periodic "still watching" ping
    # so silence never looks like a broken watcher. A real alert resets the timer.
    if heartbeat_hours > 0 and urls:
        meta = state.setdefault("__meta__", {})
        all_slots = [s for u in urls for s in state.get(u, {}).get("slots", [])]
        days = len({sl.rsplit(" ", 2)[0] for sl in all_slots})
        summary = f"{len(all_slots)} open slot(s) across {days} day(s)"
        if any_new:
            meta["last_heartbeat"] = now  # a real alert already told you it's alive
        else:
            last = meta.get("last_heartbeat")
            due = last is None or (
                datetime.fromisoformat(now) - datetime.fromisoformat(last)
                >= timedelta(hours=heartbeat_hours)
            )
            if due:
                send_heartbeat(ntfy_topic, ntfy_server, meta, summary, urls[0])
                meta["last_heartbeat"] = now

    save_state(state_file, state)
    print(f"[done] state written to {state_file}; new_slots_found={any_new}")
    return 0


# --- Self test (no network) -------------------------------------------------


def selftest() -> int:
    sample_before = """
    <html><body>
      <h1>Vielse i Kalundborg Kommune</h1>
      <div class="slots">
        <p>Ledige tider:</p>
        <ul><li>12/03/2026 kl. 10:00</li><li>19/03/2026 kl. 11:30</li></ul>
      </div>
    </body></html>
    """
    sample_after = sample_before.replace(
        "<li>19/03/2026 kl. 11:30</li>",
        "<li>19/03/2026 kl. 11:30</li><li>26/03/2026 kl. 09:00</li>",
    )
    sample_none = "<html><body><p>Der er ingen ledige tider i øjeblikket.</p></body></html>"

    before = extract_slots(sample_before, DEFAULT_SLOT_REGEX)
    after = extract_slots(sample_after, DEFAULT_SLOT_REGEX)
    none = extract_slots(sample_none, DEFAULT_SLOT_REGEX)

    assert "12/03/2026 kl. 10:00" in before, before
    assert len(before) == 2, before
    new = sorted(set(after) - set(before))
    assert new == ["26/03/2026 kl. 09:00"], new
    assert none == [], none
    assert content_hash(sample_before) != content_hash(sample_after)

    fd = extract_frontdesk_slots(
        "Tuesday October 13ᵗʰ, 2026 9:20 a.m. 3:30 p.m. "
        "Wednesday October 14ᵗʰ, 2026 2:40 p.m."
    )
    assert fd == [
        "Tuesday October 13, 2026 3:30 p.m.",
        "Tuesday October 13, 2026 9:20 a.m.",
        "Wednesday October 14, 2026 2:40 p.m.",
    ], fd

    print("selftest OK:", {"before": before, "new": new, "none": none, "frontdesk": len(fd)})
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run())
