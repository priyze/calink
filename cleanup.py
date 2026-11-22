#!/usr/bin/env python3
"""
cleanup.py
delete the Google Calendar events that calink.py created (and only those),
then reset seen.db so the next run starts fresh.

usage:
    python cleanup.py --dry-run    # list what would be deleted
    python cleanup.py              # list, ask for confirmation, then delete
"""

import argparse
import sqlite3
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calink import DB_FILE, call, get_credentials


def find_script_events(cal):
    """Events whose description matches what calink.py writes."""
    found, token = [], None
    while True:
        resp = call(
            cal.events().list(calendarId="primary", q="Detected from:", maxResults=250, pageToken=token)
        )
        for e in resp.get("items", []):
            desc = e.get("description", "")
            if desc.startswith("Email subject:") and "Detected from:" in desc and "mail.google.com" in desc:
                found.append(e)
        token = resp.get("nextPageToken")
        if not token:
            break
    return found


def when(e):
    s = e.get("start", {})
    return s.get("dateTime") or s.get("date") or "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only list the events")
    args = ap.parse_args()

    cal = build("calendar", "v3", credentials=get_credentials())
    events = find_script_events(cal)

    if not events:
        print("No events created by calink.py were found.")
        return

    print(f"Found {len(events)} events created by calink.py:")
    for e in events:
        print(f"- [{when(e)}] {e.get('summary', '(no title)')}")

    if args.dry_run:
        print("Dry run, nothing deleted.")
        return

    if input(f"\nDelete these {len(events)} events? Type yes to confirm: ").strip().lower() != "yes":
        print("Cancelled.")
        return

    deleted = 0
    for e in events:
        try:
            call(cal.events().delete(calendarId="primary", eventId=e["id"]))
            deleted += 1
        except HttpError as err:
            if err.resp.status not in (404, 410):  # already gone is fine
                raise
        time.sleep(0.1)
    print(f"Deleted {deleted} events.")

    if DB_FILE.exists():
        con = sqlite3.connect(DB_FILE)
        for table in ("events", "deadlines", "scanned"):
            con.execute(f"DELETE FROM {table}")
        con.commit()
        print("Reset seen.db, so the next run of calink.py starts fresh.")


if __name__ == "__main__":
    main()
