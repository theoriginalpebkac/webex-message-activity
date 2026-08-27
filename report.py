#!/usr/bin/env python3
"""
Webex activity report — a month-end memory aid for "when and what did I discuss?".

Reads the day files produced by fetcher.py (data/YYYY-MM-DD.jsonl) and renders a
summary of the days you posted, grouped:

    Week (Sun-Sat)  ->  Day  ->  Space/person  ->  Session(s)

A "session" is a burst of your messages in one space, split when the gap between
two of your consecutive messages exceeds session_gap_minutes. Each session shows
its time span, your messages, and the context messages you were replying to
(dimmed). The report is purely informational — it records when conversations
happened and what was said; it does not track or total effort.

Two renderers share the same shaped data (build_weeks):
  - render_html (default): a self-contained page, one week at a time with a
    toggle to view all weeks stacked. Message text is shown in full.
  - render_text (--text): the plain-text report, message bodies capped at
    textformat_message_char_limit for terminal width.

This script NEVER touches the Webex API — it only reads data/.
"""

import os
import sys
import glob
import json
import html
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
    s.setdefault("textformat_message_char_limit", 500)
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
    char_limit = settings["textformat_message_char_limit"]
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

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Rendering (HTML)
# --------------------------------------------------------------------------- #

HTML_CONTEXT_CHAR_LIMIT = 240  # dimmed context is orientation, not the record

HTML_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #1c2024;
  --ink-soft: #545a61;
  --ink-faint: #868d95;
  --line: #e3e6ea;
  --line-strong: #cfd4da;
  --day-rule: #9aa2ad;
  --accent: #2a6ee0;
  --accent-soft: #eaf1fd;
  --me-bar: #2a6ee0;
  --badge-group-bg: #eef1f4;
  --badge-group-ink: #545a61;
  --badge-direct-bg: #e5f2ec;
  --badge-direct-ink: #1f6b4a;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a;
    --panel: #1d2024;
    --ink: #e7eaed;
    --ink-soft: #a8afb7;
    --ink-faint: #737b84;
    --line: #2b2f34;
    --line-strong: #3a3f45;
    --day-rule: #59616b;
    --accent: #5b9bff;
    --accent-soft: #1b2740;
    --me-bar: #5b9bff;
    --badge-group-bg: #262b30;
    --badge-group-ink: #a8afb7;
    --badge-direct-bg: #1a2e26;
    --badge-direct-ink: #7fc9a5;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.5;
}
.wrap { width: 100%; margin: 0; padding: 0 24px 80px; }

header.report {
  padding: 28px 0 18px;
}
header.report h1 {
  margin: 0 0 6px;
  font-size: 22px;
  letter-spacing: -0.01em;
}
header.report .meta {
  color: var(--ink-faint);
  font-size: 12.5px;
}
header.report .meta code { font-family: var(--mono); }

/* Sticky week navigation */
.weeknav {
  position: sticky;
  top: 0;
  z-index: 10;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: saturate(1.4) blur(8px);
  -webkit-backdrop-filter: saturate(1.4) blur(8px);
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  flex-wrap: wrap;
}
.weeknav .spacer { flex: 1 1 auto; }
.weeknav button, .weeknav select {
  font: inherit;
  font-size: 13.5px;
  color: var(--ink);
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  padding: 6px 11px;
  cursor: pointer;
}
.weeknav button:hover:not(:disabled),
.weeknav select:hover { border-color: var(--accent); }
.weeknav button:disabled { opacity: 0.4; cursor: default; }
.weeknav select { padding-right: 26px; max-width: 60vw; }
.weeknav .nav-pos { color: var(--ink-faint); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.weeknav .toggle-all { font-weight: 600; }
.weeknav.all-mode .single-only { display: none; }

/* Week block */
.week { padding-top: 26px; }
.week > h2 {
  margin: 0 0 2px;
  font-size: 17px;
  letter-spacing: -0.01em;
}
.week > .wsum {
  margin: 0 0 14px;
  color: var(--ink-faint);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
body:not(.all-mode) .week.hidden { display: none; }

/* Activity table */
table.activity {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
}
table.activity col.c-date { width: 132px; }
table.activity col.c-time { width: 118px; }
table.activity th {
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--ink-faint);
  font-weight: 600;
  padding: 9px 14px;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 92%, var(--ink) 3%);
}
table.activity td {
  padding: 12px 14px;
  vertical-align: top;
  border-bottom: 1px solid var(--line);
}
table.activity tr:last-child td { border-bottom: none; }
td.date {
  border-right: 1px solid var(--line);
  white-space: nowrap;
}
td.date .dow { display: block; font-weight: 700; font-size: 17.5px; letter-spacing: -0.01em; }
td.date .dnum { color: var(--ink-soft); font-size: 12.5px; }
/* new-day rows get a heavier full-width rule and extra breathing room above */
tr.day-start td { border-top: 3px solid var(--day-rule); padding-top: 16px; }
tr.day-start:first-child td { border-top: none; padding-top: 12px; }

