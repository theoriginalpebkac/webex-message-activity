#!/usr/bin/env python3
"""
Webex activity report — a month-end timecard memory aid.

Reads the day files produced by fetcher.py (data/YYYY-MM-DD.jsonl) and renders a
plain-text summary of the days you posted, grouped:

    Week (Sun-Sat)  ->  Day  ->  Space/person  ->  Session(s)

A "session" is a burst of your messages in one space, split when the gap between
two of your consecutive messages exceeds session_gap_minutes. Each session shows
its time span, your messages (each capped at message_char_limit), the up-to-2
context messages you were replying to (dimmed), and a fillable Hours line.

This script NEVER touches the Webex API — it only reads data/. The shaping step
(build_weeks) is deliberately separate from rendering (render_text) so an HTML
renderer can be added later over the same shaped data.
"""

import os
import sys
import glob
import json
import argparse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.yaml")
DATA_DIR = os.path.join(HERE, "data")
UTC = ZoneInfo("UTC")

CONTEXT_CHAR_LIMIT = 100  # dimmed context lines are just for orientation


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        sys.exit(f"Error: {SETTINGS_FILE} not found (copy settings.yaml.example).")
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        s = yaml.safe_load(f) or {}
    for key in ("email", "timezone"):
        if not s.get(key):
            sys.exit(f"Error: '{key}' missing from {SETTINGS_FILE}.")
    try:
        ZoneInfo(s["timezone"])
    except Exception:
        sys.exit(f"Error: '{s['timezone']}' is not a valid IANA timezone.")
    s.setdefault("message_char_limit", 500)
    s.setdefault("session_gap_minutes", 90)
    return s


# --------------------------------------------------------------------------- #
# Loading records
# --------------------------------------------------------------------------- #

def parse_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def day_files_for(period: str) -> list:
    """period is YYYY-MM (whole month) or YYYY-MM-DD (single day)."""
    if len(period) == 7:      # YYYY-MM
        pattern = os.path.join(DATA_DIR, f"{period}-*.jsonl")
    else:                     # YYYY-MM-DD
        pattern = os.path.join(DATA_DIR, f"{period}.jsonl")
    return sorted(glob.glob(pattern))


def load_records(files: list, tz: ZoneInfo) -> list:
    """Load all records, attaching a local datetime to each."""
    records = []
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                r["_local"] = parse_utc(r["created"]).astimezone(tz)
                records.append(r)
    return records


# --------------------------------------------------------------------------- #
# Shaping:  records -> weeks -> days -> spaces -> sessions
# --------------------------------------------------------------------------- #

