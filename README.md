# webex-message-activity

A small, deterministic command-line tool that extracts **your own** Webex message
activity for a month and renders it as a readable summary — a plain-language
reminder of who you talked to, in which spaces, and on which days.

It is intentionally simple: it reads message data, filters it to the days you
actually posted, and formats the result. There is no LLM, no summarization of
other people's words, and no estimation of hours — just a faithful, grouped view
of your own messages so you can reconstruct a month's communications at a glance
(useful, for example, when filling in a timecard from memory).

The default output is a **self-contained HTML page** built for reviewing one week
at a time (with a toggle to view all weeks stacked); pass `--text` for a plain
`.txt` report instead.

## How it works

The tool is two independent stages so the (slow, networked) fetch is decoupled
from the (fast, local) formatting — you can refetch rarely and reformat freely.

1. **`fetcher.py`** — pulls messages from the Webex API for a target month (or a
   single day) and saves them locally under `data/`, one file per calendar day.
   It reads broadly but persists narrowly: only the `(space, day)` buckets where
   *you* posted are kept, plus up to two preceding messages for context.

2. **`report.py`** — reads the saved `data/` files (never the network) and writes
   a `webex-summary-YYYY-MM.html` (or `.txt` with `--text`) grouped as:

   ```
   Week (Sun–Sat)  →  Day  →  Space / person  →  Session(s)
   ```

   A *session* is a burst of your messages in one space, split whenever the gap
   between two of your consecutive messages exceeds a configurable threshold — so
   a morning and an afternoon exchange in the same space appear as separate rows.
   The HTML report renders each week as a Date / Time / Conversations table and
   shows one week at a time; the text report caps each message at
   `textformat_message_char_limit` for terminal width, while the HTML report shows
   full text.

With no arguments, both stages default to the **current month**:

```bash
./venv/bin/python fetcher.py     # fetch the current month into data/
./venv/bin/python report.py      # render webex-summary-<month>.html
```

Or target an explicit period, force text, or print to stdout:

```bash
./venv/bin/python fetcher.py --month 2026-08
./venv/bin/python report.py  --month 2026-08          # HTML (default)
./venv/bin/python report.py  --month 2026-08 --text   # plain-text report
./venv/bin/python report.py  --month 2026-08 --stdout # print instead of writing
./venv/bin/python fetcher.py --day 2026-08-19         # single day (handy for testing)
```

## What gets saved

Each line in a `data/YYYY-MM-DD.jsonl` file is one message (JSON), self-contained
and greppable. The full Webex payload is preserved except styling (`html`) and
attachment/file blobs, which are dropped. A `sharedFiles` count is added as a
breadcrumb when a message carried an attachment. The following are denormalized
onto every record so the report never needs the API:

| Field | Meaning |
|-------|---------|
| `roomTitle` | space name (or the other person's name for a 1:1) |
| `roomType` | `group` or `direct` |
| `personDisplayName` | resolved sender name (cached in `data/people_cache.json`) |

Times are stored as UTC (as the API returns them) and converted to your local
timezone when the report decides which calendar day a message belongs to.

## Configuration

Two files, split by how often they change (both are gitignored; copy the
`.example` versions):

- **`credentials.txt`** — your Webex token, and nothing else. Webex
  developer-portal tokens expire roughly every 12 hours, so you paste in a fresh
  one before a run. The fetcher warns if the file looks stale and treats an API
  `401` as an expired token.

- **`settings.yaml`** — stable settings:

  | Key | Purpose |
  |-----|---------|
  | `email` | the address you post under (identifies *your* messages) |
  | `timezone` | your IANA timezone, e.g. `America/New_York` |
  | `textformat_message_char_limit` | max characters per message in the `--text` report (HTML shows full) |
  | `session_gap_minutes` | gap that starts a new session (default 90) |

See [INSTALL.md](INSTALL.md) for setup on macOS and Linux.

## Runs are safe to repeat

- **Fetch is idempotent and resumable.** Re-running a period rebuilds that
  period's day files from scratch (no duplicates). If a run is interrupted — for
  example by a rate-limit back-off — it records progress per space in
  `data/.state.json` and resumes where it left off.
- **Report is a pure re-render.** Re-running overwrites `webex-summary-<month>.html`
  (or `.txt`).

## Authentication

Authentication is a manually supplied Webex Personal Access Token only. The token
grants read access to your own rooms and messages; the tool issues **read-only**
requests and never writes to your account.

## Privacy

`data/` and the generated `webex-summary-*` reports contain real message text — which
can include other people's words, internal links, and occasionally secrets. Both
are gitignored by default. Treat them as local, sensitive files.