td.time {
  border-right: 1px solid var(--line);
  white-space: nowrap;
}
td.time .span {
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}
td.time .count { display: block; margin-top: 3px; color: var(--ink-faint); font-size: 11.5px; }

td.msgs { width: auto; }
.space {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 9px;
}
.space .title { font-weight: 650; font-size: 19px; }
.badge {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 7px;
  border-radius: 999px;
}
.badge.group { background: var(--badge-group-bg); color: var(--badge-group-ink); }
.badge.direct { background: var(--badge-direct-bg); color: var(--badge-direct-ink); }

.msg {
  display: flex;
  gap: 10px;
  padding: 3px 0;
}
.msg + .msg { border-top: 1px dotted var(--line); padding-top: 7px; margin-top: 4px; }
.msg .mtime {
  flex: none;
  width: 62px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--ink-faint);
  padding-top: 2px;
  font-variant-numeric: tabular-nums;
}
.msg .mbody {
  flex: 1 1 auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.msg .file { color: var(--ink-faint); font-style: italic; }

.ctx {
  margin: 2px 0 4px 72px;
  padding-left: 10px;
  border-left: 2px solid var(--line-strong);
  color: var(--ink-faint);
  font-size: 13px;
}
.ctx .who { font-weight: 600; color: var(--ink-soft); }

.empty { padding: 40px 0; color: var(--ink-faint); }

footer.report { margin-top: 34px; color: var(--ink-faint); font-size: 12px; }

@media print {
  .weeknav { display: none; }
  body:not(.all-mode) .week.hidden,
  body .week.hidden { display: block !important; }
  .week { break-inside: avoid-page; }
  table.activity { border-radius: 0; }
}
@media (max-width: 620px) {
  .wrap { padding: 0 12px 60px; }
  table.activity col.c-date { width: 96px; }
  table.activity col.c-time { width: 88px; }
  table.activity td, table.activity th { padding: 9px 9px; }
  td.date .dow { font-size: 15.5px; }
  td.date { white-space: normal; }
  .msg { flex-direction: column; gap: 1px; }
  .msg .mtime { width: auto; }
  .ctx { margin-left: 0; }
}
"""

HTML_JS = """
(function () {
  var body = document.body;
  var weeks = Array.prototype.slice.call(document.querySelectorAll('.week'));
  if (!weeks.length) return;
  var sel = document.getElementById('weekSelect');
  var prev = document.getElementById('weekPrev');
  var next = document.getElementById('weekNext');
  var pos = document.getElementById('weekPos');
  var toggle = document.getElementById('toggleAll');
  var nav = document.getElementById('weeknav');
  var LS_IDX = 'webex_week_idx', LS_ALL = 'webex_view_all';

  function get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} }

  var idx = 0;
  var stored = parseInt(get(LS_IDX), 10);
  if (!isNaN(stored) && stored >= 0 && stored < weeks.length) idx = stored;
  var allMode = get(LS_ALL) === '1';

  function showSingle(i) {
    idx = Math.max(0, Math.min(weeks.length - 1, i));
    weeks.forEach(function (w, n) { w.classList.toggle('hidden', n !== idx); });
    if (sel) sel.value = String(idx);
    if (prev) prev.disabled = idx === 0;
    if (next) next.disabled = idx === weeks.length - 1;
    if (pos) pos.textContent = (idx + 1) + ' / ' + weeks.length;
    set(LS_IDX, String(idx));
  }

  function applyMode() {
    body.classList.toggle('all-mode', allMode);
    nav.classList.toggle('all-mode', allMode);
    if (allMode) {
      weeks.forEach(function (w) { w.classList.remove('hidden'); });
      toggle.textContent = 'View one week at a time';
    } else {
      toggle.textContent = 'View all weeks on one page';
      showSingle(idx);
    }
    set(LS_ALL, allMode ? '1' : '0');
  }

  if (sel) sel.addEventListener('change', function () { showSingle(parseInt(sel.value, 10)); });
  if (prev) prev.addEventListener('click', function () { showSingle(idx - 1); });
  if (next) next.addEventListener('click', function () { showSingle(idx + 1); });
  if (toggle) toggle.addEventListener('click', function () { allMode = !allMode; applyMode(); });
  document.addEventListener('keydown', function (e) {
    if (allMode || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft' && idx > 0) { showSingle(idx - 1); }
    else if (e.key === 'ArrowRight' && idx < weeks.length - 1) { showSingle(idx + 1); }
  });

  applyMode();
})();
"""


def _esc(s) -> str:
    return html.escape(s if s is not None else "", quote=True)


def _badge(space_type: str) -> str:
    if space_type == "direct":
        return '<span class="badge direct">1:1</span>'
    return '<span class="badge group">group</span>'


def _html_message(r: dict) -> str:
    """One of your messages: mono timestamp + full body + optional file note."""
    stamp = _esc(fmt_time(r["_local"]))
    body = (r.get("text") or "").rstrip()
    file_note = ""
    if r.get("sharedFiles"):
        n = r["sharedFiles"]
        file_note = f'<span class="file">[shared {n} file{"s" if n > 1 else ""}]</span>'
    if body:
        inner = _esc(body)
        if file_note:
            inner += "\n" + file_note
    else:
        inner = file_note or '<span class="file">[no text]</span>'
    return (f'<div class="msg"><span class="mtime">{stamp}</span>'
            f'<span class="mbody">{inner}</span></div>')


def _html_context(r: dict) -> str:
    """A message you were replying to: dimmed, capped, name-tagged."""
    text = " ".join((r.get("text") or "").split())
    shown, hidden = cap_text(text, HTML_CONTEXT_CHAR_LIMIT)
    if hidden:
        shown += " …"
    if not shown and r.get("sharedFiles"):
        shown = f'[shared {r["sharedFiles"]} file(s)]'
    name = r.get("personDisplayName") or r.get("personEmail") or "someone"
    return (f'<div class="ctx"><span class="who">{_esc(name)}</span> '
            f'{_esc(shown)}</div>')


def _session_span(sess: dict) -> str:
    if sess["start"] == sess["end"]:
        return _esc(fmt_time(sess["start"]))
    return f'{_esc(fmt_time(sess["start"]))}–{_esc(fmt_time(sess["end"]))}'


def _html_week(week: dict) -> str:
    """One week's activity table plus a small summary line."""
    days = week["days"]
    n_days = len(days)
    n_spaces = sum(len(d["spaces"]) for d in days)
    n_msgs = sum(sess["my_count"]
                 for d in days for s in d["spaces"] for sess in s["sessions"])

    rows = []
    for day in days:
        # flatten this day's (space, session) pairs into consecutive rows
        day_pairs = [(sp, se) for sp in day["spaces"] for se in sp["sessions"]]
        span_rows = len(day_pairs)
        for i, (space, sess) in enumerate(day_pairs):
            cells = []
            row_cls = "day-start" if i == 0 else ""
            if i == 0:
                cells.append(
                    f'<td class="date" rowspan="{span_rows}">'
                    f'<span class="dow">{_esc(day["date"].strftime("%A"))}</span>'
                    f'<span class="dnum">{_esc(day["date"].strftime("%b %-d"))}</span></td>')

            n = sess["my_count"]
            cells.append(
                f'<td class="time"><span class="span">{_session_span(sess)}</span>'
                f'<span class="count">{n} msg{"s" if n != 1 else ""}</span></td>')

            body = [f'<div class="space"><span class="title">{_esc(space["title"])}</span>'
                    f'{_badge(space["type"])}</div>']
            for item in sess["items"]:
                if item["kind"] == "context":
                    body.append(_html_context(item["record"]))
                else:
                    body.append(_html_message(item["record"]))
            cells.append(f'<td class="msgs">{"".join(body)}</td>')

            cls = f' class="{row_cls}"' if row_cls else ""
            rows.append(f"<tr{cls}>{''.join(cells)}</tr>")

    heading = (f'{week["start"].strftime("%b %-d")} – '
               f'{week["end"].strftime("%b %-d, %Y")}')
    summary = (f'{n_days} day{"s" if n_days != 1 else ""} active · '
               f'{n_spaces} conversation{"s" if n_spaces != 1 else ""} · '
               f'{n_msgs} message{"s" if n_msgs != 1 else ""}')

    return (
        '<section class="week hidden">'
        f'<h2>Week of {_esc(heading)}</h2>'
        f'<p class="wsum">{_esc(summary)}</p>'
        '<table class="activity">'
        '<colgroup><col class="c-date"><col class="c-time"><col class="c-msgs"></colgroup>'
        '<thead><tr><th>Date</th><th>Time</th><th>Conversations</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
        '</section>'
    )


