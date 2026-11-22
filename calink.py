#!/usr/bin/env python3
"""
deadline_sync.py
Scan recent Gmail messages for submission deadlines and add them to Google Calendar.

Usage:
    python deadline_sync.py --dry-run          # just print what it would add
    python deadline_sync.py                    # actually create calendar events
    python deadline_sync.py --days 60          # look back 60 days instead of 30
    python deadline_sync.py --query "from:prof@college.edu"   # extra Gmail search filter
"""

import argparse
import base64
import datetime as dt
import html
import re
import sqlite3
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ----------------------------- settings -----------------------------------

TZ_NAME = "Asia/Kolkata"
TZ = ZoneInfo(TZ_NAME)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"  # downloaded from Google Cloud
TOKEN_FILE = BASE_DIR / "token.json"              # created on first login
DB_FILE = BASE_DIR / "seen.db"

# A sentence must contain one of these to be treated as a deadline sentence.
DEADLINE_WORDS = re.compile(
    r"\b(submit|submission|submitted|deadline|due|last date|last day|"
    r"on or before|before|no later than|closes|closing|hand in|turn in|"
    r"register by|apply by|cut-?off)\b",
    re.IGNORECASE,
)

# Detects whether the matched date text includes an explicit time.
HAS_TIME = re.compile(
    r"\d{1,2}:\d{2}|\b\d{1,2}\s?(am|pm)\b|\bnoon\b|\bmidnight\b|\bEOD\b",
    re.IGNORECASE,
)

# A date match must contain a digit, month, weekday or a relative word,
# otherwise dateparser sometimes treats words like "may" or "second" as dates.
LOOKS_LIKE_DATE = re.compile(
    r"\d|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
    r"mon|tue|wed|thu|fri|sat|sun|today|tomorrow|tonight|next|this",
    re.IGNORECASE,
)

# Emails confirming a submission that already happened are skipped entirely.
# Checked against the subject and the first part of the body.
CONFIRMATION_RE = re.compile(
    r"(has|have|had)\s+been\s+(successfully\s+)?(submitted|received|recorded)|"
    r"successfully\s+(submitted|uploaded|turned\s+in|handed\s+in)|"
    r"(submitted|uploaded)\s+successfully|"
    r"submission\s+(was\s+|is\s+)?(received|successful|confirmed|confirmation|receipt)|"
    r"(confirmation|receipt|acknowledg(e)?ment)\s+(of|for)\s+(your\s+)?(assignment\s+)?submission|"
    r"(we|i)\s*(have|'ve|\u2019ve)?\s+received\s+your\s+(assignment|submission|work|response|form|application)|"
    r"thank\s+you\s+for\s+(submitting|your\s+submission)",
    re.IGNORECASE,
)
CONFIRMATION_BODY_CHARS = 500  # confirmations say so up front

# Duplicate detection: two deadlines on the same day are treated as the same
# item if their content words overlap at least this much (0 to 1).
DUPLICATE_THRESHOLD = 0.5
STOPWORDS = set(
    """a an the and or of to in on at by for from with as is are be was were will must should may can
    please kindly reminder re fwd fw fyi urgent final dear all students student your you we our this
    that it before after no later than last date day due deadline deadlines submit submission
    submissions submitted closes closing cutoff hand turn new update updated note am pm st nd rd th""".split()
)

# Gmail-side pre-filter so we only download emails that mention deadline-ish words.
GMAIL_PREFILTER = (
    '(deadline OR submit OR submission OR due OR "last date" OR "last day" '
    'OR "on or before" OR "no later than" OR closes OR closing OR cutoff)'
)


def call(request, retries=6):
    """Run a Google API request; if we hit a rate limit, wait and retry."""
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            retryable = status in (429, 500, 502, 503, 504) or (
                status == 403 and "ratelimitexceeded" in str(e).lower()
            )
            if not retryable or attempt == retries - 1:
                raise
            wait = min(15 * (attempt + 1), 65)  # quota is per minute, so wait it out
            print(f"  rate limited, waiting {wait}s (retry {attempt + 1}/{retries - 1})...")
            time.sleep(wait)


