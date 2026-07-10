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
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # keeps the script importable for --selftest without deps
    BeautifulSoup = None


# --- Configuration (all overridable via env) --------------------------------

DEFAULT_URLS = [
    # PROBE 2: the two calendar landing pages (all couples / Copenhagen residents)
    # to find the "reserve time" step that leads into the availability flow.
    "https://reservation.frontdesksuite.com/kkvielse/raadhuset/Home/Index?pageId=c819aa7d-575b-4633-b7c0-a1d425b72390&culture=en&uiCulture=en",
    "https://reservation.frontdesksuite.com/kkvielse/raadhuset/Home/Index?pageId=3777e58e-1dc4-4ab1-8ee5-1200947805d5&culture=en&uiCulture=en",
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


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "da,en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def extract_slots(html: str, slot_regex: str) -> list[str]:
    text = visible_text(html)
    low = text.lower()
    if any(p in low for p in NO_SLOTS_PHRASES):
        return []
    matches = re.findall(slot_regex, text, flags=re.IGNORECASE)
    # Normalise whitespace and de-duplicate while keeping deterministic order.
    slots = sorted({re.sub(r"\s+", " ", m).strip() for m in matches if m.strip()})
    return slots


def content_hash(html: str) -> str:
    return hashlib.sha256(visible_text(html).encode("utf-8")).hexdigest()[:16]


def push_ntfy(topic: str, server: str, title: str, message: str, click: str | None) -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title.encode("utf-8"),
        "Tags": "wedding_ring,bell",
        "Priority": "high",
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


def notify(topic: str, title: str, message: str, click: str | None, server: str) -> None:
    """Send an alert through whichever channel is configured.

    Priority: ntfy (if NTFY_TOPIC set) -> GitHub issue (if running in Actions)
    -> print (local dry-run). Both channels fire if both are available.
    """
    sent = False
    if topic:
        sent = push_ntfy(topic, server, title, message, click) or sent
    sent = open_github_issue(title, message, click) or sent
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
        if debug or first_run:  # probe the page once when first watching a URL
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
    print("selftest OK:", {"before": before, "new": new, "none": none})
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run())