def render_html(weeks: list, period: str, settings: dict, generated: datetime) -> str:
    tz_name = settings["timezone"]
    gap = settings["session_gap_minutes"]

    meta = (f'Generated {generated.strftime("%Y-%m-%d %H:%M")} · '
            f'times local (<code>{_esc(tz_name)}</code>) · '
            f'session gap {gap} min')

    if weeks:
        options = "".join(
            f'<option value="{i}">Week of '
            f'{w["start"].strftime("%b %-d")} – {w["end"].strftime("%b %-d")}</option>'
            for i, w in enumerate(weeks))
        nav = (
            '<nav class="weeknav" id="weeknav">'
            '<button id="weekPrev" class="single-only" title="Previous week (←)">‹ Prev</button>'
            f'<select id="weekSelect" class="single-only">{options}</select>'
            '<button id="weekNext" class="single-only" title="Next week (→)">Next ›</button>'
            '<span id="weekPos" class="nav-pos single-only"></span>'
            '<span class="spacer"></span>'
            '<button id="toggleAll" class="toggle-all">View all weeks on one page</button>'
            '</nav>')
        weeks_html = "".join(_html_week(w) for w in weeks)
        body = nav + weeks_html
    else:
        body = '<p class="empty">No activity found for this period.</p>'

    return (
        '<!doctype html>\n'
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Webex activity — {_esc(period)}</title>\n'
        f'<style>{HTML_CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="wrap">\n'
        '<header class="report">'
        f'<h1>Webex activity — {_esc(period)}</h1>'
        f'<div class="meta">{meta}</div>'
        '</header>\n'
        f'{body}\n'
        '<footer class="report">Informational summary of your posted messages · '
        'read one week, then move to the next.</footer>\n'
        '</div>\n'
        f'<script>{HTML_JS}</script>\n'
        '</body>\n</html>\n'
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Render a Webex activity summary from data/ (no network).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--month", help="Target month, YYYY-MM (default: current month)")
    group.add_argument("--day", help="Target day, YYYY-MM-DD")
    parser.add_argument("--text", action="store_true",
                        help="Render the plain-text report instead of HTML (default)")
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

    now = datetime.now(tz)
    if args.text:
        output = render_text(weeks, out_month, settings, now)
        ext = "txt"
    else:
        output = render_html(weeks, out_month, settings, now)
        ext = "html"

    if args.stdout:
        sys.stdout.write(output)
        return

    out_path = os.path.join(HERE, f"webex-summary-{out_month}.{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