# ----------------------------- google auth --------------------------------


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


# ----------------------------- email parsing ------------------------------


def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


def extract_body(payload) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    plain, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(decode_part(data))
        elif data and mime == "text/html":
            html_parts.append(strip_html(decode_part(data)))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain) if plain else "\n".join(html_parts)


def header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def split_sentences(text: str):
    text = re.sub(r"[ \t]+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


# ----------------------------- deadline extraction ------------------------


MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# a full date with a year, like "4 november 2026" or "nov 4, 2026" (optionally followed by a time).
# dates like this are unambiguous, so we trust them instead of guessing with dateparser.
EXPLICIT_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTHS}\.?,?\s+\d{{4}}"
    rf"|{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})"
    rf"(?:[,\s]+(?:at\s+)?(?:\d{{1,2}}:\d{{2}}\s*(?:am|pm)?|\d{{1,2}}\s*(?:am|pm)\b))?",
    re.IGNORECASE,
)


def clean_sentence(s: str) -> str:
    """Strip emoji, asterisks and fancy punctuation that confuse the date parser."""
    for a, b in (("\u2013", "-"), ("\u2014", "-"), ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        s = s.replace(a, b)
    s = re.sub(r"[^\x00-\x7f]+", " ", s)
    s = s.replace("*", " ")
    return re.sub(r"\s+", " ", s).strip()


def incomplete_date(text: str) -> bool:
    """True for things like 'november 2026' that have a month but no day.
    dateparser fills the missing day with the email's send day, which gives wrong dates."""
    if not re.search(rf"\b{MONTHS}\b", text, re.IGNORECASE):
        return False
    no_times = re.sub(r"\d{1,2}:\d{2}(\s*[ap]m)?|\d{1,2}\s*[ap]m", "", text, flags=re.IGNORECASE)
    return not re.search(r"(?<!\d)\d{1,2}(?!\d)", no_times)


def weekday_conflict(sentence: str, text: str, when: dt.datetime) -> bool:
    """True if a weekday written next to the date doesn't match the parsed date."""
    i = sentence.find(text)
    if i < 0:
        return False
    window = sentence[max(0, i - 12): i + len(text) + 15].lower()
    present = {n for n, name in enumerate(WEEKDAYS) if re.search(rf"\b{name}\b", window)}
    return bool(present) and when.weekday() not in present


MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

# optional time right after a date, like ", 11:59 pm" or " at 5pm"
TIME_AFTER = r"(?:[,\s]+(?:at|by|before)?\s*(?:\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)\b))?"
TRAILING_TIME = re.compile(r"(?:(\d{1,2}):(\d{2})\s*(am|pm)?|(\d{1,2})\s*(am|pm))\s*$", re.IGNORECASE)

# a date without a year, like "4 nov" or "nov 4th"
YEARLESS_DATE = re.compile(
    rf"\b(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{MONTHS})\b\.?"
    rf"|(?P<m2>{MONTHS})\b\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?![\d:])){TIME_AFTER}",
    re.IGNORECASE,
)

# relative phrases: "in one day", "within 3 days", "tomorrow", "friday" ...
# these are worked out from the date the email was SENT, never from today.
RELATIVE_DATE = re.compile(
    r"\b(?:(?:within|in)\s+(?P<n>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?P<unit>hour|hr|day|week)s?"
    r"|(?P<day_after>day\s+after\s+tomorrow)"
    r"|(?P<word>tomorrow|today|tonight)"
    r"|(?:(?:this|next|coming|upcoming)\s+)?(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    + TIME_AFTER,
    re.IGNORECASE,
)


VAGUE_RELATIVE = re.compile(
    r"\b(in|ago|from now|next|last|coming|week|weeks|day|days|hour|hours|month|months|"
    r"today|tomorrow|tonight|yesterday|now)\b",
    re.IGNORECASE,
)


def trailing_time(text: str):
    """Read a time at the end of text, like '11:59 pm' or '5pm'. Returns (hour, minute) or None."""
    t = TRAILING_TIME.search(text)
    if not t:
        return None
    if t.group(4):
        h, mi, ap = int(t.group(4)), 0, t.group(5)
    else:
        h, mi, ap = int(t.group(1)), int(t.group(2)), t.group(3)
    ap = (ap or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else None


def at_time(day: dt.date, tm):
    return dt.datetime.combine(day, dt.time(*(tm or (0, 0))), tzinfo=TZ)


def resolve_yearless(day: int, month: int, sent_local: dt.datetime):
    """Pick the year for a date written without one, using the email's send date."""
    try:
        d = dt.date(sent_local.year, month, day)
        if d < sent_local.date() - dt.timedelta(days=90):
            d = dt.date(sent_local.year + 1, month, day)  # e.g. a december email saying "5 jan"
        return d
    except ValueError:
        return None


def resolve_relative(m, sent_local: dt.datetime):
    """Turn a relative phrase into (when, all_day), counting from the send date."""
    g = m.groupdict()
    tm = trailing_time(m.group(0))
    if g["n"]:
        n = NUM_WORDS.get(g["n"].lower()) or int(g["n"])
        unit = g["unit"].lower()
        if unit in ("hour", "hr"):
            return sent_local + dt.timedelta(hours=n), False
        day = sent_local.date() + dt.timedelta(days=n * (7 if unit == "week" else 1))
    elif g["day_after"]:
        day = sent_local.date() + dt.timedelta(days=2)
    elif g["word"]:
        day = sent_local.date() + dt.timedelta(days=1 if g["word"].lower() == "tomorrow" else 0)
    else:
        idx = WEEKDAYS.index(g["weekday"].lower())
        day = sent_local.date() + dt.timedelta(days=(idx - sent_local.weekday()) % 7 or 7)
    return at_time(day, tm), tm is None


def find_deadlines(body: str, sent: dt.datetime, now: dt.datetime):
    """Return a list of dicts: {when, all_day, sentence, date_text}."""
    sent_local = sent.astimezone(TZ)
    settings = {
        "TIMEZONE": TZ_NAME,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",  # 03/04 means 3 April
        "RELATIVE_BASE": sent_local.replace(tzinfo=None),
    }
    found, seen = [], set()

    def add(text, when, sentence, all_day=None):
        if all_day is None:
            all_day = not HAS_TIME.search(text)
        when = when.astimezone(TZ)
        if (when.date() < now.date()) if all_day else (when < now):
            return  # already passed
        key = (when.date() if all_day else when, sentence[:60])
        if key in seen:
            return
        seen.add(key)
        found.append({"when": when, "all_day": all_day, "sentence": sentence, "date_text": text})

    for raw in split_sentences(body):
        sentence = clean_sentence(raw)
        if len(sentence) > 400 or not DEADLINE_WORDS.search(sentence):
            continue

        # 1. full dates with a year are unambiguous, so use those and skip the guessing
        explicit = list(EXPLICIT_DATE.finditer(sentence))
        if explicit:
            for m in explicit:
                when = dateparser.parse(m.group(0), settings=settings)
                if when:
                    add(m.group(0), when, sentence)
            continue

        # 2. dates without a year ("4 nov"): the year comes from the email's send date.
        #    a weekday written next to it ("(wednesday)") is just a label, so we stop here.
        yearless = list(YEARLESS_DATE.finditer(sentence))
        if yearless:
            for m in yearless:
                day = int(m.group("d1") or m.group("d2"))
                month = MONTH_NUM[(m.group("m1") or m.group("m2"))[:3].lower()]
                d = resolve_yearless(day, month, sent_local)
                if d:
                    tm = trailing_time(m.group(0))
                    add(m.group(0), at_time(d, tm), sentence, all_day=tm is None)
            continue

        # 3. relative phrases, counted from the date the email was sent
        rest = sentence
        for m in RELATIVE_DATE.finditer(sentence):
            when, all_day = resolve_relative(m, sent_local)
            add(m.group(0), when, sentence, all_day=all_day)
            rest = rest.replace(m.group(0), " ")  # keep dateparser from re-reading it against today

        # 4. anything left (like 28/11/2026): let dateparser have a go
        try:
            matches = search_dates(rest, settings=settings) or []
        except Exception:
            continue
        for text, when in matches:
            if len(text) < 3 or not LOOKS_LIKE_DATE.search(text):
                continue
            if VAGUE_RELATIVE.search(text):
                continue  # things like "in a few days" or "next month" can't be pinned to a date
            if incomplete_date(text) or weekday_conflict(rest, text, when.astimezone(TZ)):
                continue
            add(text, when, sentence)
    return found


def is_confirmation(subject: str, body: str) -> bool:
    return bool(CONFIRMATION_RE.search(subject) or CONFIRMATION_RE.search(body[:CONFIRMATION_BODY_CHARS]))


def clean_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", subject, flags=re.IGNORECASE)


def content_tokens(*texts):
    """Meaningful words (and numbers like the 3 in 'Assignment 3') used to compare deadlines."""
    words = re.findall(r"[a-z0-9]+", " ".join(texts).lower())
    return {w for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit())}


def find_duplicate(d, msg_id, thread_id, tokens, known):
    """Return an already-known deadline that is the same item as d, or None."""
    day = d["when"].date().isoformat()
    for k in known:
        if k["day"] != day:
            continue
        if thread_id and k["thread_id"] == thread_id and k["msg_id"] != msg_id:
            return k  # same email thread, same day
        shared = tokens & k["tokens"]
        union = tokens | k["tokens"]
        if len(shared) >= 2 and len(shared) / len(union) >= DUPLICATE_THRESHOLD:
            return k
    return None


# ----------------------------- database -----------------------------------


def open_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
               msg_id TEXT, due TEXT, sentence TEXT, event_id TEXT,
               PRIMARY KEY (msg_id, due, sentence))"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS scanned (msg_id TEXT PRIMARY KEY)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS deadlines (
               event_id TEXT, day TEXT, thread_id TEXT, msg_id TEXT, tokens TEXT, title TEXT)"""
    )
    return con


def load_known(con):
    known = []
    rows = con.execute("SELECT event_id, day, thread_id, msg_id, tokens, title FROM deadlines")
    for event_id, day, thread_id, msg_id, tokens, title in rows:
        known.append(
            {"event_id": event_id, "day": day, "thread_id": thread_id, "msg_id": msg_id,
             "tokens": set(tokens.split()), "title": title}
        )
    return known


# ----------------------------- calendar -----------------------------------


def build_event(subject, msg_id, d):
    when = d["when"]
    title = f"Due: {d['sentence'][:80]}"
    description = (
        f"Email subject: {subject}\n"
        f"Email: https://mail.google.com/mail/u/0/#all/{msg_id}\n\n"
        f"Detected from: {d['sentence']}"
    )
    if d["all_day"]:
        day = when.date()
        event = {
            "start": {"date": day.isoformat()},
            "end": {"date": (day + dt.timedelta(days=1)).isoformat()},
            # minutes before 00:00 of the day: 900 = 9 AM the day before
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 900}]},
        }
    else:
        event = {
            "start": {"dateTime": when.isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": (when + dt.timedelta(minutes=30)).isoformat(), "timeZone": TZ_NAME},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 1440},
                    {"method": "popup", "minutes": 120},
                ],
            },
        }
    event.update({"summary": title, "description": description})
    return event


# ----------------------------- main ---------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="how many days back to scan")
    ap.add_argument("--query", default="", help="extra Gmail search filter")
    ap.add_argument("--all", action="store_true", help="scan every email (skip the keyword pre-filter)")
    ap.add_argument("--dry-run", action="store_true", help="print only, don't create events")
    args = ap.parse_args()

    creds = get_credentials()
    gmail = build("gmail", "v1", credentials=creds)
    cal = build("calendar", "v3", credentials=creds)
    con = open_db()
    now = dt.datetime.now(TZ)
    known = load_known(con)

    prefilter = "" if args.all else GMAIL_PREFILTER
    q = f"newer_than:{args.days}d {prefilter} {args.query}".strip()
    print(f"Searching Gmail: {q}")

    msg_ids, token = [], None
    while True:
        resp = call(gmail.users().messages().list(userId="me", q=q, pageToken=token, maxResults=100))
        msg_ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    print(f"{len(msg_ids)} emails to scan")

    added = 0
    for i, msg_id in enumerate(msg_ids, 1):
        if con.execute("SELECT 1 FROM scanned WHERE msg_id=?", (msg_id,)).fetchone():
            continue
        if i % 25 == 0:
            print(f"  ...{i}/{len(msg_ids)}")
        msg = call(gmail.users().messages().get(userId="me", id=msg_id, format="full"))
        time.sleep(0.2)  # gentle pacing
        headers = msg["payload"].get("headers", [])
        subject = header(headers, "Subject") or "(no subject)"
        # when gmail received the email. this is the base for "tomorrow", "in one day" etc.
        try:
            sent = dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=dt.timezone.utc)
        except Exception:
            try:
                sent = parsedate_to_datetime(header(headers, "Date"))
                if sent.tzinfo is None:
                    sent = sent.replace(tzinfo=dt.timezone.utc)
            except Exception:
                sent = now
                print(f"  (couldn't read the send date of: {subject[:60]})")

        thread_id = msg.get("threadId", "")
        body = extract_body(msg["payload"])
        if is_confirmation(subject, body):
            print(f"- skipped confirmation email: {subject[:80]}")
            deadlines = []
        else:
            deadlines = find_deadlines(body, sent, now)

        for d in deadlines:
            due_key = d["when"].date().isoformat() if d["all_day"] else d["when"].isoformat()
            already = con.execute(
                "SELECT 1 FROM events WHERE msg_id=? AND due=? AND sentence=?",
                (msg_id, due_key, d["sentence"]),
            ).fetchone()
            if already:
                continue

            tokens = content_tokens(clean_subject(subject), d["sentence"].replace(d["date_text"], " "))
            dup = find_duplicate(d, msg_id, thread_id, tokens, known)
            if dup:
                print(f"- duplicate, skipped: {d['sentence'][:80]}\n    same as: {dup['title'][:80]}")
                if not args.dry_run:
                    con.execute(
                        "INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                        (msg_id, due_key, d["sentence"], dup["event_id"]),
                    )
                    con.commit()
                continue

            label = d["when"].strftime("%a %d %b %Y") + ("" if d["all_day"] else d["when"].strftime(" %H:%M"))
            print(f"- [{label}] {subject}\n    {d['sentence'][:120]}")
            record = {
                "event_id": None, "day": d["when"].date().isoformat(), "thread_id": thread_id,
                "msg_id": msg_id, "tokens": tokens, "title": f"Due: {d['sentence'][:80]}",
            }
            known.append(record)  # also makes dry runs catch duplicates within the same run
            if args.dry_run:
                continue

            created = call(cal.events().insert(calendarId="primary", body=build_event(subject, msg_id, d)))
            record["event_id"] = created["id"]
            con.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (msg_id, due_key, d["sentence"], created["id"]),
            )
            con.execute(
                "INSERT INTO deadlines VALUES (?,?,?,?,?,?)",
                (created["id"], record["day"], thread_id, msg_id, " ".join(sorted(tokens)), record["title"]),
            )
            con.commit()
            added += 1

        if not args.dry_run:
            con.execute("INSERT OR IGNORE INTO scanned VALUES (?)", (msg_id,))
            con.commit()

    print("Dry run, nothing created." if args.dry_run else f"Done. {added} new events added.")


if __name__ == "__main__":
    main()