def week_start(d: date) -> date:
    """Sunday that begins the week containing d (Sun-Sat weeks)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def split_sessions(space_records: list, my_email: str, gap_minutes: int) -> list:
    """
    Given all records for one space on one day (time-ordered), produce sessions.

    Sessions are bounded by gaps between MY messages. Context (others') messages
    attach to the session of the message they precede. Each session:
        { "start": dt, "end": dt, "my_count": int,
          "items": [ {"kind": "me"|"context", "record": r}, ... ] }
    where start/end span your first..last message (how long you were engaged).
    """
    gap = timedelta(minutes=gap_minutes)
    sessions = []
    current = None
    pending_context = []
    last_mine = None

    def new_session():
        return {"start": None, "end": None, "my_count": 0, "items": []}

    for r in space_records:
        is_mine = (r.get("personEmail") or "").lower() == my_email
        if is_mine:
            if current is None or (last_mine is not None and r["_local"] - last_mine > gap):
                current = new_session()
                sessions.append(current)
            # context that preceded this message belongs with it
            for c in pending_context:
                current["items"].append({"kind": "context", "record": c})
            pending_context = []
            current["items"].append({"kind": "me", "record": r})
            current["my_count"] += 1
            if current["start"] is None:
                current["start"] = r["_local"]
            current["end"] = r["_local"]
            last_mine = r["_local"]
        else:
            pending_context.append(r)

    return sessions


def build_weeks(records: list, my_email: str, gap_minutes: int) -> list:
    """
    Full shaped structure, ready for any renderer:
        [ { "start": date(Sun), "end": date(Sat),
            "days": [ { "date": date,
                        "spaces": [ { "title": str, "type": "group"|"direct",
                                      "sessions": [...] } ] } ] } ]
    Only days/spaces where you posted appear (guaranteed by the fetcher).
    """
    # day -> roomId -> list of records
    by_day = {}
    room_meta = {}
    for r in records:
        d = r["_local"].date()
        by_day.setdefault(d, {}).setdefault(r["roomId"], []).append(r)
        room_meta[r["roomId"]] = (r.get("roomTitle", "(untitled)"),
                                  r.get("roomType", "group"))

    # week(Sun) -> list of day dicts
    weeks = {}
    for d in sorted(by_day):
        spaces = []
        for room_id, recs in by_day[d].items():
            recs.sort(key=lambda x: x["_local"])
            sessions = split_sessions(recs, my_email, gap_minutes)
            if not sessions:
                continue
            title, rtype = room_meta[room_id]
            spaces.append({"title": title, "type": rtype,
                          "sessions": sessions,
                          "_first": sessions[0]["start"]})
        # order spaces by when you first engaged that day
        spaces.sort(key=lambda s: s["_first"])
        weeks.setdefault(week_start(d), []).append({"date": d, "spaces": spaces})

    return [{"start": ws, "end": ws + timedelta(days=6), "days": weeks[ws]}
            for ws in sorted(weeks)]


# --------------------------------------------------------------------------- #
# Rendering (text)
# --------------------------------------------------------------------------- #

def fmt_time(dt: datetime) -> str:
    """12-hour, no leading zero, lowercase meridiem: 9:12am, 5:04pm."""
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def cap_text(text: str, limit: int):
    """Truncate on a word/line boundary; return (shown, hidden_char_count)."""
    text = (text or "").rstrip()
    if len(text) <= limit:
        return text, 0
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > limit * 0.6:
        cut = cut[:boundary]
    cut = cut.rstrip()
    return cut, len(text) - len(cut)


def render_message(lines_out: list, r: dict, char_limit: int):
    """A message you sent: timestamp + (capped, indented) body + file note."""
    stamp = fmt_time(r["_local"])
    indent = " " * (10 + len(stamp) + 2)  # align wrapped lines under the text

    body, hidden = cap_text(r.get("text", ""), char_limit)
    body_lines = body.split("\n") if body else []

    if not body_lines and r.get("sharedFiles"):
        n = r["sharedFiles"]
        lines_out.append(f"        {stamp}  [shared {n} file{'s' if n > 1 else ''}]")
        return

    first = body_lines[0] if body_lines else ""
    lines_out.append(f"        {stamp}  {first}")
    for ln in body_lines[1:]:
        lines_out.append(f"{indent}{ln}")
    if r.get("sharedFiles"):
        n = r["sharedFiles"]
        lines_out.append(f"{indent}[shared {n} file{'s' if n > 1 else ''}]")
    if hidden:
        lines_out.append(f"{indent}… (+{hidden:,} more chars)")


def render_context(lines_out: list, r: dict):
    """A message you were replying to: one dimmed, short line."""
    text = " ".join((r.get("text") or "").split())
    shown, hidden = cap_text(text, CONTEXT_CHAR_LIMIT)
    if hidden:
        shown += " …"
    if not shown and r.get("sharedFiles"):
        shown = f"[shared {r['sharedFiles']} file(s)]"
    name = r.get("personDisplayName") or r.get("personEmail", "someone")
    lines_out.append(f"        ‹ {name}: {shown}")


def render_text(weeks: list, period: str, settings: dict, generated: datetime) -> str:
    tz_name = settings["timezone"]
    char_limit = settings["message_char_limit"]
    gap = settings["session_gap_minutes"]

    out = []
    out.append(f"WEBEX ACTIVITY — {period}")
    out.append(f"Generated {generated.strftime('%Y-%m-%d %H:%M')} · "
               f"times local ({tz_name}) · session gap {gap} min "
               f"· message cap {char_limit} chars")
    out.append("=" * 66)

    if not weeks:
        out.append("")
        out.append("No activity found for this period.")
        return "\n".join(out) + "\n"

    for week in weeks:
        out.append("")
        out.append(f"WEEK OF {week['start'].strftime('%a %Y-%m-%d')} "
                   f"– {week['end'].strftime('%a %Y-%m-%d')}")
        out.append("-" * 66)

        for day in week["days"]:
            out.append("")
            out.append(f"  {day['date'].strftime('%a %Y-%m-%d')}")

            for space in day["spaces"]:
                label = "1:1" if space["type"] == "direct" else "group"
                out.append("")
                out.append(f"    {space['title']} · {label}")

                for sess in space["sessions"]:
                    if sess["start"] == sess["end"]:
                        span = fmt_time(sess["start"])
                    else:
                        span = f"{fmt_time(sess['start'])}–{fmt_time(sess['end'])}"
                    n = sess["my_count"]
                    out.append(f"        {span} · {n} msg{'s' if n != 1 else ''}")

                    for item in sess["items"]:
                        if item["kind"] == "context":
                            render_context(out, item["record"])
                        else:
                            render_message(out, item["record"], char_limit)
                    out.append("        Hours: ____")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Render a Webex activity summary from data/ (no network).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--month", help="Target month, YYYY-MM (default: current month)")
    group.add_argument("--day", help="Target day, YYYY-MM-DD")
    parser.add_argument("--stdout", action="store_true",
                        help="Print to stdout instead of writing the file")
    args = parser.parse_args()

    settings = load_settings()
    tz = ZoneInfo(settings["timezone"])

    period = args.month or args.day or datetime.now(tz).strftime("%Y-%m")
    out_month = period[:7]  # file is named by covered month
    files = day_files_for(period)
    if not files:
        sys.exit(f"No data files found for {period} in {DATA_DIR}/ "
                 "(run fetcher.py first).")

    records = load_records(files, tz)
    weeks = build_weeks(records, settings["email"].lower(),
                       settings["session_gap_minutes"])
    text = render_text(weeks, out_month, settings, datetime.now(tz))

    if args.stdout:
        sys.stdout.write(text)
        return

    out_path = os.path.join(HERE, f"webex-summary-{out_month}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
